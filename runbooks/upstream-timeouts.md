# Upstream timeouts and slow dependencies

Symptoms: "timed out after Ns" errors in a caller; latency (p95) jumps in the
callee; warnings about an external provider breaching its SLO.

Check:
- Which hop timed out? Follow the trace_id from frontend down the path.
- Did the callee have a deploy around the start? If not, a rollback will not help.
- Warnings naming an external dependency (fraud-check provider, payment gateway).

Fix: if an outside provider is slow, do not roll back. Escalate to the provider,
enable a fallback if one exists, and tell stakeholders. If a deploy caused it,
roll back (needs human approval).
