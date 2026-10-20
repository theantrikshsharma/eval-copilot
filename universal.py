"""
Universal App Testing: call any JSON HTTP API under SSRF guards and map its response to the text MANTIS evaluates.

Guards: http/https only, no credentials in the URL, every resolved address must be public (blocks loopback, private,
link-local/metadata, shared, reserved and multicast ranges, including IPv4-mapped IPv6), redirects are never followed,
bounded timeouts and response size, and configured header values are scrubbed from anything stored or returned.
Like the other adapters, invoke() never raises.
"""

import ipaddress
import json
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

import requests

from adapters import AdapterResponse

INPUT_PLACEHOLDER = "{{input}}"
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH")
MAX_RESPONSE_BYTES = 1_000_000
MAX_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 5.0
MIN_SECRET_CHARS = 4
MAX_ERROR_CHARS = 300


class UnsafeTarget(ValueError):
    """The URL or its resolved addresses are not allowed."""


def check_url(url: str) -> List[str]:
    """Validates scheme, host and every resolved address. Returns the addresses; raises UnsafeTarget."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeTarget("Only http:// and https:// URLs are allowed.")
    if not parts.hostname:
        raise UnsafeTarget("The URL has no host.")
    if parts.username or parts.password:
        raise UnsafeTarget("Put credentials in a header, not in the URL.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError, UnicodeError):
        raise UnsafeTarget(f"Could not resolve {parts.hostname}.")
    addresses = sorted({info[4][0] for info in infos})
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if not (mapped or ip).is_global or (mapped or ip).is_multicast:
            raise UnsafeTarget(f"{parts.hostname} resolves to a non-public address ({address}); internal targets are blocked.")
    return addresses


def render_request(config: Dict[str, Any], test_input: str):
    """The URL and body for one input. The input is JSON-escaped into the body and URL-encoded into the URL."""
    url = config["url"].replace(INPUT_PLACEHOLDER, quote(test_input, safe=""))
    if config["method"] == "GET" or not config.get("body_template"):
        return url, None
    escaped = json.dumps(test_input)[1:-1]
    body = config["body_template"].replace(INPUT_PLACEHOLDER, escaped)
    json.loads(body)  # the template must stay valid JSON after substitution
    return url, body.encode()


def map_response(data: Any, path: str) -> Optional[str]:
    """Follows a dotted path (e.g. choices.0.message.content). Empty path keeps the whole body."""
    for key in [k for k in (path or "").split(".") if k]:
        if isinstance(data, list) and key.isdigit() and int(key) < len(data):
            data = data[int(key)]
        elif isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return None
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)


def redact(text: str, headers: Dict[str, str]) -> str:
    for value in headers.values():
        for secret in {value, value.split(" ", 1)[-1]}:
            if len(secret) >= MIN_SECRET_CHARS:
                text = text.replace(secret, "[redacted]")
    return text


def validate_config(config: Dict[str, Any]):
    """Raises UnsafeTarget or ValueError with a message the user can act on."""
    if config["method"] not in ALLOWED_METHODS:
        raise ValueError(f"Method must be one of {', '.join(ALLOWED_METHODS)}.")
    check_url(config["url"].replace(INPUT_PLACEHOLDER, "x"))
    try:
        render_request(config, 'sample "input"')
    except ValueError:
        raise ValueError('The request template must be valid JSON with "{{input}}" inside a string, e.g. {"question": "{{input}}"}.')


def invoke(config: Dict[str, Any], test_input: str, timeout: float = 20.0) -> AdapterResponse:
    headers = dict(config.get("headers") or {})
    start = time.time()

    def failed(error: str, status: Optional[int] = None, contract: bool = False) -> AdapterResponse:
        error = redact(error, headers)[:MAX_ERROR_CHARS]
        return AdapterResponse(success=False, status_code=status, latency_ms=round((time.time() - start) * 1000, 2),
                               raw_response="", error=error, contract_error=error if contract else None)

    try:
        url, body = render_request(config, test_input)
        check_url(url)  # resolved again right before the call, so a changed DNS answer is caught
    except UnsafeTarget as e:
        return failed(f"Blocked: {e}")
    except ValueError:
        return failed("The request template is not valid JSON after inserting the input.")
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
    try:
        with requests.request(config["method"], url, headers=headers, data=body, stream=True, allow_redirects=False,
                              timeout=(CONNECT_TIMEOUT_S, min(float(timeout), MAX_TIMEOUT_S))) as resp:
            if 300 <= resp.status_code < 400:
                return failed(f"Redirects are not followed (HTTP {resp.status_code}).", resp.status_code)
            raw = b""
            for chunk in resp.iter_content(65_536):
                raw += chunk
                if len(raw) > MAX_RESPONSE_BYTES:
                    return failed(f"Response exceeded {MAX_RESPONSE_BYTES} bytes.", resp.status_code)
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
            if resp.status_code != 200:
                return failed(f"HTTP {resp.status_code}: {text[:200]}", resp.status_code)
    except requests.exceptions.Timeout:
        return failed(f"Request timed out after {min(float(timeout), MAX_TIMEOUT_S)}s")
    except requests.exceptions.RequestException as e:
        return failed(f"Request failed: {type(e).__name__}")

    latency = round((time.time() - start) * 1000, 2)
    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        if config.get("response_path"):
            return failed("Response is not JSON, so the response mapping can't be applied.", 200, contract=True)
    answer = map_response(parsed, config.get("response_path", "")) if parsed is not None else text
    if answer is None:
        return failed(f"Response mapping '{config['response_path']}' matched nothing in the response.", 200, contract=True)
    if not answer.strip():
        return failed("The mapped response is empty.", 200, contract=True)
    return AdapterResponse(success=True, status_code=200, latency_ms=latency, raw_response=redact(answer, headers),
                           parsed_data=parsed if isinstance(parsed, dict) else None)
