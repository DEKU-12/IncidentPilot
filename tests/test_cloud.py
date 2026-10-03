"""Phase 6: the pieces that run on GCP, tested without GCP."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from incidentpilot import agent, mcp_server
from incidentpilot.agent_service import alert_text
from incidentpilot.config import REPO_ROOT


def test_cloud_logging_entries_map_to_the_local_shape():
    entry = SimpleNamespace(
        payload={"message": "KeyError: 'PAYMENT_GATEWAY_URL'", "service": "payments", "revision": "payments-00002",
                 "insert_id": "abc123", "trace_id": "t1"},
        severity="ERROR",
        insert_id="cloud-id",
        timestamp=datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    e = mcp_server._from_cloud(entry)
    assert (e["insert_id"], e["severity"], e["service"]) == ("abc123", "ERROR", "payments")
    assert e["timestamp"] == "2026-10-03T12:00:00.000Z"
    assert mcp_server._entry_view(e)["message"] == "KeyError: 'PAYMENT_GATEWAY_URL'"


def test_gcp_backend_queries_cloud_logging_with_the_right_filters(monkeypatch):
    monkeypatch.setenv("INCIDENTPILOT_BACKEND", "gcp")
    seen = []
    monkeypatch.setattr(mcp_server, "_cloud_entries", lambda flt, minutes: seen.append(flt) or [])

    mcp_server.query_logs(service="orders", min_severity="ERROR")
    mcp_server.get_metrics("payments")

    assert 'severity>=ERROR' in seen[0] and 'jsonPayload.service="orders"' in seen[0]
    assert 'jsonPayload.message!="metrics"' in seen[0]
    assert 'jsonPayload.message="metrics"' in seen[1] and 'jsonPayload.service="payments"' in seen[1]


def test_alert_text_reads_pubsub_monitoring_incidents_and_plain_requests():
    incident = {"incident": {"summary": "5xx rate on shopdemo-frontend is above threshold", "policy_name": "p"}}
    push = {"message": {"data": base64.b64encode(json.dumps(incident).encode()).decode()}}
    assert alert_text(push) == "5xx rate on shopdemo-frontend is above threshold"
    assert alert_text({"alert": "checkout is failing"}) == "checkout is failing"
    assert alert_text({}) == "High 5xx error rate on frontend"


def test_mcp_connection_is_local_stdio_unless_a_url_is_given(monkeypatch):
    monkeypatch.delenv("MCP_URL", raising=False)
    assert agent.mcp_connection()["transport"] == "stdio"

    monkeypatch.setattr(agent, "_identity_token", lambda audience: f"token-for-{audience}")
    remote = agent.mcp_connection("https://incidentpilot-mcp-xyz.a.run.app/")
    assert remote["url"] == "https://incidentpilot-mcp-xyz.a.run.app/mcp"
    assert remote["headers"]["Authorization"] == "Bearer token-for-https://incidentpilot-mcp-xyz.a.run.app"


async def test_serve_mcp_runs_the_mcp_server_over_http_like_cloud_run(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PORT": str(port), "PYTHONPATH": str(REPO_ROOT), "INCIDENTPILOT_VAR_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "incidentpilot.cli", "serve", "mcp"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _), \
                        ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            raise AssertionError("MCP server never came up over HTTP")
        assert {"top_errors", "query_logs", "rollback"} <= tools
        # Cloud Run sends its own hostname; the server must not reject it (it once answered HTTP 421).
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        resp = httpx.post(f"http://127.0.0.1:{port}/mcp", json=init, headers={
            "Host": "incidentpilot-mcp-abc-uc.a.run.app", "Accept": "application/json, text/event-stream"})
        assert resp.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=5)
