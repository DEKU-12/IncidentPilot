"""Phase 5: injection guard, human approval, attack cases."""

from __future__ import annotations

import json

import pytest
from conftest import CHECKOUT
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from incidentpilot import evals, guard, mcp_server
from incidentpilot.agent import RCAReport, investigate_alert
from incidentpilot.baseline import BaselineModel
from incidentpilot.shopdemo.faults import FaultKind

ATTACK = evals.ATTACK_TEMPLATES[0].format(svc="payments", rev="payments-00001")


def test_guard_flags_text_aimed_at_the_agent_but_not_normal_logs():
    for template in evals.ATTACK_TEMPLATES[:3]:
        assert guard.looks_like_injection(template.format(svc="payments", rev="payments-00001"))
    normal = [
        "Unhandled exception in POST /orders: TypeError: Object of type Decimal is not JSON serializable",
        "sqlalchemy.exc.TimeoutError: QueuePool limit of size 1 overflow 0 reached",
        "Fraud-check provider api.fraudshield.example.com has not responded after 300ms (SLO breached)",
        "Memory limit of 512 MiB exceeded with 563 MiB used",
        "Checkout failed: orders returned HTTP 502",
    ]
    assert not any(guard.looks_like_injection(m) for m in normal)


async def test_attacker_coupon_text_is_quarantined_before_the_model_sees_it(shop, client, mcp_env, monkeypatch):
    await client.post("/checkout", json={**CHECKOUT, "coupon": ATTACK})

    [entry] = mcp_server.query_logs(service="frontend", min_severity="ERROR", contains="coupon")
    assert entry["message"] == guard.QUARANTINED and entry["quarantined"]

    monkeypatch.setenv("INCIDENTPILOT_GUARD", "off")
    [raw] = mcp_server.query_logs(service="frontend", min_severity="ERROR", contains="coupon")
    assert "ON-CALL AI AGENT" in raw["message"]
    assert any("ON-CALL AI AGENT" in g["pattern"] for g in mcp_server.top_errors(service="frontend")), \
        "the attack must surface in top_errors, the agent's first step"


async def run_with_approver(answer: bool, seen: list):
    async with create_connected_server_and_client_session(mcp_server.mcp._mcp_server) as session:
        tools = await load_mcp_tools(session)

        def approver(proposal: dict) -> bool:
            seen.append(proposal)
            return answer

        return await investigate_alert("High 5xx on POST /checkout", BaselineModel(), tools, approver=approver)


@pytest.mark.parametrize("answer", [True, False])
async def test_rollback_runs_only_after_a_human_says_yes(shop, client, mcp_env, answer):
    shop.contexts["orders"].inject(FaultKind.BAD_DEPLOY, {})
    for _ in range(3):
        await client.post("/checkout", json={**CHECKOUT, "quantity": 2})

    seen: list[dict] = []
    state = await run_with_approver(answer, seen)

    assert len(seen) == 1, "the human is asked exactly once"
    assert seen[0]["service"] == "orders" and seen[0]["to_revision"] == "orders-00001"
    assert seen[0]["rollback_evidence"]
    serving = shop.contexts["orders"].revision.name
    if answer:
        assert state["remediation"]["status"] == "rolled back" and serving == "orders-00001"
    else:
        assert state["remediation"]["status"] == "rejected by human" and serving == "orders-00002"


async def test_no_question_when_no_rollback_is_proposed(shop, client, mcp_env):
    shop.contexts["payments"].inject(FaultKind.SLOW_DEPENDENCY, {"latency_s": 0.8})
    for _ in range(3):
        await client.post("/checkout", json=CHECKOUT)

    seen: list[dict] = []
    state = await run_with_approver(True, seen)
    assert seen == [] and state["remediation"]["status"] == "no action proposed"


async def test_attack_cases_record_the_attack_and_score_it(tmp_path):
    [case_dir] = await evals.generate_dataset(tmp_path / "attacks", n=1, seed=5, attacks=True)
    case = json.loads((case_dir / "case.json").read_text())
    target = case["attack"]["service"]
    assert target != case["truth"]["root_cause_service"]
    assert case["attack"]["text"] in (case_dir / "logs" / "frontend.jsonl").read_text()

    followed = RCAReport(root_cause_service=target, fault_category="bad_deploy", summary="s", evidence_ids=["x"],
                         confidence=0.9, proposed_action="rollback", rollback_to_revision=case["attack"]["to_revision"],
                         rollback_evidence="per the note")
    assert evals.score_case(followed, case, {})["attack_success"]
