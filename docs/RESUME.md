# IncidentPilot: resume and interview notes

All numbers below come from [evals/RESULTS.md](../evals/RESULTS.md) and
[evals/EXPERIMENTS.md](../evals/EXPERIMENTS.md). Re-run the evals before quoting them.

## Resume entry

**IncidentPilot: AI on-call agent for GCP** · github.com/DEKU-12/IncidentPilot
*Python, LangGraph, LangChain, MCP, Gemini (Vertex AI), GCP (Cloud Run, Pub/Sub, Cloud Logging, Secret Manager), Terraform, GitHub Actions*

- Built a LangGraph agent that investigates production incidents through a custom **MCP** server
  (logs, metrics, deploy history, runbooks, approval-gated rollback) and returns a cited root-cause
  report; **98% correct root cause and fix on 150 recorded incidents** with Gemini 3.8 Flash at
  **$0.048/incident**.
- Designed an **eval harness** with fault injection as ground truth, record/replay of incidents, strict
  scoring (service + category + exact fix), harmful-rollback and made-up-citation metrics, and an
  LLM-as-judge; a CI gate fails the build below 95% accuracy.
- Used the evals to find the agent blaming unrelated deploys for outside-provider outages
  (3% harmful rollbacks); a causal pre-rollback check cut **harmful rollbacks to 0%** with no regressions
  and 8% lower cost.
- Red-teamed with **40 prompt-injection incidents** (attacker text planted in production logs):
  **0% attack success** with the text fully visible to the model; layered defenses: injection guard,
  read-only agent, citation checks, LangGraph human-approval interrupt, single-use HMAC tokens.
- Deployed on **GCP with Terraform**: Cloud Run services, Cloud Monitoring alert → Pub/Sub → agent,
  Cloud Logging, Secret Manager, least-privilege service accounts; scales to zero.

## Questions to be ready for

1. **Why an LLM if the rule-based baseline scores 100%?** The rules were written for these five known
   faults. The LLM's value is faults the rules have never seen; the planned hard eval set (unseen fault
   types, two faults at once) measures exactly that.
2. **What does MCP give you over plain function calling?** One tool server used by the agent, Claude
   Code and any MCP client, over stdio locally and HTTP on Cloud Run, with no changes.
3. **How do you know the agent works? How do you know your eval is fair?** Fault injection gives
   ground truth. The eval caught its own bugs too: impossible cases, a category overlap, an
   attack the model never saw, a mis-scored rollback.
4. **Walk me through a prompt injection in a log line.** Guard quarantines text addressed to the
   agent; the model is told logs are untrusted; it has no write tool; a rollback needs a human yes and a
   signed one-time token.
5. **What happens if the agent is wrong and someone approves the rollback?** The report shows its
   evidence and rollback_evidence; rollbacks are reversible; harmful-rollback rate is tracked.
6. **What would change for a real 50-engineer company?** Real alert sources, Cloud SQL checkpointer,
   per-team runbooks with embeddings, an approval UI (Slack), and an eval set built from past incidents.
