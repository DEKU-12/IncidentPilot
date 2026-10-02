"""payments: runs a fraud check with an outside provider, then charges the card.

Run: uvicorn incidentpilot.shopdemo.payments:create_app --factory --port 8003
"""

from __future__ import annotations

import asyncio
import secrets
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from incidentpilot.config import Settings
from incidentpilot.shopdemo.base import create_service_app, trace_id_of
from incidentpilot.shopdemo.faults import FaultKind

FRAUD_SLO_S = 0.3
DECLINE_RATE = 0.03


class ChargeRequest(BaseModel):
    order_id: str
    email: str
    amount: str
    card: str


def load_gateway_config(env: dict[str, str]) -> dict[str, str]:
    return {"url": env["PAYMENT_GATEWAY_URL"], "fraud_url": env["FRAUD_API_URL"]}


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    app, ctx = create_service_app("payments", settings, transport)

    def log_config() -> None:
        missing = [k for k in ("PAYMENT_GATEWAY_URL", "FRAUD_API_URL") if k not in ctx.revision.env]
        if missing:
            ctx.logger.warning(f"Config loaded with {len(missing)} expected key(s) missing: {', '.join(missing)}")
        else:
            ctx.logger.info("Config loaded: payment gateway and fraud-check provider configured")

    ctx.startup_hooks.append(log_config)

    @app.post("/charge")
    async def charge(body: ChargeRequest, request: Request):
        trace_id = trace_id_of(request)
        gateway = load_gateway_config(ctx.revision.env)
        fraud_host = urlparse(gateway["fraud_url"]).hostname

        if ctx.has_fault(FaultKind.SLOW_DEPENDENCY):
            latency = ctx.params(FaultKind.SLOW_DEPENDENCY)["latency_s"] * ctx.rng.uniform(0.9, 1.1)
        else:
            latency = ctx.rng.uniform(0.01, 0.04)
        # POST {fraud_url}/v1/score, warning as soon as the SLO is breached (even if the caller gives up).
        await asyncio.sleep(min(latency, FRAUD_SLO_S))
        if latency > FRAUD_SLO_S:
            ctx.logger.warning(
                f"Fraud-check provider {fraud_host} has not responded after {FRAUD_SLO_S * 1000:.0f}ms "
                "(SLO breached), still waiting",
                trace_id=trace_id,
                dependency=fraud_host,
            )
            await asyncio.sleep(latency - FRAUD_SLO_S)

        if ctx.rng.random() < DECLINE_RATE:
            ctx.logger.warning(
                f"Card {body.card} declined for {body.email}: insufficient_funds", trace_id=trace_id
            )
            return JSONResponse({"error": "card_declined"}, status_code=402)

        ctx.logger.info(
            f"Charged {body.amount} USD for order {body.order_id} via "
            f"{urlparse(gateway['url']).hostname}",
            trace_id=trace_id,
        )
        return {"charge_id": f"ch_{secrets.token_hex(6)}", "status": "succeeded"}

    return app
