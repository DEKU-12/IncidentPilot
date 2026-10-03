"""The LangGraph agent, driven by the offline baseline model, against every fault."""

from __future__ import annotations

import random

import pytest
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from incidentpilot import mcp_server
from incidentpilot.agent import RCAReport, check_report, investigate_alert
from incidentpilot.baseline import BaselineModel
from incidentpilot.chaos import ChaosController
from incidentpilot.shopdemo.faults import FaultKind
from incidentpilot.traffic import run_traffic

FAST_PARAMS = {
    "connection_exhaustion": {"query_s": 0.15, "acquire_timeout_s": 0.05},
    "memory_leak": {"mb_per_request": 100},
    "slow_dependency": {"latency_s": 0.8},
}


async def run_agent(model) -> dict:
    async with create_connected_server_and_client_session(mcp_server.mcp._mcp_server) as session:
        tools = await load_mcp_tools(session)
        return await investigate_alert("High 5xx error rate on frontend POST /checkout", model, tools)


@pytest.mark.parametrize(
    "fault", ["bad_deploy", "config_error", "connection_exhaustion", "memory_leak", "slow_dependency"]
)
async def test_baseline_agent_finds_each_root_cause(shop, mcp_env, fault):
    chaos = ChaosController(shop.settings, transport=shop.transport, rng=random.Random(0))
    truth = await chaos.inject(fault, noise=True, params=FAST_PARAMS.get(fault))
    await chaos.aclose()
    await run_traffic(shop.settings, rps=40, duration_s=1.0, seed=1, transport=shop.transport)

    state = await run_agent(BaselineModel())
    report = state["report"]

    assert state["problems"] == [], "the report's citations should check out"
    assert report.root_cause_service == truth["root_cause_service"]
    assert report.fault_category == fault
    if truth["correct_action"]["type"] == "rollback":
        assert report.proposed_action == "rollback"
        assert report.rollback_to_revision == truth["correct_action"]["to_revision"]
    else:
        assert report.proposed_action == "none"
    assert all(name != "rollback" for name, _ in state["tool_calls"]), "the agent never gets the write tool"


def test_check_report_rejects_invented_evidence_and_revisions():
    seen = [ToolMessage('{"id": "abc123", "serving": "orders-00002", "name": "orders-00001"}', tool_call_id="1")]
    good = RCAReport(
        root_cause_service="orders", fault_category="bad_deploy", summary="s", evidence_ids=["abc123"],
        confidence=0.9, proposed_action="rollback", rollback_to_revision="orders-00001",
        rollback_evidence="orders-00002 changed the serializer; the TypeError comes from that code",
    )
    assert check_report(good, seen) == []
    assert "without rollback_evidence" in check_report(good.model_copy(update={"rollback_evidence": None}), seen)[0]
    assert "never appeared" in check_report(good.model_copy(update={"evidence_ids": ["fake99"]}), seen)[0]
    assert "not a orders revision" in check_report(good.model_copy(update={"rollback_to_revision": "orders-00007"}), seen)[0]
    assert check_report(None, seen)


class LyingModel(BaselineModel):
    """Investigates like the baseline, then cites evidence that doesn't exist."""

    def _report(self, results):
        return {**super()._report(results), "evidence_ids": ["made-up-id"]}


async def test_unverifiable_report_is_retried_then_marked_low_confidence(shop, mcp_env, client):
    shop.contexts["orders"].inject(FaultKind.BAD_DEPLOY, {})
    await client.post("/checkout", json={"email": "a@example.com", "card": "4242", "sku": "MUG-001", "quantity": 2})

    state = await run_agent(LyingModel())

    assert state["verify_retries"] == 2
    assert state["report"].confidence <= 0.3
    assert any("made-up-id" in p for p in state["problems"])
