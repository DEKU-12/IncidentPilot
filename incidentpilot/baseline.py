"""A rule-based stand-in for an LLM, so the agent runs with no API keys.

It plays the same game through the same graph: follow the loudest error upstream,
check that service's deploys, then report. It is also the bar the real model has to
beat in the evals.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

_UPSTREAM = re.compile(r"\b(orders|payments)\b (?:returned|POST|unreachable)")
_REQUEST_LOG = re.compile(r"^(?:GET|POST) /")
_CATEGORIES = [
    ("memory_leak", "Memory limit"),
    ("connection_exhaustion", "QueuePool"),
    ("config_error", "KeyError"),
    ("bad_deploy", "Error:"),  # any other unhandled exception
    ("slow_dependency", "timed out|SLO breached"),
]


def _results(messages: list[AnyMessage]) -> list[tuple[str, dict, Any]]:
    """(tool name, args, parsed result) for every tool call answered so far."""
    calls = {c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    out = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.tool_call_id in calls:
            sc = (m.artifact or {}).get("structured_content", {}) if isinstance(m.artifact, dict) else {}
            data = sc.get("result", sc)
            out.append((m.name, calls[m.tool_call_id]["args"], data))
    return out


def _real_errors(groups: list[dict]) -> list[dict]:
    return [g for g in groups or [] if not _REQUEST_LOG.match(g["pattern"])]


class BaselineModel(BaseChatModel):
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "incidentpilot-baseline"

    def bind_tools(self, tools: list, **kwargs: Any):
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"tool_names": names})

    def _generate(self, messages: list[AnyMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        results = _results(messages)
        if self.tool_names == ["RCAReport"]:
            msg = AIMessage("", tool_calls=[{"name": "RCAReport", "args": self._report(results), "id": "report"}])
        else:
            step = self._next_call(results)
            if step is None:
                msg = AIMessage("I have enough evidence to write the report.")
            else:
                name, args = step
                msg = AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{len(results)}"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @staticmethod
    def _suspect(results: list[tuple[str, dict, Any]]) -> tuple[str | None, str | None]:
        """(service to look at next, service the latest top_errors call looked at)."""
        tops = [(args.get("service"), data) for name, args, data in results if name == "top_errors"]
        if not tops:
            return None, None
        looked_at, groups = tops[-1]
        errors = _real_errors(groups)
        if not errors:
            return looked_at, looked_at
        upstream = _UPSTREAM.search(errors[0]["pattern"])
        return (upstream.group(1) if upstream else errors[0]["service"]), looked_at

    def _next_call(self, results: list[tuple[str, dict, Any]]) -> tuple[str, dict] | None:
        done = {(name, args.get("service")) for name, args, _ in results}
        suspect, looked_at = self._suspect(results)
        if suspect is None:
            return "top_errors", {}
        if suspect != looked_at:
            return "top_errors", {"service": suspect}
        if ("query_logs", suspect) not in done:
            return "query_logs", {"service": suspect, "min_severity": "WARNING", "limit": 20}
        if ("list_revisions", suspect) not in done:
            return "list_revisions", {"service": suspect}
        return None

    def _report(self, results: list[tuple[str, dict, Any]]) -> dict[str, Any]:
        suspect, _ = self._suspect(results)
        suspect = suspect or "frontend"
        entries = [e for name, args, data in results if name == "query_logs" and args.get("service") == suspect for e in data]
        entries += [
            g["example"] for name, args, data in results if name == "top_errors" for g in _real_errors(data)
            if g["service"] == suspect
        ]
        text = " ".join(e["message"] for e in entries)
        category = next((c for c, pattern in _CATEGORIES if re.search(pattern, text)), "unknown")

        revs = next((d for name, args, d in results if name == "list_revisions" and args.get("service") == suspect), None)
        action, target = "none", None
        if revs and category != "slow_dependency":
            names = [r["name"] for r in revs["revisions"]]
            if revs["serving"] != names[0]:
                action, target = "rollback", names[names.index(revs["serving"]) - 1]

        return {
            "root_cause_service": suspect,
            "fault_category": category,
            "summary": f"Errors trace back to {suspect} ({category}). "
            + (f"Revision {revs['serving']} is serving." if revs else ""),
            "evidence_ids": list(dict.fromkeys(e["id"] for e in entries))[:5],
            "confidence": 0.6,
            "proposed_action": action,
            "rollback_to_revision": target,
            "rollback_evidence": f"{revs['serving']} changed {revs['revisions'][-1]['commit']!r} "
            f"{revs['revisions'][-1]['env_changes'] or ''}".strip() if action == "rollback" else None,
        }
