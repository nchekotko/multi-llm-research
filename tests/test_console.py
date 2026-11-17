import threading

from fastapi.testclient import TestClient

from application.api import create_app
from application.console import ConsoleJobs, JobRequest
from application.demo import demo_config

TOKEN = "console-test-" + "x" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}


def test_console_assets_auth_and_job_validation(tmp_path):
    app = create_app(tmp_path, demo_config(), token=TOKEN, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert "Signal Lab" in client.get("/").text
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/v1/console/overview").status_code == 401
        assert client.get("/v1/console/jobs").status_code == 401
        bad = {"kind": "scan_replay", "parameters": {"identity": "../private-value"}}
        assert client.post("/v1/console/jobs", json=bad).status_code == 401
        rejected = client.post("/v1/console/jobs", headers=HEADERS, json=bad)
        assert rejected.status_code == 422
        assert "private-value" not in rejected.text
        request = {"kind": "scan_replay", "parameters": {"identity": "a" * 64}}
        first = client.post("/v1/console/jobs", headers=HEADERS, json=request)
        second = client.post("/v1/console/jobs", headers=HEADERS, json=request)
        assert first.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert len(client.get("/v1/console/jobs", headers=HEADERS).json()) == 1


def test_worker_persists_result_and_sanitizes_failure(tmp_path, monkeypatch):
    jobs = ConsoleJobs(tmp_path)
    request = JobRequest(kind="scan_replay", parameters={"identity": "a" * 64})
    jobs.submit(request)
    stop = threading.Event()

    def fail(*args):
        stop.set()
        raise ValueError("secret-provider-value")

    monkeypatch.setattr("application.console.verify_scan", fail)
    jobs.work(stop)
    saved = ConsoleJobs(tmp_path).list()[0]
    assert saved["status"] == "failed"
    assert "secret-provider-value" not in saved["error"]
    assert saved["finished_at"]


def test_restart_marks_running_and_retains_queued(tmp_path):
    from application.algopack import save_json

    jobs = ConsoleJobs(tmp_path)
    running = jobs.submit(JobRequest(kind="scan_replay", parameters={"identity": "a" * 64}))
    queued = jobs.submit(JobRequest(kind="scan_replay", parameters={"identity": "b" * 64}))
    running["status"] = "running"
    save_json(jobs.root / (running["id"] + ".json"), running)
    restored = {j["id"]: j for j in ConsoleJobs(tmp_path).list()}
    assert restored[running["id"]]["status"] == "interrupted"
    assert restored[queued["id"]]["status"] == "queued"
