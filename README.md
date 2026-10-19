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
- **Content Safety** (`nvidia/nemotron-3.5-content-safety:free`): opt-in, needing both `OPENROUTER_API_KEY` and `MANTIS_SEMANTIC_SAFETY=1`. Adds one check per non-empty response; an unsafe response is `FAIL`, with the hazard categories as evidence. After a daily-quota 429 it stops calling until 00:00 UTC.
- **Credential Leak (local)**: always on, no provider. Fails if a response contains what looks like an API key, token or private key (redacted in the evidence). It does not assess harmful content.
- A provider outage, timeout or rate limit makes the judge's check `NEEDS REVIEW`, never `ERROR`. `ERROR` is reserved for the target application failing.
- An optional check that couldn't run is stored as `NEEDS REVIEW` with evidence starting "Not evaluated:" (never `PASS`) and does not decide the run's verdict. The UI shows it as **Not evaluated**.
- **Display verdicts:** stored history is never rewritten. The API adds a `verdict` beside each run's stored `overall_status`; they differ only for older runs whose sole open item was an optional check that couldn't run, and those carry a `verdict_note` saying how they were recorded. Summary counts use display verdicts.

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
uv pip install -r requirements.txt                              # core: web UI, API, evaluator, storage
uv pip install -r requirements-classic.txt                      # optional: legacy Gradio view at /classic
```

### Step 2: Database Configuration (Optional Supabase)
To use Supabase:
1. Create a project at [supabase.com](https://supabase.com).
2. Copy `.env.example` to `.env` and fill in `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the service-role key: `schema.sql` enables Row Level Security, so the anon key has no access).
3. Run `schema.sql` in the Supabase SQL Editor.
4. To bring local history across: `python scripts/migrate_sqlite_to_supabase.py` (dry run, writes a backup to `backups/`), then the same with `--apply`. It never overwrites, re-running copies nothing twice, and the SQLite file is opened read-only.

*Note: If `.env` is omitted, MANTIS runs using the persistent local SQLite database `eval_copilot.db` with identical schema and functionality. The file keeps its original name so existing run history carries over.*

### Step 3: Run Application
```bash
uvicorn main:app --reload --port 8000
```
Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## 6. Verification Test Suite
Run the automated acceptance test script (makes real network calls to the targets; uses a temporary database):
```bash
python -m unittest tests/test_mvp.py
```
Run the offline tests (no network; anything that writes uses a temporary database) and the front-end logic tests:
```bash
python -m unittest tests/test_audit_fixes.py tests/test_safety_and_verdicts.py tests/test_case_checks.py tests/test_judges.py tests/test_web.py
node --test web/lib.test.mjs
```

## 7. Deploying to Vercel
`vercel.json` runs `main.py` as one Python function serving the UI, assets and API. `.vercelignore` keeps `.env`, local databases, the virtualenv and tests out of the upload. Only `requirements.txt` is installed (about 30 MB); the classic Gradio view is local-only.

Hosted deployments are deliberately strict:
- **Durable storage is required to save anything.** Vercel's filesystem is temporary and per-instance, so without Supabase the app is read-only and says so, rather than accepting runs it would lose.
- **Writes need `MANTIS_ADMIN_TOKEN`.** Running tests and adding cases send `Authorization: Bearer <token>`; the UI asks for it once per tab. Without the variable, hosted writes are refused.
- **The interactive API docs are off**, and every page is served with a strict Content Security Policy.

| Variable | Needed for | Environments |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Durable storage (required to save runs) | Production, Preview |
| `MANTIS_ADMIN_TOKEN` | Running tests and adding cases | Production, Preview |
| `GROQ_API_KEY` | Optional LLM judge | Production, Preview |
| `OPENROUTER_API_KEY` + `MANTIS_SEMANTIC_SAFETY=1` | Optional semantic content safety | Only if wanted |
