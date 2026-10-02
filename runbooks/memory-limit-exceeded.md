# Memory limit exceeded / OOM restarts

Symptoms: "Memory limit of N MiB exceeded", "OOMKilled", rising memory_mb that drops
back after each restart (a sawtooth), brief 503s during restarts.

Check:
- get_metrics: memory_mb trend and restarts counter.
- list_revisions: a recent change to caching or buffering (look at env_changes and
  commit messages).

Fix: if a new revision introduced the growth, roll it back (needs human approval).
Raising the memory limit only delays the next crash.
