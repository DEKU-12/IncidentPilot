"""Traffic generator: steady fake shoppers hitting the frontend."""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from collections.abc import Callable

import httpx

from incidentpilot.config import Settings

# Fake customers. The card numbers are public test numbers, never real ones.
USERS = [
    ("alice@example.com", "4242 4242 4242 4242"),
    ("bob@example.com", "5555 5555 5555 4444"),
    ("chen@example.com", "4000 0566 5566 5556"),
    ("dana@example.com", "3782 822463 10005"),
    ("eli@example.com", "6011 1111 1111 1117"),
    ("fatima@example.com", "4242 4242 4242 4242"),
]
SKUS = ["MUG-001", "TEE-002", "BAG-003", "CAP-004"]


async def _one_request(client: httpx.AsyncClient, rng: random.Random, counts: Counter) -> None:
    try:
        if rng.random() < 0.7:
            email, card = rng.choice(USERS)
            body = {"email": email, "card": card, "sku": rng.choice(SKUS), "quantity": rng.randint(1, 3)}
            resp = await client.post("/checkout", json=body)
        else:
            resp = await client.get("/products")
        counts[resp.status_code] += 1
    except httpx.HTTPError as exc:
        counts[type(exc).__name__] += 1


async def run_traffic(
    settings: Settings,
    *,
    rps: float = 5.0,
    duration_s: float = 60.0,
    seed: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    report: Callable[[Counter], None] | None = None,
    report_every_s: float = 5.0,
) -> Counter:
    """Send about ``rps`` requests per second for ``duration_s`` seconds (0 = forever)."""
    rng = random.Random(seed)
    counts: Counter = Counter()
    interval = 1.0 / rps
    pending: set[asyncio.Task] = set()
    async with httpx.AsyncClient(
        transport=transport, base_url=settings.urls["frontend"], timeout=10.0
    ) as client:
        start = last_report = time.monotonic()
        while duration_s <= 0 or time.monotonic() - start < duration_s:
            task = asyncio.create_task(_one_request(client, rng, counts))
            pending.add(task)
            task.add_done_callback(pending.discard)
            await asyncio.sleep(rng.expovariate(1.0 / interval))
            if report and time.monotonic() - last_report >= report_every_s:
                report(counts)
                last_report = time.monotonic()
        if pending:
            await asyncio.gather(*pending)
    if report:
        report(counts)
    return counts
