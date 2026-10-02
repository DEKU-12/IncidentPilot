# Roadmap

Each phase ends with something that works on its own.

## Phase 1: The app we'll break ✅
ShopDemo (frontend, orders, payments) with structured JSON logs, metrics and Cloud Run-style
revisions. Five injectable faults plus red-herring noise. Ground truth recorded for every injection.
CLI: `up`, `traffic`, `chaos`, `logs`. Test suite.

**Done when:** one command starts the shop, a fault can be turned on, and errors show up in the logs.

## Phase 2: MCP server ✅
Built as one server (split per IAM role in Phase 6 if needed). Keyword runbook search, regex PII redaction.

- `observability`: `query_logs`, `get_metrics`, `list_revisions`, `top_errors` (local files now, Cloud Logging later)
- `runbooks`: runbooks as MCP resources, plus a `search_runbooks` tool (RAG)
- `remediation`: `rollback`, refused without a signed approval token (`scale` skipped: no fault needs it)
- PII redaction inside the observability server
- Connect the servers to Claude Code

**Done when:** Claude Code answers "why is orders failing?" using these MCP tools.

## Phase 3: The LangGraph agent ✅
Built lean: investigate ⇄ tools → report → verify (no separate triage or plan nodes until evals show they help). Verification is a code check of citations. Offline `baseline` model included.

LangChain for models, prompts, structured `RCAReport` output and MCP tool loading. Gemini on Vertex AI.
In-memory checkpointer (Postgres in Phase 6), LangSmith tracing via env vars, token tracking.

**Done when:** the agent names the right service and cites real log lines.

## Phase 4: Evals ✅
Built lean: a case folder *is* the recording, replayed by the normal MCP server with a frozen clock (no separate recorder or replay server). Decoy deploys added to make cases harder. `--min-accuracy` gives the CI gate.

150 recorded incidents. Metrics: root-cause accuracy, evidence
grounding, hallucinated citations, cost, steps and fix correctness. LLM-as-judge, checked against
human grades. Model and prompt experiments, kept in a results table.

**Done when:** `make eval` prints a results table.

## Phase 5: Guardrails and human-in-the-loop
Untrusted-data handling, an injection detector, read and write tools kept separate, a LangGraph
`interrupt` for approval, budgets, citation checks, and a red-team suite of 40 attacks.

**Done when:** the injection demo fails safely and evals show 0 unsafe actions.

## Phase 6: GCP, CI/CD and resume packaging
Terraform (Cloud Run, Pub/Sub, Cloud SQL + pgvector, Secret Manager, IAM, a budget alert), real Cloud
Logging and Vertex AI, Cloud Build, GitHub Actions with an eval check, docs, a demo video and resume
bullets.

**Done when:** a real GCP alert triggers the agent automatically.
