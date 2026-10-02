"""Structured logging and request metrics.

Log entries use the fields Cloud Logging understands (``severity``, ``message``,
``timestamp``), so the same services can run on Cloud Run unchanged. Locally each
service also appends to ``var/logs/<service>.jsonl``, which the observability
MCP server reads in Phase 2.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonLogger:
    def __init__(
        self,
        service: str,
        revision: Callable[[], str],
        path: Path,
        *,
        echo: bool = True,
        stream: TextIO | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.service = service
        self._revision = revision
        self._fh = open(path, "a", buffering=1, encoding="utf-8")
        self._echo = echo
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def log(
        self,
        severity: str,
        message: str,
        *,
        trace_id: str | None = None,
        http: dict[str, Any] | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "insert_id": uuid.uuid4().hex[:16],
            "timestamp": utc_now_iso(),
            "severity": severity,
            "service": self.service,
            "revision": self._revision(),
            "message": message,
        }
        if trace_id:
            entry["trace_id"] = trace_id
        if http:
            entry["http"] = http
        if context:
            entry["context"] = context
        line = json.dumps(entry, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            if self._echo:
                self._stream.write(line + "\n")
        return entry

    def info(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("INFO", message, **kw)

    def warning(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("WARNING", message, **kw)

    def error(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("ERROR", message, **kw)

    def critical(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("CRITICAL", message, **kw)

    def close(self) -> None:
        self._fh.close()


class Metrics:
    """Request counters and latencies since the last flush."""

    def __init__(self) -> None:
        self._latencies: list[float] = []
        self._errors = 0

    def observe(self, latency_ms: float, status: int) -> None:
        self._latencies.append(latency_ms)
        if status >= 500:
            self._errors += 1

    def snapshot(self, *, reset: bool = False) -> dict[str, Any]:
        lat = sorted(self._latencies)
        n = len(lat)

        def pct(p: float) -> float:
            return round(lat[min(n - 1, int(p * n))], 1) if n else 0.0

        snap = {
            "requests": n,
            "errors_5xx": self._errors,
            "error_rate": round(self._errors / n, 3) if n else 0.0,
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "max_ms": round(lat[-1], 1) if n else 0.0,
        }
        if reset:
            self._latencies.clear()
            self._errors = 0
        return snap
