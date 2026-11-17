from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .config import load_config
from .contracts import CONTRACTS, TaskSpec
from .engine import Engine
from .providers import ProviderFailure
from .replay import replay
from .security import PolicyDenied, safe_error
from .storage import BudgetExceeded, Conflict, Store
from .validation import GateFailed


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def export_run(engine: Engine, run_id: str, destination: Path):
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    store = engine.store
    run = store.run(run_id)
    # Local export may contain sensitive sources. It never publishes or uploads.
    (destination / "manifest.json").write_text(
        engine.manifest(run_id).model_dump_json(indent=2), encoding="utf-8"
    )
    (destination / "task.json").write_text(run["task_json"], encoding="utf-8")
    (destination / "config.json").write_text(run["config_json"], encoding="utf-8")
    artifact_dir = destination / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    for meta in store.artifacts(run_id):
        _, body = store.artifact(run_id, meta.artifact_id)
        (artifact_dir / f"{meta.artifact_id}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if run["final_id"]:
        body = store.artifact(run_id, run["final_id"])[1]
        (destination / "report.md").write_text(body["markdown"], encoding="utf-8")
        (destination / "package.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return {"run_id": run_id, "exported_to": str(destination)}


def parser():
    result = argparse.ArgumentParser(prog="multi-llm", description="Durable multi-provider workflow core")
    result.add_argument("--root", default="var", help="Local durable state directory")
    result.add_argument("--config", help="Versioned role/model JSON; default: offline demo")
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("run", "submit"):
        sub = commands.add_parser(command)
        sub.add_argument("task", type=Path)
        sub.add_argument("--idempotency-key")
    sub = commands.add_parser("demo")
    sub.add_argument("--task", type=Path, default=Path("examples/task.json"))
    sub.add_argument("--output", type=Path, default=Path("output/demo"))
    commands.add_parser("list")
    commands.add_parser("research-catalog")
    commands.add_parser("research-futures")
    for command in ("algopack-download", "liquidity-backtest", "market-scan"):
        commands.add_parser(command).add_argument("spec", type=Path)
    for command in ("liquidity-replay", "market-scan-replay"):
        commands.add_parser(command).add_argument("identity")
    sub = commands.add_parser("research-hypotheses")
    sub.add_argument("--include-text", action="store_true")
    for command in ("status", "cancel", "resume", "manifest", "replay"):
        commands.add_parser(command).add_argument("run_id")
    for command in ("approve", "reject"):
        sub = commands.add_parser(command)
        sub.add_argument("run_id")
        sub.add_argument("--sha256", required=True)
        sub.add_argument("--actor", required=True)
    sub = commands.add_parser("export")
    sub.add_argument("run_id")
    sub.add_argument("destination", type=Path)
    sub = commands.add_parser("work")
    sub.add_argument("--once", action="store_true")
    sub = commands.add_parser("serve")
    sub.add_argument("--port", type=int, default=8088)
    sub = commands.add_parser("schemas")
    sub.add_argument("--output", type=Path, default=Path("schemas"))
    sub = commands.add_parser("eval")
    sub.add_argument("--task", type=Path, default=Path("examples/task.json"))
    sub.add_argument("--output", type=Path, default=Path("output/evaluations"))
    sub = commands.add_parser("sandbox")
    sub.add_argument("code", type=Path)
    sub.add_argument("--input", type=Path)
    sub = commands.add_parser(
        "openrouter-setup", help="Four real contract probes, then save a public-data pilot config"
    )
    sub.add_argument("--output", type=Path, default=Path("config/openrouter.json"))
    sub.add_argument("--max-cost-usd", type=Decimal, default=Decimal("2"))
    sub.add_argument("--planner-model", default="openai/gpt-6-astra")
    sub.add_argument("--executor-model", default="anthropic/claude-sonnet-5.5")
    sub.add_argument("--planner-upstream", default="openai")
    sub.add_argument("--executor-upstream", default="anthropic")
    return result


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command in {
            "algopack-download",
            "liquidity-backtest",
            "market-scan",
            "liquidity-replay",
            "market-scan-replay",
        }:
            from .algopack import PROJECT, AlgoPack, DownloadSpec
            from .liquidity import LiquiditySpec, run_liquidity, verify_study
            from .market_scanner import ScanSpec, run_scan, verify_scan

            if args.command == "algopack-download":
                client = AlgoPack()
                try:
                    manifest = client.download(
                        DownloadSpec.model_validate_json(args.spec.read_text(encoding="utf-8")), print_json
                    )
                    print_json({"dataset_id": manifest["dataset_id"], "products": manifest["products"]})
                finally:
                    client.close()
            elif args.command == "liquidity-backtest":
                print_json(
                    run_liquidity(LiquiditySpec.model_validate_json(args.spec.read_text(encoding="utf-8")))
                )
            elif args.command == "market-scan":
                print_json(run_scan(ScanSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))))
            elif args.command == "liquidity-replay":
                print_json(verify_study(PROJECT / "var/algopack", args.identity))
            else:
                print_json(verify_scan(PROJECT / "var/algopack", args.identity))
            return 0
        if args.command == "openrouter-setup":
            from .openrouter_setup import setup_openrouter

            print_json(
                setup_openrouter(
                    args.output,
                    budget=args.max_cost_usd,
                    planner_model=args.planner_model,
                    executor_model=args.executor_model,
                    planner_upstream=args.planner_upstream,
                    executor_upstream=args.executor_upstream,
                )
            )
            return 0
        if args.command == "schemas":
            from .algopack import DownloadSpec
            from .liquidity import LiquiditySpec
            from .market_scanner import ScanSpec

            contracts = {**CONTRACTS, **{c.__name__: c for c in (DownloadSpec, LiquiditySpec, ScanSpec)}}
            args.output.mkdir(parents=True, exist_ok=True)
            for name, contract in contracts.items():
                (args.output / f"{name}.json").write_text(
                    json.dumps(contract.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8"
                )
            print_json({"schemas": len(contracts), "directory": str(args.output.resolve())})
            return 0
        config = load_config(args.config)
        if args.command in {"research-catalog", "research-futures", "research-hypotheses"}:
            from .economic_calendar import EconomicCalendar

            connector = EconomicCalendar()
            value = (
                connector.catalog()
                if args.command == "research-catalog"
                else connector.futures()
                if args.command == "research-futures"
                else connector.hypotheses(args.include_text)
            )
            print_json(value)
            return 0
        if args.command == "serve":
            import uvicorn

            from .api import create_app

            token = os.environ.get("MULTILLM_API_TOKEN")
            if not token:
                token = secrets.token_urlsafe(32)
                print("Local API bearer token (save privately): " + token, flush=True)
            # This CLI intentionally only binds to loopback; public hosting needs a separate deployment.
            uvicorn.run(
                create_app(args.root, config, token), host="127.0.0.1", port=args.port, access_log=False
            )
            return 0
        store = Store(args.root)
        engine = Engine(store, config)
        if args.command in {"run", "submit", "demo"}:
            task = TaskSpec.model_validate_json(args.task.read_text(encoding="utf-8"))
            run_id = engine.submit(task, getattr(args, "idempotency_key", None))
            if args.command != "submit":
                engine.run(run_id)
            run = store.run(run_id)
            output = {"run_id": run_id, "status": run["status"], "reason": run["reason"]}
            if run["final_id"]:
                output["package_sha256"] = store.artifact(run_id, run["final_id"])[0].sha256
            if args.command == "demo":
                output.update(export_run(engine, run_id, args.output))
                output["replay"] = replay(store, run_id)
            print_json(output)
            if run["status"] not in {"queued", "running", "waiting_approval", "completed"}:
                return 2
        elif args.command == "list":
            print_json(store.list_runs())
        elif args.command == "status":
            run = store.run(args.run_id)
            print_json(
                {k: run[k] for k in ("id", "status", "reason", "final_id", "created_at", "updated_at")}
            )
        elif args.command == "cancel":
            store.cancel(args.run_id)
            print_json({"status": store.run(args.run_id)["status"]})
        elif args.command == "resume":
            store.resume(args.run_id)
            print_json({"status": engine.run(args.run_id)["status"]})
        elif args.command == "manifest":
            print(engine.manifest(args.run_id).model_dump_json(indent=2))
        elif args.command == "replay":
            print_json(replay(store, args.run_id))
        elif args.command in {"approve", "reject"}:
            if args.command == "approve":
                replay(store, args.run_id)
            store.approval(args.run_id, args.sha256, args.actor, args.command == "approve")
            print_json({"status": store.run(args.run_id)["status"]})
        elif args.command == "export":
            print_json(export_run(engine, args.run_id, args.destination))
        elif args.command == "eval":
            from .evaluations import evaluate

            report = evaluate(
                args.output, TaskSpec.model_validate_json(args.task.read_text(encoding="utf-8"))
            )
            print_json(
                {
                    "report": str((args.output / "comparison.md").resolve()),
                    "cases": len(report["rows"]),
                    "ac11_demonstrated": False,
                }
            )
        elif args.command == "sandbox":
            from .sandbox import DockerSandbox

            data = json.loads(args.input.read_text(encoding="utf-8")) if args.input else None
            print_json(
                asdict(
                    DockerSandbox(config.sandbox_image, config.sandbox_enabled).execute(
                        args.code.read_text(encoding="utf-8"), data, allowed=True
                    )
                )
            )
        elif args.command == "work":
            while True:
                for run_id in store.runnable():
                    engine.run(run_id)
                if args.once:
                    break
                time.sleep(0.5)
        return 0
    except (
        ValueError,
        KeyError,
        Conflict,
        PolicyDenied,
        OSError,
        ProviderFailure,
        BudgetExceeded,
        GateFailed,
    ) as exc:
        # Validation errors can echo sensitive rejected input. Print only an error class.
        print_json(
            {
                "error": safe_error(exc),
                "message": (
                    str(exc)
                    if args.command == "openrouter-setup"
                    and isinstance(exc, (PolicyDenied, Conflict, ProviderFailure, BudgetExceeded, GateFailed))
                    else "Operation failed. Check task schema, policy and run state."
                ),
            }
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
