# Incident triage (start here)

1. Find the loudest errors: which service, which message, since when.
2. Follow the request path: frontend -> orders -> payments -> fraud-check provider.
   The service that shows the error is often not the one that caused it. A timeout
   in a caller usually means the callee, or something the callee depends on, is slow.
3. Check deploy history for every service on the path. An error rate that jumps
   right after a new revision went live points at that revision.
4. Separate signal from noise: warnings that were present before the incident, or
   that don't line up with the failing requests, are not the cause.
5. Cite evidence (log IDs, metric points, revisions) for every claim.
6. Propose the smallest safe fix. Never roll back without human approval.
