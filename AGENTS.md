# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, Copilot, Gemini, and others) working in this repo.

## Project

MANTIS (Machine-Assisted Network Testing & Intelligent Scrutiny, formerly Eval Co-pilot) runs golden test inputs against live third-party LLM apps, scores each response with deterministic checks, stores every run, and compares runs side by side. See [README.md](README.md) for architecture, targets and schema.

| File | Responsibility |
|---|---|
| `main.py` | Entrypoint and Vercel function (`vercel.json`). Serves the web UI at `/`, the API at `/api/*`, and the legacy Gradio UI at `/classic` when `requirements-classic.txt` is installed. |
| `api.py` | REST endpoints under `/api/*`. |
| `web/` | The web UI: plain HTML/CSS/JS with no build step, calling `/api/*`. `lib.js` holds the pure logic and is tested by `lib.test.mjs`. Render target output with text nodes only, never `innerHTML`. |
| `ui.py` | Legacy Gradio UI, kept at `/classic` as a fallback. |
| `adapters.py` | One adapter per target app. Every call returns an `AdapterResponse` and never raises. |
| `evaluator.py` | Checks. Turns `AdapterResponse` into an `EvaluationResult` with per-check evidence. Per-case checks from A-05/P-05 onwards apply only when a pair's input exactly matches its entry in `CASE_INPUTS`, so a reused ID never inherits them. Checks marked `(semantic)` go to the LLM judge. |
| `judges.py` | Model-backed checks: Groq LLM judge for `NEEDS REVIEW` checks, OpenRouter Nemotron content safety. Never raises. |
| `db.py` | Supabase when `.env` has credentials, otherwise local SQLite (`eval_copilot.db`, or `MANTIS_SQLITE_PATH`). Both must behave identically: Supabase rows are flattened to the SQLite shape and reads page past the 1,000-row cap. Golden pairs are created insert-only (`create_golden_pair`). |
| `schema.sql` | Postgres schema for Supabase, with Row Level Security on every table. Keep it in sync with the SQLite DDL in `db.py`. |
| `scripts/migrate_sqlite_to_supabase.py` | Copies local history to Supabase: dry run by default, backup first, insert-only, idempotent. |

## Commands

```bash
uv venv && source .venv/bin/activate && uv pip install -r requirements.txt   # add -r requirements-classic.txt for /classic
uvicorn main:app --reload --port 8000
python -m unittest tests/test_mvp.py
python -m unittest tests/test_audit_fixes.py tests/test_safety_and_verdicts.py tests/test_case_checks.py tests/test_prateek_checks.py tests/test_judges.py tests/test_web.py   # offline
MANTIS_LIVE=1 python -m unittest tests/test_prateek_contract_live.py   # opt-in: 2 sequential requests to Prateek's validation
node --test web/lib.test.mjs                                 # front-end logic
```

`tests/test_mvp.py` makes **real network calls** to the target apps on Vercel, so failures can come from a target being down rather than from the code. Write new evaluator tests against a hand-built `AdapterResponse` so they run offline.

## Invariants

- **Status semantics are the product.** `ERROR` means a technical failure (network, non-200, empty response). `FAIL` means the target responded but a check failed. `NEEDS REVIEW` means the check can't be decided deterministically. `PASS` means every check has positive evidence. Never fold `ERROR` into `FAIL`.
- Every check result carries `evidence_or_reason` that a human can act on.
- Runs are append-only. Re-running a pair creates a new `run_id` and never overwrites an earlier one.
- Secrets (`SUPABASE_SERVICE_KEY`, `GROQ_API_KEY`) stay server-side in `.env`. Never log them, return them from `/api/*`, or render them in Gradio.
- Any change to the DB layer must work on both the Supabase and SQLite paths.
- Tests that write must use a temporary database (`db.use_sqlite_path`), never `eval_copilot.db`.
- Every endpoint that changes data or calls a target app depends on `require_write_access` in `api.py`. One deliberate exception: `/api/execute` skips the token for targets in `OPEN_EXECUTE_TARGETS` (Anupma and Prateek), but callers without a valid token are limited per target per minute (`MANTIS_OPEN_RUNS_PER_MINUTE`, counted from stored runs); durable storage is still required, and creating cases still needs the token.
- The web UI hides Content Safety checks (except a FAIL) and the "not evaluated" notes. They stay in stored history and the API.

