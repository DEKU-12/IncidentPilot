"""The faults ShopDemo can be broken with, and the ground truth for each.

Four faults arrive as a new revision (a bad code change or a bad config change),
so rolling back fixes them. ``slow_dependency`` is an outside provider getting
slow: no deploy caused it, so the correct action is to escalate, not roll back.
An agent that rolls back anyway gets that eval case wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FaultKind(str, Enum):
    BAD_DEPLOY = "bad_deploy"
    CONFIG_ERROR = "config_error"
    CONNECTION_EXHAUSTION = "connection_exhaustion"
    MEMORY_LEAK = "memory_leak"
    SLOW_DEPENDENCY = "slow_dependency"


@dataclass(frozen=True)
class FaultSpec:
    kind: FaultKind
    service: str
    summary: str
    fix: str  # "rollback" or "none"
    via_revision: bool
    commit: str | None = None
    # New env values for the bad revision. None means the variable is removed.
    env_changes: dict[str, str | None] = field(default_factory=dict)
    default_params: dict[str, float] = field(default_factory=dict)


CATALOG: dict[FaultKind, FaultSpec] = {
    spec.kind: spec
    for spec in (
        FaultSpec(
            kind=FaultKind.BAD_DEPLOY,
            service="orders",
            summary="New orders revision ships a serializer bug: bulk orders fail with "
            "'Decimal is not JSON serializable'.",
            fix="rollback",
            via_revision=True,
            commit="9f3c2ab refactor(orders): keep totals as Decimal, add bulk discount line",
        ),
        FaultSpec(
            kind=FaultKind.CONFIG_ERROR,
            service="payments",
            summary="New payments revision dropped the PAYMENT_GATEWAY_URL env var, so "
            "every charge raises KeyError.",
            fix="rollback",
            via_revision=True,
            commit="41d07e9 chore(payments): move gateway settings to Secret Manager",
            env_changes={"PAYMENT_GATEWAY_URL": None},
        ),
        FaultSpec(
            kind=FaultKind.CONNECTION_EXHAUSTION,
            service="orders",
            summary="New orders revision set DB_POOL_SIZE=1, so requests time out waiting "
            "for a database connection.",
            fix="rollback",
            via_revision=True,
            commit="c81e5d0 perf(orders): tune database settings for smaller instances",
            env_changes={"DB_POOL_SIZE": "1"},
            default_params={"query_s": 0.4, "acquire_timeout_s": 1.0},
        ),
        FaultSpec(
            kind=FaultKind.MEMORY_LEAK,
            service="frontend",
            summary="New frontend revision made the product cache unbounded; memory grows "
            "until the container is OOM-killed.",
            fix="rollback",
            via_revision=True,
            commit="5b6a1f4 perf(frontend): cache product catalogue responses",
            env_changes={"PRODUCT_CACHE": "unbounded"},
            default_params={"mb_per_request": 3.0},
        ),
        FaultSpec(
            kind=FaultKind.SLOW_DEPENDENCY,
            service="payments",
            summary="The external fraud-check provider called by payments got slow; "
            "orders times out waiting for payments. No deploy is involved.",
            fix="none",
            via_revision=False,
            default_params={"latency_s": 3.0},
        ),
    )
}
