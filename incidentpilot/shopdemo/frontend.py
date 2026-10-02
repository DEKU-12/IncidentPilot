"""frontend: product listing and checkout. Calls orders.

Run: uvicorn incidentpilot.shopdemo.frontend:create_app --factory --port 8001
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from incidentpilot.config import Settings
from incidentpilot.shopdemo.base import (
    BASE_MEMORY_MB,
    UpstreamError,
    UpstreamTimeout,
    create_service_app,
    trace_id_of,
)
from incidentpilot.shopdemo.faults import FaultKind

PRODUCTS = [
    {"sku": "MUG-001", "name": "Coffee mug", "price": "12.50"},
    {"sku": "TEE-002", "name": "Logo t-shirt", "price": "24.00"},
    {"sku": "BAG-003", "name": "Canvas tote", "price": "18.00"},
    {"sku": "CAP-004", "name": "Baseball cap", "price": "21.00"},
]


class CheckoutRequest(BaseModel):
    email: str
    sku: str
    quantity: int = Field(1, ge=1, le=10)
    card: str


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    app, ctx = create_service_app("frontend", settings, transport)
    limit = ctx.settings.memory_limit_mb

    def log_cache_config() -> None:
        ctx.logger.info(f"Product cache configured: {ctx.revision.env['PRODUCT_CACHE']}")

    ctx.startup_hooks.append(log_cache_config)

    @app.get("/products")
    async def products(request: Request):
        trace_id = trace_id_of(request)
        if ctx.revision.env.get("PRODUCT_CACHE") == "unbounded":
            ctx.memory_mb += ctx.params(FaultKind.MEMORY_LEAK)["mb_per_request"]
            if ctx.memory_mb >= 0.85 * limit and not ctx.flags.get("memory_warned"):
                ctx.flags["memory_warned"] = True
                ctx.logger.warning(
                    f"Memory usage {ctx.memory_mb:.0f} MiB is {ctx.memory_mb / limit:.0%} of the "
                    f"{limit:.0f} MiB limit",
                    trace_id=trace_id,
                )
            if ctx.memory_mb > limit:
                ctx.logger.critical(
                    f"Memory limit of {limit:.0f} MiB exceeded with {ctx.memory_mb:.0f} MiB used. "
                    "Consider increasing the memory limit, see "
                    "https://cloud.google.com/run/docs/configuring/memory-limits",
                    trace_id=trace_id,
                )
                ctx.logger.error(
                    "Container instance terminated (exit code 137, OOMKilled); starting a new instance",
                    trace_id=trace_id,
                )
                ctx.memory_mb = BASE_MEMORY_MB["frontend"]
                ctx.restarts += 1
                ctx.flags["memory_warned"] = False
                return JSONResponse({"error": "service unavailable"}, status_code=503)
        return {"products": PRODUCTS}

    @app.post("/checkout")
    async def checkout(body: CheckoutRequest, request: Request):
        trace_id = trace_id_of(request)
        ctx.logger.info(
            f"Checkout started for {body.email}: {body.quantity} x {body.sku}", trace_id=trace_id
        )
        try:
            resp = await ctx.call(
                "orders",
                "POST",
                "/orders",
                trace_id=trace_id,
                timeout=ctx.settings.upstream_timeout_s * 1.5,
                json=body.model_dump(),
            )
        except UpstreamTimeout as exc:
            ctx.logger.error(
                f"Checkout failed: orders POST /orders {exc.detail}", trace_id=trace_id, upstream="orders"
            )
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        except UpstreamError as exc:
            ctx.logger.error(
                f"Checkout failed: orders unreachable ({exc.detail})", trace_id=trace_id, upstream="orders"
            )
            return JSONResponse({"error": "upstream unavailable"}, status_code=502)

        if resp.status_code == 402:
            return JSONResponse(resp.json(), status_code=402)
        if resp.status_code >= 400:
            ctx.logger.error(
                f"Checkout failed: orders returned HTTP {resp.status_code}",
                trace_id=trace_id,
                upstream="orders",
                upstream_status=resp.status_code,
            )
            return JSONResponse({"error": "checkout failed"}, status_code=502)
        return resp.json()

    return app
