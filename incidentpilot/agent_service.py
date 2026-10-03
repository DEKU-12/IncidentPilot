"""The agent as a web service: Cloud Monitoring fires an alert into Pub/Sub, Pub/Sub pushes
it here, and the agent investigates through the MCP server and logs its report.

This service is read-only. Rollbacks stay with a human: `incidentpilot investigate --approve`.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

from fastapi import FastAPI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from incidentpilot.agent import investigate_alert, make_model, mcp_connection

DEFAULT_ALERT = "High 5xx error rate on frontend"


def alert_text(body: dict[str, Any]) -> str:
    """Accepts a Pub/Sub push (wrapping a Cloud Monitoring incident) or a plain {"alert": "..."}."""
    if "message" in body:
        raw = base64.b64decode(body["message"].get("data", "") or b"").decode() or "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return raw or DEFAULT_ALERT
    incident = body.get("incident")
    if isinstance(incident, dict):
        return incident.get("summary") or incident.get("policy_name") or DEFAULT_ALERT
    return body.get("alert") or DEFAULT_ALERT


def create_app() -> FastAPI:
    app = FastAPI(title="incidentpilot-agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/incident")
    async def incident(body: dict[str, Any]) -> dict[str, Any]:
        alert = alert_text(body)
        async with MultiServerMCPClient({"incidentpilot": mcp_connection()}).session("incidentpilot") as session:
            state = await investigate_alert(alert, make_model(), await load_mcp_tools(session))
        report = state["report"].model_dump() if state["report"] else None
        result = {"alert": alert, "report": report, "problems": state["problems"], "usage": state["usage"]}
        # One structured log line per incident; find reports with jsonPayload.message="rca_report".
        sys.stdout.write(json.dumps({"severity": "NOTICE", "message": "rca_report", **result}) + "\n")
        sys.stdout.flush()
        return result

    return app
