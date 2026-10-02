"""Chaos controller: breaks ShopDemo on purpose and records the ground truth.

Every injection appends one line to ``var/ground_truth.jsonl``. That line holds
the true root cause and the correct fix, which the evals in Phase 4 grade the
agent against. The agent never sees this file.
"""

from __future__ import annotations

import json
import random
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx

from incidentpilot.config import SERVICES, Settings
from incidentpilot.shopdemo.faults import CATALOG, FaultKind
from incidentpilot.shopdemo.telemetry import utc_now_iso


class ChaosController:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.rng = rng or random.Random()
        self._http = httpx.AsyncClient(
            transport=transport,
            headers={"x-admin-token": settings.admin_token},
            timeout=10.0,
        )

    async def _admin(self, service: str, method: str, path: str, body: Any = None) -> dict[str, Any]:
        resp = await self._http.request(method, self.settings.urls[service] + path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def inject(
        self,
        fault: FaultKind | str,
        *,
        noise: bool = False,
        params: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        kind = FaultKind(fault)
        spec = CATALOG[kind]
        params = params or {}

        before = await self._admin(spec.service, "GET", "/admin/state")
        after = await self._admin(
            spec.service, "POST", "/admin/faults", {"fault": kind.value, "params": params}
        )

        noise_service = None
        if noise:
            noise_service = self.rng.choice([s for s in SERVICES if s != spec.service])
            await self._admin(noise_service, "POST", "/admin/noise", {"enabled": True})

        if spec.fix == "rollback":
            correct_action = {
                "type": "rollback",
                "service": spec.service,
                "to_revision": before["revision"],
            }
        else:
            correct_action = {"type": "none"}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        record = {
            "incident_id": f"inc_{stamp}_{secrets.token_hex(3)}",
            "injected_at": utc_now_iso(),
            "fault": kind.value,
            "root_cause_service": spec.service,
            "summary": spec.summary,
            "revision_before": before["revision"],
            "revision_after": after["revision"],
            "correct_action": correct_action,
            "red_herring_service": noise_service,
            "params": {**spec.default_params, **params},
        }
        path = self.settings.ground_truth_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return record

    async def clear(self) -> None:
        for service in SERVICES:
            await self._admin(service, "POST", "/admin/reset")

    async def status(self) -> dict[str, dict[str, Any]]:
        return {service: await self._admin(service, "GET", "/admin/state") for service in SERVICES}

    async def aclose(self) -> None:
        await self._http.aclose()


def load_ground_truth(settings: Settings) -> list[dict[str, Any]]:
    path = settings.ground_truth_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
