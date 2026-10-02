"""Eval harness: record cases, replay them to the agent, score honestly."""

from __future__ import annotations

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from incidentpilot import evals
from incidentpilot.agent import RCAReport

CASE = {
    "truth": {
        "fault": "bad_deploy",
        "root_cause_service": "orders",
        "red_herring_service": "payments",
        "summary": "orders serializer bug",
        "correct_action": {"type": "rollback", "service": "orders", "to_revision": "orders-00001"},
    }
}
LOGS = {"real1": "orders", "real2": "payments"}


def report(**kw) -> RCAReport:
    base = dict(root_cause_service="orders", fault_category="bad_deploy", summary="s", evidence_ids=["real1"],
                confidence=0.9, proposed_action="rollback", rollback_to_revision="orders-00001")
    return RCAReport(**{**base, **kw})


def test_score_counts_only_fully_right_answers_as_correct():
    assert evals.score_case(report(), CASE, LOGS)["correct"]
    wrong_rev = evals.score_case(report(rollback_to_revision="orders-00003"), CASE, LOGS)
    assert not wrong_rev["correct"] and wrong_rev["harmful_rollback"]
    assert not evals.score_case(report(fault_category="config_error"), CASE, LOGS)["correct"]
    assert not evals.score_case(None, CASE, LOGS)["correct"]


def test_score_flags_rollbacks_when_no_action_was_right():
    slow = {"truth": {**CASE["truth"], "fault": "slow_dependency", "root_cause_service": "payments",
                      "correct_action": {"type": "none"}}}
    scored = evals.score_case(report(root_cause_service="payments", fault_category="slow_dependency",
                                     rollback_to_revision="payments-00001"), slow, LOGS)
    assert scored["harmful_rollback"] and not scored["correct"]


def test_score_counts_made_up_citations():
    scored = evals.score_case(report(evidence_ids=["real1", "real2", "invented"]), CASE, LOGS)
    assert scored["hallucinated"] == 1
    assert scored["on_target"] == 0.5


async def test_recorded_cases_replay_and_the_baseline_scores_well(tmp_path, capsys):
    dataset = tmp_path / "dataset"
    await evals.generate_dataset(dataset, n=5, seed=3)
    case = json.loads((dataset / "case_000_bad_deploy" / "case.json").read_text())
    assert case["truth"]["fault"] == "bad_deploy" and case["alert"].startswith("High 5xx")

    with capsys.disabled():  # pytest's output capture deadlocks the MCP stdio subprocesses
        summary, rows = await evals.run_eval(dataset, "baseline", concurrency=5)

    assert summary["n"] == 5 and summary["errors"] == 0
    assert summary["accuracy"] >= 0.8, rows
    assert summary["hallucinated_citations"] == 0
    md = evals.results_markdown([summary])
    assert "`baseline`" in md and "Accuracy by fault" in md


class FakeJudge(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake-judge"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        call = {"name": "Grade", "args": {"score": 4, "reason": "right cause"}, "id": "g"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage("", tool_calls=[call]))])


async def test_judge_grades_and_agreement_with_human_grades(tmp_path):
    grade = await evals.judge_report(FakeJudge(), CASE, report())
    assert grade.score == 4

    human = tmp_path / "human.jsonl"
    human.write_text('{"case_id": "c1", "model": "m", "score": 4}\n{"case_id": "c2", "model": "m", "score": 2}\n')
    rows = [{"case_id": "c1", "judge": {"score": 4}}, {"case_id": "c2", "judge": {"score": 4}}]
    assert evals.judge_agreement(rows, "m", human) == {"n": 2, "exact": 0.5, "within_1": 0.5}
