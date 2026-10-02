# IncidentPilot

**An AI on-call engineer for GCP that finds the root cause and asks before it acts.**

When an alert fires, IncidentPilot investigates: it reads logs, metrics and recent deploys through
MCP servers, runs a LangGraph investigation loop on Gemini, and writes a root-cause report that cites
real log entries. Any fix, such as a rollback, waits for a human to approve it.

To measure how well it works, it investigates a demo shop that we break on purpose, so the true root
cause of every incident is known.

> **Status:** Phases 1–3 of 6 are done (the demo shop, the MCP server and the LangGraph agent). See [docs/ROADMAP.md](docs/ROADMAP.md).

---

## ShopDemo: the system under investigation

Three FastAPI services that call each other, the way a small Cloud Run app would:

```
shopper ──► frontend :8001 ──► orders :8002 ──► payments :8003 ──► fraud-check provider (external)
            /products           /orders          /charge
            /checkout           (DB pool)
```

- **Structured JSON logs** in Cloud Logging's format (`severity`, `message`, `timestamp`, plus
  `trace_id`, `revision`, `http`). One trace ID follows each request through all three services.
- **Metric points** every 5 seconds per service: request count, 5xx rate, p50/p95 latency, memory,
  restarts, DB pool usage.
- **Cloud Run-style revisions.** A deploy creates `orders-00002` and moves all traffic to it. A
  rollback moves traffic back.
- **Admin API** (`/admin/*`, needs `X-Admin-Token`) used by the chaos controller, and later by the
  remediation MCP server.

Locally, logs go to `var/logs/<service>.jsonl` and metrics to `var/metrics/<service>.jsonl`.

### The faults

| Fault | Service | How it arrives | What you see | Correct fix |
|---|---|---|---|---|
| `bad_deploy` | orders | new revision (code change) | bulk orders 500 with `TypeError: Decimal is not JSON serializable` | roll back orders |
| `config_error` | payments | new revision (env var removed) | every charge 500s with `KeyError: 'PAYMENT_GATEWAY_URL'` | roll back payments |
| `connection_exhaustion` | orders | new revision (`DB_POOL_SIZE 10 -> 1`) | intermittent `QueuePool limit of size 1 ... timed out` | roll back orders |
| `memory_leak` | frontend | new revision (`PRODUCT_CACHE -> unbounded`) | memory climbs, then `Memory limit exceeded` and an OOM restart | roll back frontend |
| `slow_dependency` | payments | outside provider slows down, **no deploy** | orders times out calling payments; payments warns about the fraud-check SLO | **none**: escalate, a rollback won't help |

Add `--noise` to any fault to turn on **red-herring warnings** in a different, healthy service. These
test whether the agent gets distracted by noise instead of finding the real cause.

Every injection appends the **ground truth** (true service, correct fix, red-herring service) to
`var/ground_truth.jsonl`. The agent never sees this file. The evals grade the agent against it.

---

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make test
```

Then, in three terminals:

```bash
incidentpilot up --fresh                         # 1. start the shop
incidentpilot traffic --rps 5                    # 2. fake shoppers
incidentpilot logs --severity WARNING -f         # 3. watch warnings and errors
```

Break something and watch the logs:

```bash
incidentpilot chaos list                         # the faults you can inject
incidentpilot chaos inject bad_deploy --noise    # break orders, add noise elsewhere
incidentpilot chaos status                       # what each service is serving
incidentpilot chaos history                      # recorded ground truth
incidentpilot chaos clear                        # back to healthy
```

Tune a fault with `--param`, for example `--param latency_s=5` for `slow_dependency` or
`--param mb_per_request=10` to make the memory leak faster.

---

## The MCP server

One server gives any MCP client (the agent in Phase 3, or Claude Code) eyes on ShopDemo and one
gated pair of hands:

| Tool | Kind | What it returns |
|---|---|---|
| `top_errors` | read | ERROR/CRITICAL messages grouped by shape, with counts, revisions and an example |
| `query_logs` | read | log entries filtered by service, severity, time window and text, each with a citable `id` |
| `get_metrics` | read | per-service metric points (error rate, p95, memory, restarts, DB pool) |
| `list_revisions` | read | deploy history: commit message, image, env var changes, which revision serves |
| `search_runbooks` | read | the best-matching runbooks from `runbooks/` (also exposed as `runbook://<name>` resources) |
| `rollback` | **write** | shifts traffic to an earlier revision, **only with a human approval token** |

