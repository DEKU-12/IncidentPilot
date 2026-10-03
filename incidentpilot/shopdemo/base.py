"""What every ShopDemo service shares: revisions, admin API, telemetry, upstream calls.

Revisions follow Cloud Run: a deploy creates ``<service>-0000N`` and shifts all
traffic to it; a rollback shifts traffic back to an older revision.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from incidentpilot.config import Settings
from incidentpilot.shopdemo.faults import CATALOG, FaultKind
from incidentpilot.shopdemo.telemetry import JsonLogger, Metrics, utc_now_iso

IMAGE_REPO = "us-central1-docker.pkg.dev/shopdemo/services"

BASE_ENV: dict[str, dict[str, str]] = {
    "frontend": {"PRODUCT_CACHE": "lru:256"},
    "orders": {"DB_POOL_SIZE": "10", "DB_HOST": "10.12.0.3"},
    "payments": {
        "PAYMENT_GATEWAY_URL": "https://gateway.example.com/v2",
        "FRAUD_API_URL": "https://api.fraudshield.example.com",
    },
}

BASE_MEMORY_MB: dict[str, float] = {"frontend": 140.0, "orders": 180.0, "payments": 110.0}

# Plausible but harmless warnings, emitted while "red herring" noise is on.
NOISE: dict[str, list[str]] = {
    "frontend": [
        "Deprecated API version header 'X-Api-Version: 1' received from client",
        "Template cache miss ratio 0.41 above threshold 0.30",
        "Slow GC pause: 182ms",
    ],
    "orders": [
        "Retrying idempotency-key lookup after transient read conflict",
        "Inventory sync lagging by 45s",
        "Slow query warning: SELECT * FROM promotions took 240ms",
    ],
    "payments": [
        "Webhook signature check skipped for sandbox event",
        "Currency rate cache is 6 minutes old",
        "TLS session resumption failed; full handshake used",
    ],
}

TRACE_KEY = "incidentpilot.trace_id"
UNTRACKED_PREFIXES = ("/admin", "/health", "/metrics")


@dataclass
class Revision:
    name: str
    created_at: str
    image: str
    commit: str
    env: dict[str, str]
    env_changes: dict[str, str] = field(default_factory=dict)
    # Ground truth. Never exposed through the public revisions endpoint.
    fault: FaultKind | None = None
    params: dict[str, float] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "image": self.image,
            "commit": self.commit,
            "env_changes": self.env_changes,
        }


class UpstreamError(Exception):
    def __init__(self, service: str, detail: str) -> None:
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.detail = detail


class UpstreamTimeout(UpstreamError):
    pass


def trace_id_of(request: Request) -> str:
    return request.scope.get(TRACE_KEY) or uuid.uuid4().hex


class ServiceContext:
    def __init__(
        self,
        name: str,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.settings = settings
        self.rng = random.Random()
        self.logger = JsonLogger(
            name,
            lambda: self.revision.name,
            settings.logs_dir / f"{name}.jsonl",
            echo=settings.log_to_stdout,
        )
        self.metrics = Metrics()
        self.http = httpx.AsyncClient(transport=transport)
        # Called whenever a revision starts serving (startup, deploy, rollback).
        self.startup_hooks: list[Callable[[], None]] = []
        self.extra_gauges: Callable[[], dict[str, Any]] = dict
        self._reset_state()

    def _reset_state(self) -> None:
        self.revisions = [self._make_revision(1, "initial release", dict(BASE_ENV[self.name]))]
        self.current = 0
        self.external_fault: FaultKind | None = None
        self.external_params: dict[str, float] = {}
        self.noise = False
        self.memory_mb = BASE_MEMORY_MB[self.name]
        self.restarts = 0
        self.flags: dict[str, Any] = {}

    def _make_revision(
        self,
        number: int,
        commit: str,
        env: dict[str, str],
        env_changes: dict[str, str] | None = None,
        fault: FaultKind | None = None,
        params: dict[str, float] | None = None,
    ) -> Revision:
        return Revision(
            name=f"{self.name}-{number:05d}",
            created_at=utc_now_iso(),
            image=f"{IMAGE_REPO}/{self.name}:sha-{secrets.token_hex(4)}",
            commit=commit,
            env=env,
            env_changes=env_changes or {},
            fault=fault,
            params=params or {},
        )

    @property
    def revision(self) -> Revision:
        return self.revisions[self.current]

    def has_fault(self, kind: FaultKind) -> bool:
        return self.revision.fault == kind or self.external_fault == kind

    def params(self, kind: FaultKind) -> dict[str, float]:
        overrides = self.revision.params if self.revision.fault == kind else self.external_params
        return {**CATALOG[kind].default_params, **overrides}

    def run_startup_hooks(self) -> None:
        for hook in self.startup_hooks:
            hook()

    # -- changes made through the admin API ---------------------------------

    def inject(self, kind: FaultKind, params: dict[str, float]) -> None:
        spec = CATALOG[kind]
        if spec.service != self.name:
            raise ValueError(f"{kind.value} is a {spec.service} fault, not a {self.name} fault")
        if spec.via_revision:
            self.deploy(spec.commit or kind.value, spec.env_changes, fault=kind, params=params)
        else:
            self.external_fault = kind
            self.external_params = params

    def deploy(
        self,
        commit: str,
        env_changes: dict[str, str | None],
        *,
        fault: FaultKind | None = None,
        params: dict[str, float] | None = None,
    ) -> Revision:
        env = dict(self.revision.env)
        shown: dict[str, str] = {}
        for key, value in env_changes.items():
            old = env.get(key, "(unset)")
            if value is None:
                env.pop(key, None)
                shown[key] = f"{old} -> (removed)"
            else:
                env[key] = value
                shown[key] = f"{old} -> {value}"
        rev = self._make_revision(len(self.revisions) + 1, commit, env, shown, fault, params)
        self.revisions.append(rev)
        self.current = len(self.revisions) - 1
        self.memory_mb = BASE_MEMORY_MB[self.name]
        self.logger.info(f"Deploying revision {rev.name} (image {rev.image})", event="deploy")
        self.logger.info(f"Revision {rev.name} is ready and serving 100% of traffic", event="traffic")
        self.run_startup_hooks()
        return rev

    def rollback(self, to_revision: str | None = None) -> dict[str, str]:
        if to_revision is None:
            if self.current == 0:
                raise ValueError("no earlier revision to roll back to")
            target = self.current - 1
        else:
            names = [r.name for r in self.revisions]
            if to_revision not in names:
                raise ValueError(f"unknown revision {to_revision!r}")
            target = names.index(to_revision)
        if target == self.current:
            raise ValueError(f"{to_revision} is already serving")
        old = self.revision.name
        self.current = target
        self.memory_mb = BASE_MEMORY_MB[self.name]
        self.flags.clear()
        self.logger.info(
            f"Traffic shifted: 100% to {self.revision.name} (rollback from {old})", event="rollback"
        )
        self.run_startup_hooks()
        return {"service": self.name, "from_revision": old, "to_revision": self.revision.name}

    def reset(self) -> None:
        self._reset_state()

    def state(self) -> dict[str, Any]:
        """Full state, including ground truth. Only the chaos controller reads this."""
        return {
            "service": self.name,
            "revision": self.revision.name,
            "revision_fault": self.revision.fault.value if self.revision.fault else None,
            "external_fault": self.external_fault.value if self.external_fault else None,
            "noise": self.noise,
            "memory_mb": round(self.memory_mb, 1),
            "restarts": self.restarts,
            "revisions": [r.public() for r in self.revisions],
        }

    # -- calls to other services --------------------------------------------

    async def call(
        self,
        service: str,
        method: str,
        path: str,
        *,
        trace_id: str,
        timeout: float,
        json: Any = None,
    ) -> httpx.Response:
        url = self.settings.urls[service] + path
        try:
            return await asyncio.wait_for(
                self.http.request(method, url, json=json, headers={"x-trace-id": trace_id}),
                timeout,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamTimeout(service, f"timed out after {timeout:.1f}s") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(service, f"{type(exc).__name__}: {exc}") from exc

    # -- metrics ------------------------------------------------------------

    def gauges(self) -> dict[str, Any]:
        jitter = self.rng.uniform(-4, 4)
        return {
            "memory_mb": round(self.memory_mb + jitter, 1),
            "restarts": self.restarts,
            **self.extra_gauges(),
        }

    def flush_metrics(self) -> dict[str, Any]:
        point = {
            "timestamp": utc_now_iso(),
            "service": self.name,
            "revision": self.revision.name,
            **self.metrics.snapshot(reset=True),
            **self.gauges(),
        }
        path = self.settings.metrics_dir / f"{self.name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(point) + "\n")
        if self.settings.log_to_stdout:  # on Cloud Run, stdout goes to Cloud Logging, where the MCP server reads it
            sys.stdout.write(json.dumps({"severity": "DEBUG", "message": "metrics", "service": self.name,
                                         "metrics": point}) + "\n")
        return point


class TelemetryMiddleware:
    """Trace propagation, request logs, metrics, and uncaught-exception logging."""

    def __init__(self, app: Any, ctx: ServiceContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        ctx = self.ctx
        method, path = scope["method"], scope["path"]
        headers = dict(scope.get("headers") or [])
        trace_id = headers.get(b"x-trace-id", b"").decode() or uuid.uuid4().hex
        scope[TRACE_KEY] = trace_id
        response = {"status": 500, "started": False}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                response["started"] = True
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            stack = "".join(traceback.format_exception(exc)[-6:])
            ctx.logger.error(
                f"Unhandled exception in {method} {path}: {type(exc).__name__}: {exc}",
                trace_id=trace_id,
                stack_trace=stack,
            )
            response["status"] = 500
            if not response["started"]:
                body = b'{"error":"internal server error"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})

        if path.startswith(UNTRACKED_PREFIXES):
            return
        latency_ms = (time.perf_counter() - start) * 1000
        status = response["status"]
        ctx.metrics.observe(latency_ms, status)
        severity = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
        ctx.logger.log(
            severity,
            f"{method} {path} {status} {latency_ms:.0f}ms",
            trace_id=trace_id,
            http={"method": method, "path": path, "status": status, "latency_ms": round(latency_ms, 1)},
        )
        if ctx.noise and ctx.rng.random() < 0.6:
            ctx.logger.warning(ctx.rng.choice(NOISE[ctx.name]), trace_id=trace_id)


class FaultRequest(BaseModel):
    fault: FaultKind
    params: dict[str, float] = {}


class NoiseRequest(BaseModel):
    enabled: bool


class RollbackRequest(BaseModel):
    to_revision: str | None = None


def _admin_router(ctx: ServiceContext) -> APIRouter:
    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if not x_admin_token or not secrets.compare_digest(x_admin_token, ctx.settings.admin_token):
            raise HTTPException(status_code=401, detail="invalid admin token")

    router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    @router.get("/state")
    async def state() -> dict[str, Any]:
        return ctx.state()

    @router.get("/revisions")
    async def revisions() -> dict[str, Any]:
        return {
            "service": ctx.name,
            "serving": ctx.revision.name,
            "revisions": [r.public() for r in ctx.revisions],
        }

    @router.post("/faults")
    async def inject(req: FaultRequest) -> dict[str, Any]:
        try:
            ctx.inject(req.fault, req.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ctx.state()

    @router.post("/noise")
    async def noise(req: NoiseRequest) -> dict[str, Any]:
        ctx.noise = req.enabled
        return ctx.state()

    @router.post("/rollback")
    async def rollback(req: RollbackRequest) -> dict[str, str]:
        try:
            return ctx.rollback(req.to_revision)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/reset")
    async def reset() -> dict[str, Any]:
        ctx.reset()
        return ctx.state()

    return router


async def _metrics_loop(ctx: ServiceContext) -> None:
    while True:
        await asyncio.sleep(ctx.settings.metrics_interval_s)
        ctx.flush_metrics()


def create_service_app(
    name: str,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[FastAPI, ServiceContext]:
    settings = settings or Settings.from_env()
    ctx = ServiceContext(name, settings, transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.logger.info(f"{name} started; serving revision {ctx.revision.name}")
        ctx.run_startup_hooks()
        task = asyncio.create_task(_metrics_loop(ctx))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await ctx.http.aclose()
            ctx.logger.close()

    app = FastAPI(title=f"shopdemo-{name}", lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(TelemetryMiddleware, ctx=ctx)
    app.include_router(_admin_router(ctx))

    @app.get("/health")  # not /healthz: Cloud Run reserves paths ending in "z"
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": name, "revision": ctx.revision.name}

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        return {"service": name, "revision": ctx.revision.name, **ctx.metrics.snapshot(), **ctx.gauges()}

    return app, ctx
