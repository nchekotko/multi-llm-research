"""Exercise the real HTTP server, durable worker and replay without external API keys."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main():
    project = Path(__file__).resolve().parent.parent
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "MULTILLM_API_TOKEN": token, "PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryDirectory(prefix="multi-llm-smoke-") as state:
        process = subprocess.Popen(
            [sys.executable, "-m", "application", "--root", state, "serve", "--port", str(port)],
            cwd=project,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        response = client.get("/health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError("HTTP server did not become healthy")
                    time.sleep(0.1)
                assert client.get("/v1/runs").status_code == 401
                headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "smoke-task"}
                task = json.loads((project / "examples/task.json").read_text(encoding="utf-8"))
                created = client.post("/v1/runs", headers=headers, json=task)
                created.raise_for_status()
                run_id = created.json()["run_id"]
                while True:
                    status = client.get(f"/v1/runs/{run_id}", headers=headers).json()["status"]
                    if status not in {"queued", "running"}:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("worker did not finish before deadline")
                    time.sleep(0.1)
                assert status == "waiting_approval", status
                result = client.get(f"/v1/runs/{run_id}/result", headers=headers)
                result.raise_for_status()
                assert result.json()["package"]["numeric_lineage"][0]["value"] == "265000"
                replayed = client.post(f"/v1/runs/{run_id}/replay", headers=headers)
                replayed.raise_for_status()
                assert replayed.json()["status"] == "passed"
                assert replayed.json()["model_calls"] == 0
                print(
                    json.dumps(
                        {
                            "http_server": "passed",
                            "authentication": "passed",
                            "durable_worker": "passed",
                            "result": "passed",
                            "replay": "passed",
                            "external_calls": 0,
                        },
                        indent=2,
                    )
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