Safety built in:
- **PII redaction:** emails and card numbers in log text are masked before they leave the server.
- **Approval tokens:** `rollback` needs an HMAC-signed token for that exact service and revision. It
  expires after 10 minutes and works once. Mint one with `incidentpilot approve orders orders-00001`.
- **Log text is labeled untrusted** in the server instructions and tool descriptions (full
  prompt-injection defenses come in Phase 5).

Run it:

```bash
incidentpilot mcp          # stdio, for Claude Code and the agent
incidentpilot mcp --http   # streamable HTTP on :8000
```

**Use it from Claude Code:** the repo's `.mcp.json` registers the server. Run `claude` in this
folder, approve the `incidentpilot` server when asked, break the shop, then ask
*"why is orders failing?"*.

---

## The agent

A LangGraph loop ([incidentpilot/agent.py](incidentpilot/agent.py)) that uses the MCP tools through
`langchain-mcp-adapters`:

```
investigate ⇄ tools  →  report  →  verify ─┐
     ▲                                     │ a cited log ID or revision isn't real
     └─────────────────────────────────────┘ (2 retries, then confidence capped at 0.3)
```

- **Output:** a Pydantic `RCAReport`: root-cause service, fault category, summary, cited log IDs,
  confidence, proposed action and rollback target.
- **Verify** is plain code, not another LLM call: every cited `id` must appear in a tool result,
  and a proposed rollback must target a real earlier revision.
- **Read-only:** the agent only gets the read tools. It proposes rollbacks; it can't run them.
- **Budget:** at most 15 tool calls, then it must report.
- **Models:** any LangChain `provider:model` string, plus `baseline`, a rule-based model that runs
  offline and is the bar the LLM has to beat in the evals.

```bash
incidentpilot investigate --model baseline                           # offline
incidentpilot investigate --model google_vertexai:gemini-2.5-flash   # Vertex AI (needs a GCP project)
```

For Vertex AI: `gcloud auth application-default login` and `export GOOGLE_CLOUD_PROJECT=<id>`.
Without a GCP project, a free AI Studio key works too: `pip install langchain-google-genai`,
`export GOOGLE_API_KEY=<key>`, then `--model google_genai:gemini-2.5-flash`.

**Tracing:** set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` to see every step, tool call and
token count in LangSmith. No code changes needed.

---

## Repo layout

```
incidentpilot/
  config.py            settings from environment variables
  chaos.py             fault injection and ground-truth recording
  traffic.py           traffic generator
  cli.py               `incidentpilot` command
  mcp_server.py        MCP tools: logs, metrics, revisions, runbooks, gated rollback
  approval.py          signed, expiring, single-use approval tokens
  agent.py             LangGraph agent, RCAReport schema, citation check
  baseline.py          offline rule-based model
  redact.py            PII redaction
  shopdemo/
    base.py            revisions, admin API, telemetry middleware, upstream calls
    frontend.py        /products, /checkout
    orders.py          /orders (DB pool simulation)
    payments.py        /charge (fraud-check provider)
    faults.py          fault catalogue and ground truth
    serializers.py     the buggy refactor behind bad_deploy
    telemetry.py       JSON logger and metrics
    inprocess.py       all three services in one process (tests, evals)
runbooks/              on-call runbooks the agent can search
tests/                 pytest suite
docs/ROADMAP.md        the six build phases
```
