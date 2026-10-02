# Missing or wrong configuration

Symptoms: every request to one endpoint fails right after a revision change, often
with KeyError or "not set" errors naming an environment variable. Startup logs may
warn about missing keys.

Check:
- list_revisions: the env_changes of the serving revision. Was a variable removed
  or changed?
- Startup log lines of the new revision.

Fix: roll back to the revision that still had the variable, then restore the
setting properly before redeploying. Needs human approval.
