# Database connection pool exhaustion

Symptoms: intermittent errors like "QueuePool limit of size N overflow 0 reached,
connection timed out". Worse under load. Metrics: db_pool_in_use pinned at
db_pool_size.

Check:
- get_metrics for orders: db_pool_size and db_pool_in_use.
- list_revisions: did a recent revision change DB_POOL_SIZE or other DB settings?
- Slow queries holding connections longer than usual.

Fix: if a config change shrank the pool, roll back that revision (needs human
approval). If the pool size is unchanged, look for slow queries or a traffic surge.
