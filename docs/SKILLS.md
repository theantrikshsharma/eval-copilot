# Agent Skills Guide

This guide covers the 83 skills offered by `npx claude-code-templates` (third-party, from [davila7/claude-code-templates](https://github.com/davila7/claude-code-templates)). For each skill it says what the skill does and whether it helps Eval Co-pilot. The descriptions are summarised from each skill's own `SKILL.md` as of 2026-10-19.

The installer writes every skill to `.claude/skills/<name>/SKILL.md` in this repo. Claude Code loads skills from there automatically. Other agents (Codex, Cursor, Copilot, Gemini) read them through the routing table in [`AGENTS.md`](../AGENTS.md).

**Verdicts**
- **Core**: install now. It's relevant to day-to-day work on this repo.
- **Situational**: install when the matching task comes up.
- **Skip**: wrong stack, wrong domain, a duplicate of another skill, or already built into Claude Code.

Context for the verdicts: this repo is roughly 2k lines of Python (FastAPI + Gradio + Supabase/SQLite). It calls two external LLM apps over HTTP and scores their responses with deterministic checks. There is no React, Node or TypeScript code.

---

## 1. Core (14)

| Skill | What it does | How to use it here |
|---|---|---|
| `agent-evaluation` | Testing and benchmarking LLM agents: behavioural tests, capability assessment, reliability metrics, production monitoring. | The closest match to what this product does. Use it when designing new golden pairs, new check types in `evaluator.py`, or the `NEEDS REVIEW` vs `FAIL` policy. |
| `senior-prompt-engineer` | Prompt design, structured outputs, few-shot and CoT, AI evaluation. | Use it for the planned semantic LLM judge (`GROQ_API_KEY` in `.env.example`) and for writing `expected_behaviour` criteria that a judge can score reliably. |
| `api-integration-specialist` | Robust third-party API calls: auth, retries, rate limits, timeouts, response transformation. | Use it for `adapters.py`: retry/backoff for flaky Vercel targets, consistent `AdapterResponse` errors, and adding Target C. |
| `api-patterns` | API design decisions: response formats, versioning, pagination. | Use it for `api.py` endpoints, for example paginating `/api/results` as run history grows. |
| `python-pro` | Modern Python 3.12+, async, pydantic, FastAPI, uv, ruff. | The default style guide for every `.py` change. Covers the pydantic models and FastAPI routes. |
| `test-driven-development` | Write the failing test first, then the implementation. | Every new check in `evaluator.py` should get a test with a canned `AdapterResponse`, so it runs without network access. |
| `systematic-debugging` | Root-cause investigation before proposing a fix. | Use it when a target starts returning `ERROR` or a check flips status between runs. |
| `postgres-best-practices` | Supabase's own Postgres guidance on queries, schema and config. | Use it for `schema.sql`, indexes on `run_id`/`pair_id`, and Supabase query patterns in `db.py`. |
| `security-best-practices` | Security review for Python and JS code. It triggers only when asked. | Use it to keep `SUPABASE_SERVICE_KEY` server-side and to review inputs stored from target responses before Gradio renders them. |
| `writing-plans` | Turns a spec into a step-by-step plan before any code is written. | Use it for each new checkpoint spec (for example "Checkpoint 5"). |
| `git-commit-helper` | Writes commit messages from the diff. | Use it for every commit. |
| `code-review-checklist` | A checklist for reviews: functionality, security, performance, maintainability. | Use it to review PRs before merging. |
| `clean-code` | Short, pragmatic standards: no over-engineering, no filler comments. | Keeps `evaluator.py` from growing more per-pair `if/elif` branches without a reason. |
| `statistical-analysis` | Hypothesis tests, confidence intervals, power analysis. | Use it in Run Comparison to say whether a pass-rate change between two runs is real or noise. Live LLM targets are nondeterministic. |

## 2. Situational (12)

| Skill | What it does | When to install it for this repo |
|---|---|---|
| `rag-engineer` | RAG design: embeddings, chunking, retrieval quality. | When writing deeper checks for Target B (Prateek's retriever), such as citation faithfulness or source grounding. |
| `scientific-critical-thinking` | Judges how rigorous a method is: bias, confounding, quality of evidence. | When auditing whether the golden set actually measures what you claim it does. |
| `playwright` | Drives a real browser from the terminal (`playwright-cli`). | When adding end-to-end tests for the Gradio UI's four tabs. |
| `docker-expert` | Dockerfiles, multi-stage builds, Compose, hardening. Marked "use PROACTIVELY", so it triggers often. | When you containerise for deployment. |
| `database-architect` | Schema modelling and technology selection. | When the 3-table schema needs to grow, for example to add runs metadata, tags or judge scores. |
| `sql-pro` | Advanced SQL and OLTP/OLAP queries. | When summary and comparison queries move from Python into SQL. |
| `mcp-builder` | Building MCP servers with Python FastMCP or the TypeScript SDK. | If you expose Eval Co-pilot as an MCP server, so coding agents can run evals on their own changes. This is a strong future feature. |
| `agent-development` | Writing Claude Code subagents: frontmatter, triggers, tool lists. | If you add repo-specific subagents, such as an "eval-runner" agent. |
| `skill-creator` | Creating and evaluating skills. Duplicates Claude Code's built-in skill-creator. | If you write a project skill such as "add-golden-pair". |
| `web-design-guidelines` | Reviews UI code against web interface guidelines and accessibility. | For an accessibility pass on `ui.py`. Gradio limits how much you can change. |
| `excel-analysis` | Analysis, pivots and charts on `.xlsx` and tabular data. | If you export run history for stakeholders. |
| `game-changing-features` | Product strategy: finding "10x" opportunities. | When planning what comes after the MVP. |

## 3. Skip (57)

### Wrong stack: React, Node or TypeScript-specific (14)
| Skill | What it does |
|---|---|
| `senior-frontend` | React/Next.js/Tailwind scaffolding and bundle optimisation. |
| `frontend-dev-guidelines` | React + MUI v7 + TanStack Router conventions. |
| `typescript-expert` | Type-level TS, monorepos, TS tooling. Triggers proactively. |
| `nodejs-best-practices` | Node framework and async decisions. |
| `backend-dev-guidelines` | Express + Prisma + Zod microservice conventions. |
| `senior-fullstack` | React/Next/Node/GraphQL project scaffolding. |
| `senior-qa` | Test strategy for React/Next/Node apps. `test-driven-development` covers this repo better. |
| `artifacts-builder` | Multi-component claude.ai HTML artifacts built with React and shadcn. |
| `ui-design-system` | Design tokens and component docs for a custom design system. |
| `ui-ux-pro-max` | A large style, palette and font database for React/Vue/Svelte/Flutter UIs. |
| `frontend-design` | Distinctive visual design for custom UIs. Gradio's theming leaves little room for it. |
| `scroll-experience` | Parallax and scroll-driven storytelling sites. |
| `performance` | Page-speed audits for websites. |
| `web-performance-optimization` | Core Web Vitals, bundle size, caching. |

### Wrong domain (18)
| Skill | What it does |
|---|---|
| `lead-research-assistant` | Finds sales leads. |
| `competitor-alternatives` | SEO "X vs Y" comparison pages. |
| `schema-markup` | JSON-LD and schema.org structured data for SEO. |
| `analytics-tracking` | GA4, GTM and conversion tracking. |
| `market-research-reports` | 50+ page consulting-style reports in LaTeX. |
| `email-composer` | Drafts professional emails. |
| `scientific-writing` | IMRAD manuscripts with citations. |
| `scientific-slides` | Research talk decks. |
| `scientific-brainstorming` | Research hypothesis generation. |
| `security-compliance` | SOC2, ISO27001 and HIPAA programmes. |
| `senior-security` | Pentesting and security architecture. Too heavy for this repo; `security-best-practices` covers it. |
| `senior-data-engineer` | Spark, Airflow, dbt and Kafka pipelines. |
| `senior-data-scientist` | ML modelling, A/B tests, causal inference. `statistical-analysis` covers what this repo needs. |
| `data-scientist` | General ML and BI analytics. Same overlap as above. |
| `workflow-automation` | n8n, Temporal and Inngest durable workflows. |
| `file-organizer` | Cleans up folders and duplicates. The repo has 12 files. |
| `ux-researcher-designer` | Personas, journey maps, usability-test frameworks. Only useful if you run formal UX research on the tool's users. |
| `agent-management` | Lifecycle commands for the separate **AI Maestro** CLI. Useless without that tool. |

### For building LLM agents, which this repo doesn't do (7)
| Skill | What it does |
|---|---|
| `autonomous-agents` | Agent loops, planning and self-correction. |
| `langgraph` | The LangGraph stateful agent framework. |
| `agent-memory-systems` | Short- and long-term agent memory architectures. |
| `conversation-memory` | Persistent chat memory. |
| `context-window-management` | Summarising and trimming long contexts. |
| `agent-tool-builder` | Tool schema design for agents. Revisit if you build the MCP server. |
| `mcp-integration` | Configuring MCP inside Claude Code *plugins* (`.mcp.json`). |

### Overlaps a Core or Situational pick (14)
| Skill | What it does | Overlaps |
|---|---|---|
| `python-patterns` | Python decision-making: project structure, async choices. | `python-pro` |
| `error-resolver` | Diagnoses errors from first principles and keeps a replay log. | `systematic-debugging` |
| `code-reviewer` | Multi-language review with scripts. | `code-review-checklist` |
| `code-review` (sentry) | Review following Sentry's engineering practices. | `code-review-checklist` |
| `best-practices` | Web security and modernisation audits. | `security-best-practices` |
| `api-security-best-practices` | API auth, validation and rate limiting. | `security-best-practices` + `api-patterns` |
| `backend-architect` | Microservices and distributed systems. Overkill here. | `api-patterns` |
| `senior-backend` | Node/Go/Python backends, mostly Node-flavoured. | `python-pro` |
| `senior-architect` | Architecture across many stacks, with diagram generation. | `writing-plans` |
| `software-architecture` | Says it applies "in any case that relates to software development", so it triggers constantly. | `clean-code` |
| `database-optimizer` | General query tuning. | `postgres-best-practices` |
| `postgresql-optimization` | Postgres tuning workflow. | `postgres-best-practices` |
| `planning` | Persistent markdown plan files (from AI Maestro). | `writing-plans` |
| `browser-automation` | Playwright and Puppeteer patterns, including anti-detection for scraping. | `playwright` |

### Built into Claude Code, duplicated, or overly aggressive (4)
| Skill | What it does |
|---|---|
| `pdf` | PDF read, merge and create. Claude Code already has this built in. |
| `pdf-processing` | Same description as `pdf`. |
| `pptx` | PowerPoint files. Claude Code already has this built in. |
| `brainstorming` | Says "You MUST use this before any creative work", so it would hijack nearly every task. `writing-plans` covers the useful part. |

---

## Install the recommended set

The Core set:

```bash
npx claude-code-templates@latest --skill ai-research/agent-evaluation,development/senior-prompt-engineer,development/api-integration-specialist,development/api-patterns,development/python-pro,development/test-driven-development,development/systematic-debugging,development/postgres-best-practices,security/security-best-practices,development/writing-plans,development/git-commit-helper,development/code-review-checklist,development/clean-code,scientific/statistical-analysis
```

To add a Situational skill later, run the same command with just that skill, for example `--skill development/playwright`. Then add a row for it to the routing table in `AGENTS.md`.

**Review after installing.** These skills are third-party instructions that agents will follow. Read the diff under `.claude/skills/` before committing it, the same way you'd review a new dependency.
