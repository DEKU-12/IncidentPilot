# IncidentPilot

**An AI on-call engineer for GCP that finds the root cause and asks before it acts.**

When an alert fires, IncidentPilot investigates: it reads logs, metrics and recent deploys through
MCP servers, runs a LangGraph investigation loop on Gemini, and writes a root-cause report that cites
real log entries. Any fix, such as a rollback, waits for a human to approve it.

To measure how well it works, it investigates a demo shop that we break on purpose, so the true root
cause of every incident is known.

> **Status:** Phase 1 of 6 is done (the demo shop and the chaos tooling). See [docs/ROADMAP.md](docs/ROADMAP.md).

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

## Repo layout

```
incidentpilot/
  config.py            settings from environment variables
  chaos.py             fault injection and ground-truth recording
  traffic.py           traffic generator
  cli.py               `incidentpilot` command
  shopdemo/
    base.py            revisions, admin API, telemetry middleware, upstream calls
    frontend.py        /products, /checkout
    orders.py          /orders (DB pool simulation)
    payments.py        /charge (fraud-check provider)
    faults.py          fault catalogue and ground truth
    serializers.py     the buggy refactor behind bad_deploy
    telemetry.py       JSON logger and metrics
    inprocess.py       all three services in one process (tests, evals)
tests/                 pytest suite
docs/ROADMAP.md        the six build phases
```
