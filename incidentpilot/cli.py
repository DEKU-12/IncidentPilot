"""Command line: ``incidentpilot up | traffic | chaos | logs | mcp | approve | investigate | eval``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from incidentpilot.chaos import ChaosController, load_ground_truth
from incidentpilot.config import DEFAULT_PORTS, SERVICES, Settings
from incidentpilot.shopdemo.faults import CATALOG, FaultKind
from incidentpilot.traffic import run_traffic

SEVERITY_ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
COLORS = {"WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[1;31m"}
RESET = "\033[0m"


# -- up ---------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.fresh:
        for path in (settings.logs_dir, settings.metrics_dir):
            shutil.rmtree(path, ignore_errors=True)
        settings.ground_truth_path.unlink(missing_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    # Treat `kill` (SIGTERM) like Ctrl+C so the services are always stopped with us.
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)

    procs: list[subprocess.Popen] = []
    try:
        for name in SERVICES:
            cmd = [
                sys.executable, "-m", "uvicorn", f"incidentpilot.shopdemo.{name}:create_app",
                "--factory", "--port", str(DEFAULT_PORTS[name]),
                "--no-access-log", "--log-level", "warning",
            ]
            env = {**os.environ, "LOG_TO_STDOUT": "0"}
            procs.append(subprocess.Popen(cmd, env=env))

        deadline = time.monotonic() + 15
        pending = set(SERVICES)
        while pending and time.monotonic() < deadline:
            for name in list(pending):
                try:
                    if httpx.get(settings.urls[name] + "/healthz", timeout=0.5).status_code == 200:
                        pending.discard(name)
                except httpx.HTTPError:
                    pass
            time.sleep(0.3)
        if pending:
            print(f"Services failed to start: {', '.join(sorted(pending))}", file=sys.stderr)
            return 1

        print("ShopDemo is running:")
        for name in SERVICES:
            print(f"  {name:<9} {settings.urls[name]}")
        print(f"Logs: {settings.logs_dir}    (Ctrl+C to stop)")
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("A service exited unexpectedly.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopping ShopDemo...")
        return 0
    finally:
        _stop(procs)


def _stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


# -- traffic ----------------------------------------------------------------


def cmd_traffic(args: argparse.Namespace) -> int:
    settings = Settings.from_env()

    def report(counts: Counter) -> None:
        total = sum(counts.values())
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=str))
        print(f"[traffic] {total} requests  ({parts})", flush=True)

    duration = "until Ctrl+C" if args.duration <= 0 else f"for {args.duration:g}s"
    print(f"Sending ~{args.rps:g} req/s to {settings.urls['frontend']} {duration}")
    try:
        asyncio.run(run_traffic(settings, rps=args.rps, duration_s=args.duration, seed=args.seed, report=report))
    except KeyboardInterrupt:
        pass
    return 0


# -- chaos ------------------------------------------------------------------


def _parse_params(items: list[str]) -> dict[str, float]:
    params = {}
    for item in items:
        key, _, value = item.partition("=")
        if not value:
            raise SystemExit(f"--param must look like key=value, got {item!r}")
        params[key] = float(value)
    return params


def cmd_chaos(args: argparse.Namespace) -> int:
    settings = Settings.from_env()

    if args.chaos_cmd == "list":
        for kind, spec in CATALOG.items():
            print(f"{kind.value:<22} {spec.service:<9} fix={spec.fix:<9} {spec.summary}")
        return 0
    if args.chaos_cmd == "history":
        for rec in load_ground_truth(settings):
            print(json.dumps(rec))
        return 0

    async def run() -> int:
        chaos = ChaosController(settings)
        try:
            if args.chaos_cmd == "inject":
                rec = await chaos.inject(args.fault, noise=args.noise, params=_parse_params(args.param))
                print(f"Injected {rec['fault']} into {rec['root_cause_service']} "
                      f"({rec['revision_before']} -> {rec['revision_after']}).")
                if rec["red_herring_service"]:
                    print(f"Red-herring noise is on in {rec['red_herring_service']}.")
                print(f"Ground truth recorded as {rec['incident_id']} in {settings.ground_truth_path}")
            elif args.chaos_cmd == "clear":
                await chaos.clear()
                print("All services reset to a clean first revision.")
            elif args.chaos_cmd == "status":
                for name, state in (await chaos.status()).items():
                    fault = state["revision_fault"] or state["external_fault"] or "-"
                    print(f"{name:<9} serving {state['revision']:<15} fault={fault:<22} "
                          f"noise={'on' if state['noise'] else 'off':<3} memory={state['memory_mb']}MiB")
        except httpx.ConnectError:
            print("Can't reach ShopDemo. Start it first with: incidentpilot up", file=sys.stderr)
            return 1
        finally:
            await chaos.aclose()
        return 0

    return asyncio.run(run())


# -- logs -------------------------------------------------------------------


def _read_entries(logs_dir: Path, services: list[str]) -> list[dict]:
    entries = []
    for name in services:
        path = logs_dir / f"{name}.jsonl"
        if path.exists():
            entries += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return sorted(entries, key=lambda e: e["timestamp"])


def _format(entry: dict, color: bool) -> str:
    sev = entry["severity"]
    line = f"{entry['timestamp'][11:23]} {sev:<8} {entry['service']:<9} {entry['revision']:<15} {entry['message']}"
    if color and sev in COLORS:
        line = COLORS[sev] + line + RESET
    return line


def cmd_logs(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    services = [args.service] if args.service else list(SERVICES)
    min_level = SEVERITY_ORDER.index(args.severity)
    color = sys.stdout.isatty()

    def wanted(e: dict) -> bool:
        return SEVERITY_ORDER.index(e["severity"]) >= min_level

    seen: set[str] = set()
    entries = [e for e in _read_entries(settings.logs_dir, services) if wanted(e)]
    for e in entries[-args.lines:]:
        print(_format(e, color))
    seen.update(e["insert_id"] for e in entries)
    try:
        while args.follow:
            time.sleep(1)
            for e in _read_entries(settings.logs_dir, services):
                if e["insert_id"] not in seen:
                    seen.add(e["insert_id"])
                    if wanted(e):
                        print(_format(e, color), flush=True)
    except KeyboardInterrupt:
        pass
    return 0


# -- mcp / approve ------------------------------------------------------------


def cmd_mcp(args: argparse.Namespace) -> int:
    from incidentpilot.mcp_server import main as serve

    serve(http=args.http)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from incidentpilot import approval

    print(approval.mint(args.service, args.to_revision))
    return 0


# -- investigate ---------------------------------------------------------------


def cmd_investigate(args: argparse.Namespace) -> int:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    from incidentpilot.agent import investigate_alert, make_model, mcp_server_params

    async def run() -> dict:
        client = MultiServerMCPClient({"incidentpilot": mcp_server_params()})
        async with client.session("incidentpilot") as session:
            tools = await load_mcp_tools(session)
            return await investigate_alert(args.alert, make_model(args.model), tools)

    print(f"Investigating with {args.model}: {args.alert}\n")
    state = asyncio.run(run())
    for i, (name, call_args) in enumerate(state["tool_calls"], 1):
        print(f"  step {i:>2}: {name}({json.dumps(call_args)})")
    report = state["report"]
    print("\nRoot-cause report:")
    print(report.model_dump_json(indent=2) if report else "  (no valid report)")
    if state["problems"]:
        print("\nVerification problems:", *state["problems"], sep="\n  - ")
    u = state["usage"]
    print(f"\nTokens: {u['input_tokens']} in / {u['output_tokens']} out, {len(state['tool_calls'])} tool calls")
    return 0


# -- eval ------------------------------------------------------------------------


def cmd_eval(args: argparse.Namespace) -> int:
    from incidentpilot import evals

    dataset = Path(args.dataset) if args.dataset else evals.default_dataset_dir()
    if args.eval_cmd == "generate":
        def progress(i: int, n: int, case: dict) -> None:
            print(f"  [{i}/{n}] {case['case_id']}", flush=True)

        print(f"Recording {args.n} incidents into {dataset}")
        asyncio.run(evals.generate_dataset(dataset, args.n, seed=args.seed, on_case=progress))
        return 0

    def progress(row: dict) -> None:
        mark = "ERR" if row["error"] else ("ok " if row["correct"] else "x  ")
        print(f"  {mark} {row['case_id']:<32} {row['tool_calls']:>2} calls {row['latency_s']:>6}s", flush=True)

    print(f"Evaluating {args.model} on {dataset}")
    summary, rows = asyncio.run(evals.run_eval(
        dataset, args.model, concurrency=args.concurrency, limit=args.limit, judge_model=args.judge, on_result=progress,
    ))
    path = evals.write_results(summary, rows)
    print("\n" + evals.results_markdown([summary]))
    print(f"Saved {path} and evals/RESULTS.md")
    if args.min_accuracy is not None and summary["accuracy"] < args.min_accuracy:
        print(f"FAIL: accuracy {summary['accuracy']:.0%} is below --min-accuracy {args.min_accuracy:.0%}", file=sys.stderr)
        return 1
    return 0


# -- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incidentpilot", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start the three ShopDemo services")
    up.add_argument("--fresh", action="store_true", help="delete old logs, metrics and ground truth first")
    up.set_defaults(func=cmd_up)

    traffic = sub.add_parser("traffic", help="send fake shopper traffic to the frontend")
    traffic.add_argument("--rps", type=float, default=5.0)
    traffic.add_argument("--duration", type=float, default=0, help="seconds; 0 runs until Ctrl+C")
    traffic.add_argument("--seed", type=int)
    traffic.set_defaults(func=cmd_traffic)

    chaos = sub.add_parser("chaos", help="break ShopDemo on purpose")
    chaos_sub = chaos.add_subparsers(dest="chaos_cmd", required=True)
    chaos_sub.add_parser("list", help="show the faults you can inject")
    inject = chaos_sub.add_parser("inject", help="inject a fault and record its ground truth")
    inject.add_argument("fault", choices=[k.value for k in FaultKind])
    inject.add_argument("--noise", action="store_true", help="also turn on red-herring warnings in another service")
    inject.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    chaos_sub.add_parser("clear", help="reset every service")
    chaos_sub.add_parser("status", help="show what each service is serving")
    chaos_sub.add_parser("history", help="print the recorded ground truth")
    chaos.set_defaults(func=cmd_chaos)

    logs = sub.add_parser("logs", help="show recent logs from all services")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.add_argument("-s", "--service", choices=SERVICES)
    logs.add_argument("--severity", choices=SEVERITY_ORDER, default="INFO", help="minimum severity")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    mcp = sub.add_parser("mcp", help="run the IncidentPilot MCP server (stdio by default)")
    mcp.add_argument("--http", action="store_true", help="serve streamable HTTP on :8000 instead")
    mcp.set_defaults(func=cmd_mcp)

    approve = sub.add_parser("approve", help="mint a 10-minute approval token for one rollback")
    approve.add_argument("service", choices=SERVICES)
    approve.add_argument("to_revision")
    approve.set_defaults(func=cmd_approve)

    inv = sub.add_parser("investigate", help="run the agent on the current incident")
    inv.add_argument("--model", default=os.environ.get("INCIDENTPILOT_MODEL", "google_vertexai:gemini-2.5-flash"),
                     help="'baseline' (offline) or a LangChain provider:model string")
    inv.add_argument("--alert", default="High 5xx error rate on frontend POST /checkout")
    inv.set_defaults(func=cmd_investigate)

    ev = sub.add_parser("eval", help="record incidents and score the agent on them")
    ev_sub = ev.add_subparsers(dest="eval_cmd", required=True)
    gen = ev_sub.add_parser("generate", help="record a dataset of incidents with known root causes")
    gen.add_argument("-n", type=int, default=150)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--dataset", help="output folder (default var/evals/dataset)")
    run = ev_sub.add_parser("run", help="replay the dataset to the agent and score it")
    run.add_argument("--model", default="baseline")
    run.add_argument("--limit", type=int, help="only the first N cases")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--judge", help="model that grades each summary 1-5 (LLM-as-judge)")
    run.add_argument("--min-accuracy", type=float, help="exit 1 below this accuracy (CI gate)")
    run.add_argument("--dataset", help="dataset folder (default var/evals/dataset)")
    ev.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
