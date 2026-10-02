"""Runtime settings, read from environment variables with local-dev defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SERVICES: tuple[str, ...] = ("frontend", "orders", "payments")
DEFAULT_PORTS: dict[str, int] = {"frontend": 8001, "orders": 8002, "payments": 8003}

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    var_dir: Path
    admin_token: str
    urls: dict[str, str]
    upstream_timeout_s: float = 2.0
    metrics_interval_s: float = 5.0
    memory_limit_mb: float = 512.0
    log_to_stdout: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        urls = {
            name: os.environ.get(f"{name.upper()}_URL", f"http://127.0.0.1:{port}")
            for name, port in DEFAULT_PORTS.items()
        }
        return cls(
            var_dir=Path(os.environ.get("INCIDENTPILOT_VAR_DIR", REPO_ROOT / "var")),
            admin_token=os.environ.get("SHOPDEMO_ADMIN_TOKEN", "dev-admin-token"),
            urls=urls,
            upstream_timeout_s=float(os.environ.get("UPSTREAM_TIMEOUT_S", "2.0")),
            metrics_interval_s=float(os.environ.get("METRICS_INTERVAL_S", "5.0")),
            memory_limit_mb=float(os.environ.get("MEMORY_LIMIT_MB", "512")),
            log_to_stdout=os.environ.get("LOG_TO_STDOUT", "1") == "1",
        )

    @property
    def logs_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def metrics_dir(self) -> Path:
        return self.var_dir / "metrics"

    @property
    def ground_truth_path(self) -> Path:
        return self.var_dir / "ground_truth.jsonl"
