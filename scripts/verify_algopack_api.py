"""Read-only verification of running servers and existing AlgoPack artifacts."""
import hashlib
import json
from pathlib import Path

import httpx


def main():
    project = Path(__file__).resolve().parents[1]
    study = "7fd717d355aa50623c980ef1e5d74408063ebf6145f9b4bc133359b8c07e2077"
    scan = "f7528023102a65dafd799d91ee37aaeccb8eaa30c4bcaeecae72bb8887d64efe"
    results = []
    for port, state in [(8088, "var"), (8089, "var/openrouter")]:
        token = (project / state / "api-token.txt").read_text(encoding="utf-8").strip()
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False) as client:
            assert client.get("/v1/algopack/datasets").status_code == 401
            client.headers["Authorization"] = "Bearer " + token
            datasets = client.get("/v1/algopack/datasets")
            datasets.raise_for_status()
            assert len(datasets.json()) >= 2
            studies = client.get("/v1/algopack/studies/" + study)
            studies.raise_for_status()
            scans = client.get("/v1/algopack/scans/" + scan)
            scans.raise_for_status()
            assert studies.json()["decision"] == "inconclusive"
            assert scans.json()["events_count"] == 563
            schema = client.get("/openapi.json").json()
            assert "/v1/algopack/market-scan" in schema["paths"]
            assert "/v1/algopack/liquidity-backtest" in schema["paths"]
            results.append({"port": port, "auth": "passed", "datasets": len(datasets.json()),
                            "study_result": "passed", "scan_result": "passed", "new_routes": "passed"})
    destination = project / "output/algopack-20261117"
    destination.mkdir(parents=True, exist_ok=True)
    copies = {
        "liquidity-report.md": project / "var/algopack/studies" / study / "report.md",
        "liquidity-result.json": project / "var/algopack/studies" / study / "result.json",
        "scanner-report.md": project / "var/algopack/scans" / scan / "report.md",
        "scanner-result.json": project / "var/algopack/scans" / scan / "result.json",
    }
    hashes = {}
    for name, source in copies.items():
        raw = source.read_bytes()
        (destination / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    verification = {"servers": results, "exports_sha256": hashes, "model_calls": 0}
    (destination / "verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
    print(json.dumps(verification, indent=2))


if __name__ == "__main__":
    main()
