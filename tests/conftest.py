from __future__ import annotations

import json
from pathlib import Path

import pytest

from incidentpilot.shopdemo.inprocess import InProcessShop

CHECKOUT = {"email": "alice@example.com", "card": "4242 4242 4242 4242", "sku": "MUG-001"}


class Shop(InProcessShop):
    def logs(self, service: str) -> list[dict]:
        path: Path = self.settings.logs_dir / f"{service}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def messages(self, service: str, severity: str | None = None) -> list[str]:
        return [e["message"] for e in self.logs(service) if severity is None or e["severity"] == severity]


@pytest.fixture
async def shop(tmp_path):
    shop = Shop(InProcessShop.settings_for(tmp_path, upstream_timeout_s=0.2))
    for ctx in shop.contexts.values():
        ctx.rng.seed(7)  # seed 7 makes no payment declines in the first few charges
    yield shop
    await shop.aclose()


@pytest.fixture
async def client(shop):
    async with shop.client() as c:
        yield c


@pytest.fixture
def admin_headers(shop):
    return {"x-admin-token": shop.settings.admin_token}