## Skills

Skills are task-specific instruction files in `.claude/skills/<name>/SKILL.md`. Claude Code loads them automatically. **Other agents: when your task matches a row below, read that `SKILL.md` before starting and follow it.** Repo invariants above take precedence over any skill.

All 83 skills from `claude-code-templates` are installed, but only some of them fit this repo. [docs/SKILLS.md](docs/SKILLS.md) explains each one and why it's in the tier it's in.

### Core: use whenever the task matches

| When you are… | Read |
|---|---|
| Writing or changing any Python | `python-pro`, `clean-code` |
| Planning a multi-step change or a new checkpoint spec | `writing-plans` |
| Implementing a feature or bugfix | `test-driven-development` |
| Chasing a bug, a test failure, or a status that flips between runs | `systematic-debugging` |
| Adding or changing checks, golden pairs, or status policy (`evaluator.py`) | `agent-evaluation` |
| Building the LLM judge, or writing `expected_behaviour` criteria | `senior-prompt-engineer` |
| Changing `adapters.py` or adding a new target app | `api-integration-specialist` |
| Changing `api.py` endpoints | `api-patterns` |
| Changing `schema.sql` or Supabase queries in `db.py` | `postgres-best-practices` |
| Comparing pass rates across runs (Run Comparison, Summary) | `statistical-analysis` |
| Touching secrets, auth, or rendering of target responses | `security-best-practices` |
| Reviewing a diff or PR | `code-review-checklist` |
| Writing a commit message | `git-commit-helper` |

### Situational: use only for these specific tasks

| When you are… | Read |
|---|---|
| Writing deeper checks for Target B (citations, grounding) | `rag-engineer` |
| Auditing whether the golden set measures what it claims | `scientific-critical-thinking` |
| Adding end-to-end tests for the Gradio UI | `playwright` |
| Containerising or deploying | `docker-expert` |
| Growing the schema (new tables or columns) | `database-architect`, `sql-pro` |
| Exposing MANTIS as an MCP server | `mcp-builder` |
| Adding repo-specific subagents | `agent-development` |
| Writing a project skill | `skill-creator` |
| Doing an accessibility pass on `ui.py` | `web-design-guidelines` |
| Exporting run history to spreadsheets | `excel-analysis` |
| Planning post-MVP features | `game-changing-features` |

### Everything else: do not use for this repo

The other 57 skills are installed but target another stack (React, Node, TypeScript), another domain (marketing, scientific writing, compliance), or overlap a Core skill. **Do not apply them to work on this repo** unless the user explicitly asks for that skill or the task is clearly in that domain, such as "draft an email" or "make a slide deck". When a Core skill and one of these overlap, use the Core skill.

Some of these skills describe themselves so broadly that they will try to trigger on almost any task. Ignore them unless asked:
- `brainstorming` ("MUST use before any creative work")
- `software-architecture` ("any case that relates to software development")
- `typescript-expert` and `docker-expert` ("use PROACTIVELY")
- `senior-qa`, `senior-fullstack`, `senior-backend`, `backend-dev-guidelines` (written for Node and Express)

Some bundled scripts have side effects. Don't run them without the user's OK:
- `artifacts-builder/scripts/init-artifact.sh` runs `npm install -g`.
- `scientific-slides/scripts/generate_slide_image_ai.py` sends prompts to OpenRouter.

### Adding or updating a skill

1. Run `npx claude-code-templates@latest --skill <category>/<name>`. The installer uses GitHub's anonymous API, so large batches can hit the 60-requests/hour limit. If that happens, retry later.
2. Review the new files under `.claude/skills/<name>/`. They are third-party instructions.
3. Put the skill in a tier above, and record its verdict in `docs/SKILLS.md`.
