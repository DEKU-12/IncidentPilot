"""The chaos controller injects faults through the admin API and records ground truth."""

from __future__ import annotations

import random

import pytest

from incidentpilot.chaos import ChaosController, load_ground_truth
from incidentpilot.traffic import run_traffic


@pytest.fixture
async def chaos(shop):
    controller = ChaosController(shop.settings, transport=shop.transport, rng=random.Random(1))
    yield controller
    await controller.aclose()


async def test_inject_records_ground_truth_with_the_correct_fix(shop, chaos):
    rec = await chaos.inject("bad_deploy", noise=True)

    assert rec["root_cause_service"] == "orders"
    assert rec["revision_before"] == "orders-00001"
    assert rec["revision_after"] == "orders-00002"
    assert rec["correct_action"] == {"type": "rollback", "service": "orders", "to_revision": "orders-00001"}
    assert rec["red_herring_service"] in {"frontend", "payments"}
    assert shop.contexts[rec["red_herring_service"]].noise
    assert load_ground_truth(shop.settings) == [rec]


async def test_slow_dependency_ground_truth_says_do_not_roll_back(chaos):
    rec = await chaos.inject("slow_dependency")
    assert rec["correct_action"] == {"type": "none"}
    assert rec["revision_before"] == rec["revision_after"]


async def test_clear_resets_every_service(shop, chaos):
    await chaos.inject("config_error", noise=True)
    await chaos.clear()
    status = await chaos.status()
    for state in status.values():
        assert state["revision"].endswith("-00001")
        assert state["revision_fault"] is None and state["external_fault"] is None
        assert not state["noise"]


async def test_traffic_generator_hits_the_shop(shop):
    counts = await run_traffic(shop.settings, rps=50, duration_s=0.5, seed=3, transport=shop.transport)
    assert sum(counts.values()) > 5
    assert counts[200] > 0
