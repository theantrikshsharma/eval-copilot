# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, Copilot, Gemini, and others) working in this repo.

## Project

Eval Co-pilot runs golden test inputs against live third-party LLM apps, scores each response with deterministic checks, stores every run, and compares runs side by side. See [README.md](README.md) for architecture, targets and schema.

| File | Responsibility |
|---|---|
| `main.py` | Entrypoint. Mounts the Gradio UI onto FastAPI at `/`. |
| `api.py` | REST endpoints under `/api/*`. |
| `ui.py` | Gradio UI (Test Cases, Failure Inspector, Run Comparison, Summary). |
| `adapters.py` | One adapter per target app. Every call returns an `AdapterResponse` and never raises. |
| `evaluator.py` | Checks. Turns `AdapterResponse` into an `EvaluationResult` with per-check evidence. |
| `db.py` | Supabase when `.env` has credentials, otherwise local SQLite (`eval_copilot.db`). Both must behave identically. |
| `schema.sql` | Postgres schema for Supabase. Keep it in sync with the SQLite DDL in `db.py`. |

## Commands

```bash
uv venv && source .venv/bin/activate && uv pip install -r requirements.txt
uvicorn main:app --reload --port 8000
python -m unittest tests/test_mvp.py
```

`tests/test_mvp.py` makes **real network calls** to the target apps on Vercel, so failures can come from a target being down rather than from the code. Write new evaluator tests against a hand-built `AdapterResponse` so they run offline.

## Invariants

- **Status semantics are the product.** `ERROR` means a technical failure (network, non-200, empty response). `FAIL` means the target responded but a check failed. `NEEDS REVIEW` means the check can't be decided deterministically. `PASS` means every check has positive evidence. Never fold `ERROR` into `FAIL`.
- Every check result carries `evidence_or_reason` that a human can act on.
- Runs are append-only. Re-running a pair creates a new `run_id` and never overwrites an earlier one.
- Secrets (`SUPABASE_SERVICE_KEY`, `GROQ_API_KEY`) stay server-side in `.env`. Never log them, return them from `/api/*`, or render them in Gradio.
- Any change to the DB layer must work on both the Supabase and SQLite paths.

## Skills

Skills are task-specific instruction files in `.claude/skills/<name>/SKILL.md`. Claude Code loads them automatically. **Other agents: when your task matches a row below, read that `SKILL.md` before starting and follow it.** Repo invariants above take precedence over any skill.

If `.claude/skills/` is missing, install the set with the command in [docs/SKILLS.md](docs/SKILLS.md#install-the-recommended-set). That file also explains every skill, including the ones deliberately left out.

### Installed (Core)

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

### Situational (install first, then add a row above)

`rag-engineer` (deeper Target B checks), `playwright` (Gradio E2E tests), `docker-expert` (containerising), `database-architect` and `sql-pro` (schema growth), `mcp-builder` (exposing evals as an MCP server), `agent-development` (repo subagents), `scientific-critical-thinking` (auditing the golden set), `web-design-guidelines` (UI accessibility pass).

### Adding a skill

1. Run `npx claude-code-templates@latest --skill <category>/<name>`.
2. Review the new files under `.claude/skills/<name>/`. They are third-party instructions.
3. Add a row to the table above, and update its verdict in `docs/SKILLS.md`.
