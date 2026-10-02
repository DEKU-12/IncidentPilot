"""ShopDemo behaves normally, and each fault shows the symptoms it should."""

from __future__ import annotations

import asyncio

from conftest import CHECKOUT

from incidentpilot.shopdemo.faults import FaultKind


async def checkout(client, quantity: int = 1):
    return await client.post("/checkout", json={**CHECKOUT, "quantity": quantity})


async def test_healthy_checkout_goes_through_all_three_services(shop, client):
    resp = await checkout(client)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == "12.50"

    traces = {
        name: {e.get("trace_id") for e in shop.logs(name) if e.get("http")}
        for name in ("frontend", "orders", "payments")
    }
    shared = traces["frontend"] & traces["orders"] & traces["payments"]
    assert len(shared) == 1, "one trace id should follow the request through every service"


async def test_logs_have_cloud_logging_fields(shop, client):
    await checkout(client)
    entry = shop.logs("orders")[-1]
    for key in ("insert_id", "timestamp", "severity", "service", "revision", "message"):
        assert key in entry
    assert entry["revision"] == "orders-00001"


async def test_admin_endpoints_require_the_token(client):
    assert (await client.get("/admin/state")).status_code == 401
    assert (await client.get("/admin/state", headers={"x-admin-token": "wrong"})).status_code == 401


async def test_public_revisions_never_reveal_the_fault(shop, admin_headers):
    shop.contexts["orders"].inject(FaultKind.BAD_DEPLOY, {})
    async with shop.client("orders") as c:
        body = (await c.get("/admin/revisions", headers=admin_headers)).json()
    assert body["serving"] == "orders-00002"
    assert "fault" not in str(body).lower()


async def test_bad_deploy_breaks_bulk_orders_and_rollback_fixes_it(shop, client):
    shop.contexts["orders"].inject(FaultKind.BAD_DEPLOY, {})

    assert (await checkout(client, quantity=1)).status_code == 200
    assert (await checkout(client, quantity=2)).status_code == 502

    errors = [e for e in shop.logs("orders") if e["severity"] == "ERROR"]
    assert any("Decimal is not JSON serializable" in e["message"] for e in errors)
    assert any("serializers.py" in e["context"]["stack_trace"] for e in errors if "context" in e)
    assert any("orders returned HTTP 500" in m for m in shop.messages("frontend", "ERROR"))

    shop.contexts["orders"].rollback()
    assert (await checkout(client, quantity=2)).status_code == 200
    assert any("rollback from orders-00002" in m for m in shop.messages("orders"))


async def test_config_error_makes_every_charge_fail(shop, client):
    shop.contexts["payments"].inject(FaultKind.CONFIG_ERROR, {})

    assert (await checkout(client)).status_code == 502
    assert any("KeyError" in m and "PAYMENT_GATEWAY_URL" in m for m in shop.messages("payments", "ERROR"))
    assert any("expected key(s) missing: PAYMENT_GATEWAY_URL" in m for m in shop.messages("payments", "WARNING"))
    assert "PAYMENT_GATEWAY_URL" in shop.contexts["payments"].revision.env_changes


async def test_connection_exhaustion_times_out_under_concurrency(shop, client):
    shop.contexts["orders"].inject(
        FaultKind.CONNECTION_EXHAUSTION, {"query_s": 0.15, "acquire_timeout_s": 0.05}
    )
    results = await asyncio.gather(*(checkout(client) for _ in range(5)))
    statuses = [r.status_code for r in results]

    assert statuses.count(200) >= 1
    assert statuses.count(502) >= 1
    assert any("QueuePool limit of size 1" in m for m in shop.messages("orders", "ERROR"))


async def test_slow_dependency_times_out_in_orders_and_rollback_does_not_help(shop, client):
    payments = shop.contexts["payments"]
    payments.inject(FaultKind.SLOW_DEPENDENCY, {"latency_s": 0.5})
    assert payments.revision.name == "payments-00001", "no deploy is involved"

    assert (await checkout(client)).status_code == 502
    assert any("payments POST /charge timed out" in m for m in shop.messages("orders", "ERROR"))

    shop.contexts["payments"].deploy("unrelated change", {})
    shop.contexts["payments"].rollback()
    assert payments.has_fault(FaultKind.SLOW_DEPENDENCY), "rolling back must not fix an outside provider"


async def test_memory_leak_ends_in_an_oom_restart(shop, client):
    frontend = shop.contexts["frontend"]
    frontend.inject(FaultKind.MEMORY_LEAK, {"mb_per_request": 100})

    statuses = [(await client.get("/products")).status_code for _ in range(5)]
    assert 503 in statuses
    assert frontend.restarts == 1
    assert any("Memory limit of 512 MiB exceeded" in m for m in shop.messages("frontend", "CRITICAL"))


async def test_red_herring_noise_adds_warnings(shop, client):
    shop.contexts["frontend"].noise = True
    for _ in range(10):
        await client.get("/products")
    assert len(shop.messages("frontend", "WARNING")) >= 3


async def test_metrics_flush_writes_a_point(shop, client):
    await checkout(client)
    point = shop.contexts["orders"].flush_metrics()
    assert point["requests"] == 1
    assert point["db_pool_size"] == 10
    assert (shop.settings.metrics_dir / "orders.jsonl").exists()


async def test_inject_rejects_a_fault_for_another_service(shop, admin_headers):
    async with shop.client("frontend") as c:
        resp = await c.post("/admin/faults", json={"fault": "bad_deploy"}, headers=admin_headers)
    assert resp.status_code == 400
