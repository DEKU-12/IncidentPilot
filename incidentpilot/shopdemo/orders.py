"""orders: stores an order in the database, then charges it through payments.

Run: uvicorn incidentpilot.shopdemo.orders:create_app --factory --port 8002
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from incidentpilot.config import Settings
from incidentpilot.shopdemo.base import UpstreamError, UpstreamTimeout, create_service_app, trace_id_of
from incidentpilot.shopdemo.faults import FaultKind
from incidentpilot.shopdemo.serializers import serialize_order_v1, serialize_order_v2

PRICES = {
    "MUG-001": Decimal("12.50"),
    "TEE-002": Decimal("24.00"),
    "BAG-003": Decimal("18.00"),
    "CAP-004": Decimal("21.00"),
}


class OrderRequest(BaseModel):
    email: str
    sku: str
    quantity: int = Field(1, ge=1, le=10)
    card: str


class PoolTimeout(Exception):
    def __init__(self, size: int, timeout: float) -> None:
        super().__init__(f"pool of size {size} exhausted after {timeout}s")
        self.size = size
        self.timeout = timeout


class ConnectionPool:
    """Stands in for a database connection pool."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.in_use = 0
        self._sem = asyncio.Semaphore(size)

    @asynccontextmanager
    async def connection(self, timeout: float) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout)
        except TimeoutError as exc:
            raise PoolTimeout(self.size, timeout) from exc
        self.in_use += 1
        try:
            yield
        finally:
            self.in_use -= 1
            self._sem.release()


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    app, ctx = create_service_app("orders", settings, transport)
    pools: dict[int, ConnectionPool] = {}

    def pool() -> ConnectionPool:
        size = int(ctx.revision.env["DB_POOL_SIZE"])
        return pools.setdefault(size, ConnectionPool(size))

    def log_pool_config() -> None:
        ctx.logger.info(
            f"Database pool initialised: size={pool().size} max_overflow=0 "
            f"host={ctx.revision.env['DB_HOST']}"
        )

    ctx.startup_hooks.append(log_pool_config)
    ctx.extra_gauges = lambda: {"db_pool_size": pool().size, "db_pool_in_use": pool().in_use}

    @app.post("/orders")
    async def create_order(body: OrderRequest, request: Request):
        trace_id = trace_id_of(request)
        order_id = f"ord_{secrets.token_hex(5)}"
        params = ctx.params(FaultKind.CONNECTION_EXHAUSTION)
        slow_queries = ctx.revision.fault == FaultKind.CONNECTION_EXHAUSTION
        query_s = params["query_s"] if slow_queries else ctx.rng.uniform(0.01, 0.03)
        acquire_timeout = params["acquire_timeout_s"]

        db = pool()
        try:
            async with db.connection(acquire_timeout):
                await asyncio.sleep(query_s)  # INSERT INTO orders ...
        except PoolTimeout as exc:
            ctx.logger.error(
                f"sqlalchemy.exc.TimeoutError: QueuePool limit of size {exc.size} overflow 0 "
                f"reached, connection timed out, timeout {exc.timeout:.2f}",
                trace_id=trace_id,
                db_host=ctx.revision.env["DB_HOST"],
            )
            return JSONResponse({"error": "database unavailable"}, status_code=503)

        total = PRICES.get(body.sku, Decimal("10.00")) * body.quantity
        try:
            resp = await ctx.call(
                "payments",
                "POST",
                "/charge",
                trace_id=trace_id,
                timeout=ctx.settings.upstream_timeout_s,
                json={"order_id": order_id, "email": body.email, "amount": str(total), "card": body.card},
            )
        except UpstreamTimeout as exc:
            ctx.logger.error(
                f"Payment for {order_id} failed: payments POST /charge {exc.detail}",
                trace_id=trace_id,
                upstream="payments",
            )
            return JSONResponse({"error": "payment timeout"}, status_code=504)
        except UpstreamError as exc:
            ctx.logger.error(
                f"Payment for {order_id} failed: payments unreachable ({exc.detail})",
                trace_id=trace_id,
                upstream="payments",
            )
            return JSONResponse({"error": "payment unavailable"}, status_code=502)

        if resp.status_code == 402:
            ctx.logger.info(f"Order {order_id} rejected: payment declined", trace_id=trace_id)
            return JSONResponse(resp.json(), status_code=402)
        if resp.status_code >= 400:
            ctx.logger.error(
                f"Payment for {order_id} failed: payments returned HTTP {resp.status_code}",
                trace_id=trace_id,
                upstream="payments",
                upstream_status=resp.status_code,
            )
            return JSONResponse({"error": "payment failed"}, status_code=502)

        order = {
            "order_id": order_id,
            "sku": body.sku,
            "quantity": body.quantity,
            "total": total,
            "charge_id": resp.json()["charge_id"],
        }
        if ctx.revision.fault == FaultKind.BAD_DEPLOY:
            return serialize_order_v2(order)
        return serialize_order_v1(order)

    return app
