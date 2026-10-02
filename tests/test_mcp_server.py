"""MCP tools against a live in-process ShopDemo with a fault injected."""

from __future__ import annotations

import pytest
from conftest import CHECKOUT
from mcp.shared.memory import create_connected_server_and_client_session

from incidentpilot import approval, mcp_server
from incidentpilot.config import SERVICES
from incidentpilot.redact import redact
from incidentpilot.shopdemo.faults import FaultKind


@pytest.fixture
async def broken_shop(shop, client, monkeypatch):
    """Orders is on a bad revision and has served some failing bulk orders."""
    monkeypatch.setenv("INCIDENTPILOT_VAR_DIR", str(shop.settings.var_dir))
    monkeypatch.setenv("SHOPDEMO_ADMIN_TOKEN", shop.settings.admin_token)
    for name in SERVICES:
        monkeypatch.setenv(f"{name.upper()}_URL", f"http://{name}")
    monkeypatch.setattr(mcp_server, "TRANSPORT", shop.transport)

    shop.contexts["orders"].inject(FaultKind.BAD_DEPLOY, {})
    for qty in (1, 2, 2, 3):
        await client.post("/checkout", json={**CHECKOUT, "quantity": qty})
    return shop


def test_redact_masks_emails_and_cards():
    text = "Card 4242 4242 4242 4242 declined for alice@example.com (order ord_1234)"
    assert redact(text) == "Card [CARD] declined for [EMAIL] (order ord_1234)"


async def test_query_logs_redacts_pii_and_returns_citable_ids(broken_shop):
    entries = mcp_server.query_logs(service="frontend", min_severity="INFO", contains="checkout started")
    assert entries
    assert all("@" not in e["message"] for e in entries)
    assert all(e["id"] and e["revision"] for e in entries)


async def test_top_errors_puts_the_real_bug_first(broken_shop):
    top = mcp_server.top_errors(service="orders")
    assert "Decimal is not JSON serializable" in top[0]["pattern"]
    assert top[0]["count"] == 3
    assert top[0]["revisions"] == ["orders-00002"]
    assert "serializers.py" in top[0]["example"]["stack_trace"]


async def test_get_metrics_returns_flushed_points(broken_shop):
    broken_shop.contexts["orders"].flush_metrics()
    points = mcp_server.get_metrics("orders")
    assert points[-1]["errors_5xx"] == 3


async def test_list_revisions_shows_the_deploy_but_not_the_answer(broken_shop):
    revs = await mcp_server.list_revisions("orders")
    assert revs["serving"] == "orders-00002"
    assert "Decimal" in revs["revisions"][1]["commit"]
    assert "bad_deploy" not in str(revs)


def test_search_runbooks_finds_the_matching_guide():
    hits = mcp_server.search_runbooks("connection pool timed out")
    assert hits[0]["runbook"] == "database-connection-pool"


def test_unknown_service_is_rejected():
    with pytest.raises(ValueError, match="unknown service"):
        mcp_server.query_logs(service="billing")


async def test_rollback_needs_a_valid_token_for_that_exact_action(broken_shop):
    with pytest.raises(PermissionError, match="signature"):
        await mcp_server.rollback("orders", "orders-00001", "forged.token")
    wrong_target = approval.mint("payments", "payments-00001")
    with pytest.raises(PermissionError, match="not orders"):
        await mcp_server.rollback("orders", "orders-00001", wrong_target)
    expired = approval.mint("orders", "orders-00001", ttl_s=-1)
    with pytest.raises(PermissionError, match="expired"):
        await mcp_server.rollback("orders", "orders-00001", expired)
    assert broken_shop.contexts["orders"].revision.name == "orders-00002"

    token = approval.mint("orders", "orders-00001")
    result = await mcp_server.rollback("orders", "orders-00001", token)
    assert result["to_revision"] == "orders-00001"
    assert broken_shop.contexts["orders"].revision.name == "orders-00001"

    with pytest.raises(PermissionError, match="already used"):
        await mcp_server.rollback("orders", "orders-00001", token)


async def test_tools_are_served_over_mcp(broken_shop):
    async with create_connected_server_and_client_session(mcp_server.mcp._mcp_server) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == {"query_logs", "top_errors", "get_metrics", "list_revisions", "search_runbooks", "rollback"}

        result = await session.call_tool("top_errors", {"service": "orders"})
        assert not result.isError
        assert "Decimal is not JSON serializable" in result.content[0].text
