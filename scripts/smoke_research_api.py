"""Local real-data/API smoke. --live explicitly enables two paid model roles."""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    from application.cli import export_run
    from application.config import load_config
    from application.engine import Engine
    from application.storage import Store

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send only the public computed summary to two model families, under $1",
    )
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument(
        "--experiment-tag", default="v1", help="New tag creates a separate, fully recorded control experiment"
    )
    args = parser.parse_args()
    if not args.experiment_tag.replace("-", "").replace("_", "").isalnum() or len(args.experiment_tag) > 64:
        parser.error("experiment-tag must contain 1..64 letters, digits, underscores or hyphens")
    token = (PROJECT / "var/openrouter/api-token.txt").read_text(encoding="utf-8").strip()
    example = "moex-spread-openrouter.json" if args.live else "moex-spread.json"
    task = json.loads((PROJECT / "examples" / example).read_text(encoding="utf-8"))
    name = "moex-openrouter-reference" if args.live else "moex-si-reference"
    output = PROJECT / "output" / (name + "-" + args.experiment_tag)
    output.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url=f"http://127.0.0.1:{args.port}",
        headers={"Authorization": "Bearer " + token},
        timeout=20,
        trust_env=False,
    ) as client:
        client.get("/health").raise_for_status()
        for name in ("catalog", "futures", "hypotheses"):
            response = client.get("/v1/research/" + name)
            response.raise_for_status()
            (output / f"{name}.json").write_text(
                json.dumps(response.json(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        assert client.get("/v1/research/preview/users/users").status_code == 403
        response = client.post(
            "/v1/runs",
            json=task,
            headers={
                "Idempotency-Key": "moex-reference-20261116-" + str(args.live) + "-" + args.experiment_tag
            },
        )
        response.raise_for_status()
        run_id = response.json()["run_id"]
        print(json.dumps({"run_id": run_id, "live_model_roles": args.live}), flush=True)
        deadline = time.monotonic() + 320
        while True:
            response = client.get(f"/v1/runs/{run_id}")
            response.raise_for_status()
            state = response.json()
            if state["status"] not in {"queued", "running"}:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "research smoke exceeded deadline; inspect the existing run, do not resubmit"
                )
            time.sleep(1)
        if state["status"] != "waiting_approval":
            raise RuntimeError(json.dumps(state))
        replay = client.post(f"/v1/runs/{run_id}/replay")
        replay.raise_for_status()
        manifest = client.get(f"/v1/runs/{run_id}/manifest")
        manifest.raise_for_status()
    engine = Engine(Store(PROJECT / "var/openrouter"), load_config(PROJECT / "config/openrouter.json"))
    export_run(engine, run_id, output)
    summary = {
        "run_id": run_id,
        "status": state["status"],
        "replay": replay.json(),
        "accounting": manifest.json()["accounting"],
        "report": str(output / "report.md"),
    }
    (output / "verification.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
