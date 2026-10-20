"""
Target Application Adapters for MANTIS.
Provides isolated, robust HTTP adapters for:
- Target A: Anupma's Travel Itinerary Generator (Gradio API on Vercel)
- Target B: Prateek's Knowledge Retrieval System (FastAPI on Vercel)
"""

import json
import time
from typing import Any, Dict, Optional
from pydantic import BaseModel
import requests


class AdapterResponse(BaseModel):
    success: bool
    status_code: Optional[int] = None
    latency_ms: float = 0.0
    raw_response: str = ""
    parsed_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # Set when the target answered HTTP 200 but the body breaks its response contract (unusable, so not success)
    contract_error: Optional[str] = None


class AnupmaAdapter:
    """
    Adapter for Anupma's Travel Itinerary Generator.
    Hosted Gradio app on Vercel: https://04-gradio-client.vercel.app/
    API endpoint: /gradio_api/run/predict (fn_index=1 for itinerary generation)
    """
    BASE_URL = "https://04-gradio-client.vercel.app"
    PREDICT_URL = f"{BASE_URL}/gradio_api/run/predict"

    @classmethod
    def invoke(cls, input_text: str, timeout: float = 20.0) -> AdapterResponse:
        start_time = time.time()
        payload = {
            "data": [input_text],
            "fn_index": 1
        }
        try:
            resp = requests.post(
                cls.PREDICT_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout
            )
            latency_ms = round((time.time() - start_time) * 1000, 2)
            
            if resp.status_code != 200:
                return AdapterResponse(
                    success=False,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    raw_response=resp.text,
                    error=f"HTTP {resp.status_code}: {resp.text[:300]}"
                )
            
            data = resp.json()
            raw_text = ""
            parsed = None
            if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                raw_text = str(data["data"][0])
                # Attempt to parse json inside the string
                try:
                    parsed = json.loads(raw_text)
                except Exception:
                    parsed = None
            else:
                raw_text = json.dumps(data)

            return AdapterResponse(
                success=True,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                raw_response=raw_text,
                parsed_data=parsed if isinstance(parsed, dict) else None,
                error=None
            )
        except requests.exceptions.Timeout:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return AdapterResponse(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                raw_response="",
                error=f"Request timed out after {timeout}s"
            )
        except requests.exceptions.ConnectionError as e:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return AdapterResponse(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                raw_response="",
                error=f"Connection error: {str(e)}"
            )
        except Exception as e:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return AdapterResponse(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                raw_response="",
                error=f"Unexpected error: {str(e)}"
            )


class PrateekAdapter:
    """
    Adapter for Prateek's Knowledge Retrieval System.
    Hosted FastAPI app on Vercel: https://knowledge-retriever-mvp.vercel.app/docs
    API endpoint: /api/ans_fact
    """
    BASE_URL = "https://knowledge-retriever-mvp.vercel.app"
    ANS_FACT_URL = f"{BASE_URL}/api/ans_fact"

    @classmethod
    def invoke(cls, input_text: str, timeout: float = 20.0) -> AdapterResponse:
        start_time = time.time()
        payload = {"text": input_text}
        try:
            resp = requests.post(
                cls.ANS_FACT_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout
            )
            latency_ms = round((time.time() - start_time) * 1000, 2)
            
            if resp.status_code != 200:
                return AdapterResponse(
                    success=False,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    raw_response=resp.text,
                    error=f"HTTP {resp.status_code}: {resp.text[:300]}"
                )

            # Observed contract (the OpenAPI 200 schema is empty): a JSON object with a non-empty string "answer"
            try:
                data = resp.json()
            except ValueError:
                data = None
            answer_text = data.get("answer") if isinstance(data, dict) else None
            if not isinstance(answer_text, str) or not answer_text.strip():
                problem = ("body is not JSON" if data is None else
                           "body is not a JSON object" if not isinstance(data, dict) else
                           "'answer' field is missing" if "answer" not in data else
                           "'answer' is not a string" if not isinstance(answer_text, str) else
                           "'answer' is empty")
                return AdapterResponse(
                    success=False,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    raw_response=resp.text,
                    parsed_data=data if isinstance(data, dict) else None,
                    error=f"Response contract violation: {problem}",
                    contract_error=f"Expected a JSON object with a non-empty string 'answer'; {problem}."
                )
            return AdapterResponse(
                success=True,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                raw_response=answer_text,
                parsed_data=data,
                error=None
            )
        except requests.exceptions.Timeout:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return AdapterResponse(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                raw_response="",
                error=f"Request timed out after {timeout}s"
            )
        except requests.exceptions.ConnectionError as e:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return AdapterResponse(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                raw_response="",
                error=f"Connection error: {str(e)}"
            )
        except Exception as e:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return AdapterResponse(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                raw_response="",
                error=f"Unexpected error: {str(e)}"
            )


def invoke_target(target_app: str, input_text: str, timeout: float = 20.0) -> AdapterResponse:
    target_clean = target_app.strip().lower()
    if "anupma" in target_clean or "travel" in target_clean:
        return AnupmaAdapter.invoke(input_text, timeout=timeout)
    elif "prateek" in target_clean or "knowledge" in target_clean or "retriev" in target_clean:
        return PrateekAdapter.invoke(input_text, timeout=timeout)
    else:
        return AdapterResponse(
            success=False,
            status_code=None,
            latency_ms=0.0,
            raw_response="",
            error=f"Unknown target application '{target_app}'. Supported targets: Anupma, Prateek."
        )
