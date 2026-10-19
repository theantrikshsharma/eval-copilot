# MANTIS — Checkpoint 4 MVP

**Machine-Assisted Network Testing & Intelligent Scrutiny.** Hunt failures. Prove reliability.

*Every verdict requires evidence.* (Formerly Eval Co-pilot.)

MANTIS catches the cases a friend's AI feature gets wrong, before they ship it. It tests real inputs against live target applications, evaluates each response against golden expectations, persists complete execution history, and provides side-by-side run comparisons.

## 1. Architecture

- **Frontend:** A single-workspace web UI in `web/` (plain HTML/CSS/JS, no build step) served at `/`. Pick a test case, run it, read the verdict, select a check to highlight its evidence inside the actual response, and compare any two runs. Every view is a shareable URL. The original Gradio UI remains at `/classic`.
- **Backend:** FastAPI REST API exposing `/api/golden-pairs`, `/api/execute`, `/api/results`, `/api/compare`, and `/api/summary`.
- **Integration:** One FastAPI server on a single origin (`http://127.0.0.1:8000`) serves the web UI at `/`, the REST API at `/api/...`, and the Gradio UI at `/classic`.
- **Database:** Supabase PostgreSQL using the official Supabase Python SDK (`supabase>=2.33`). When Supabase credentials are not set, it automatically falls back to a local persistent SQLite database (`eval_copilot.db`) with the identical 3-table relational schema and queries, preserving all data across restarts.
- **Security:** Privileged credentials remain strictly on the server via environment variables (`.env`). No API keys are exposed to the client browser.

## 2. Target Applications & Integrations

- **Target A — Anupma's Travel Itinerary Generator:**
  - Live URL: `https://04-gradio-client.vercel.app/`
  - Endpoint: `POST https://04-gradio-client.vercel.app/gradio_api/run/predict`
  - Payload: `{"data": ["<trip details>"], "fn_index": 1}`
  - Response: JSON payload containing structured itinerary or text.
- **Target B — Prateek's Knowledge Retrieval System:**
  - Live URL: `https://knowledge-retriever-mvp.vercel.app/docs`
  - Endpoint: `POST https://knowledge-retriever-mvp.vercel.app/api/ans_fact`
  - Payload: `{"text": "<query>"}`
  - Response: JSON payload containing `answer` string with citations and source excerpts.

## 3. Model-Backed Checks (optional)

Both run after the deterministic checks, and each is skipped when its key is missing from `.env`.

- **LLM judge** (`GROQ_API_KEY`, `openai/gpt-oss-120b`): every check left as `NEEDS REVIEW` is sent to the judge. A `PASS`/`FAIL` verdict is accepted only when its quote appears verbatim in the response; otherwise the check stays `NEEDS REVIEW`. Deterministic `PASS`/`FAIL` results are never sent to the judge. The evidence records both the deterministic reason and the judge's verdict, model and prompt version.
- **Content Safety** (`OPENROUTER_API_KEY`, `nvidia/nemotron-3.5-content-safety:free`): adds one check per non-empty response. An unsafe response is `FAIL`, with the hazard categories as evidence.
- A provider outage, timeout or rate limit makes the check `NEEDS REVIEW`, never `ERROR`. `ERROR` is reserved for the target application failing.

## 4. Database Schema (Checkpoint 4)

Three relational tables with foreign keys:
1. `GOLDEN_PAIRS`:
   - `pair_id` (TEXT, PK): Identifier for test case (e.g. `A-01`, `P-01`).
   - `target_app` (TEXT): Name of target application.
   - `input` (TEXT): Exact prompt / payload sent to target.
   - `expected_behaviour` (TEXT): Explicit behavioural criteria and constraints.
2. `RUN_RESULTS`:
   - `result_id` (BIGINT, PK): Unique ID for each individual execution.
   - `run_id` (TEXT): Logical batch run identifier.
   - `pair_id` (TEXT, FK -> GOLDEN_PAIRS): Reference to golden pair.
   - `actual_response` (TEXT): Exact response or error from target app.
   - `overall_status` (TEXT): `PASS`, `FAIL`, `ERROR`, or `NEEDS REVIEW`.
   - `timestamp` (TIMESTAMPTZ): ISO execution timestamp.
3. `CHECK_RESULTS`:
   - `check_result_id` (BIGINT, PK): Unique check result ID.
   - `result_id` (BIGINT, FK -> RUN_RESULTS): Reference to run result.
   - `check_name` (TEXT): Name of specific deterministic check.
   - `status` (TEXT): `PASS`, `FAIL`, `ERROR`, or `NEEDS REVIEW`.
   - `evidence_or_reason` (TEXT): Recorded evidence or failure explanation.

## 5. Setup & Running Locally

### Step 1: Virtual Environment
```bash
cd ~/Documents/eval-copilot
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### Step 2: Database Configuration (Optional Supabase)
To use Supabase:
1. Create a project at [supabase.com](https://supabase.com).
2. Copy `.env.example` to `.env` and fill in `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.
3. Run `schema.sql` in the Supabase SQL Editor.

*Note: If `.env` is omitted, MANTIS runs using the persistent local SQLite database `eval_copilot.db` with identical schema and functionality. The file keeps its original name so existing run history carries over.*

### Step 3: Run Application
```bash
uvicorn main:app --reload --port 8000
```
Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## 6. Verification Test Suite
Run the automated acceptance test script (makes real network calls to the targets):
```bash
python -m unittest tests/test_mvp.py
```
Run the offline tests for the judge, content safety check and web routes, and the front-end logic tests:
```bash
python -m unittest tests/test_judges.py tests/test_web.py
node --test web/lib.test.mjs
```
