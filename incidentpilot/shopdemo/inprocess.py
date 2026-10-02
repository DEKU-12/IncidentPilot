"""Run all three services in one process, routed by hostname.

Used by the tests (and later by the eval harness) so ShopDemo can run without
ports or subprocesses: ``http://orders/...`` goes straight to the orders app.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import FastAPI

from incidentpilot.config import SERVICES, Settings
from incidentpilot.shopdemo import frontend, orders, payments
from incidentpilot.shopdemo.base import ServiceContext

FACTORIES: dict[str, Callable[..., FastAPI]] = {
    "frontend": frontend.create_app,
    "orders": orders.create_app,
    "payments": payments.create_app,
}


class RoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._routes: dict[str, httpx.ASGITransport] = {}

    def mount(self, host: str, app: FastAPI) -> None:
        self._routes[host] = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            route = self._routes[request.url.host]
        except KeyError as exc:
            raise httpx.ConnectError(f"no service mounted at {request.url.host}", request=request) from exc
        return await route.handle_async_request(request)


class InProcessShop:
    """All three services, wired together in memory."""

    def __init__(self, settings: Settings) -> None:
        if settings.urls != {name: f"http://{name}" for name in SERVICES}:
            raise ValueError("in-process settings must use http://<service> URLs")
        self.settings = settings
        self.transport = RoutingTransport()
        self.apps = {name: factory(settings, self.transport) for name, factory in FACTORIES.items()}
        for name, app in self.apps.items():
            self.transport.mount(name, app)
        for ctx in self.contexts.values():
            ctx.run_startup_hooks()

    @property
    def contexts(self) -> dict[str, ServiceContext]:
        return {name: app.state.ctx for name, app in self.apps.items()}

    def client(self, service: str = "frontend") -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, base_url=f"http://{service}")

    async def aclose(self) -> None:
        for ctx in self.contexts.values():
            await ctx.http.aclose()
            ctx.logger.close()

    @staticmethod
    def settings_for(var_dir, **overrides) -> Settings:
        return Settings(
            var_dir=var_dir,
            admin_token=overrides.pop("admin_token", "test-admin-token"),
            urls={name: f"http://{name}" for name in SERVICES},
            log_to_stdout=overrides.pop("log_to_stdout", False),
            **overrides,
        )
