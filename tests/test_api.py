import json

from fastapi.testclient import TestClient

from application.api import create_app
from application.demo import demo_config

TOKEN = "test-token-" + "a" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}


def test_api_requires_auth_and_deduplicates(tmp_path, task):
    app = create_app(tmp_path, demo_config(), token=TOKEN, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/runs").status_code == 401
        assert client.post("/v1/runs", json=task.model_dump(mode="json")).status_code == 401
        headers = {**HEADERS, "Idempotency-Key": "request-001"}
        first = client.post("/v1/runs", headers=headers, json=task.model_dump(mode="json"))
        assert first.status_code == 202
        second = client.post("/v1/runs", headers=headers, json=task.model_dump(mode="json"))
        run_id = first.json()["run_id"]
        assert second.json()["run_id"] == run_id
        app.state.engine.run(run_id)
        result = client.get(f"/v1/runs/{run_id}/result", headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["run_status"] == "waiting_approval"
        approved = client.post(
            f"/v1/runs/{run_id}/approval",
            headers=HEADERS,
            json={
                "package_sha256": result.json()["package_sha256"],
                "actor": "test_operator",
                "accept": True,
            },
        )
        assert approved.json()["status"] == "completed"
        assert client.post(f"/v1/runs/{run_id}/replay", headers=HEADERS).json()["status"] == "passed"
        assert client.get("/v1/runs/missing", headers=HEADERS).status_code == 404


def test_artifacts_scoped_to_run(tmp_path, task):
    app = create_app(tmp_path, demo_config(), token=TOKEN, start_worker=False)
    with TestClient(app) as client:
        first = app.state.engine.submit(task)
        second = app.state.engine.submit(task)
        app.state.engine.run(first)
        aid = app.state.engine.store.artifacts(first)[0].artifact_id
        assert client.get(f"/v1/runs/{second}/artifacts/{aid}", headers=HEADERS).status_code == 404


def test_validation_errors_do_not_echo_secrets(tmp_path, task):
    app = create_app(tmp_path, demo_config(), token=TOKEN, start_worker=False)
    with TestClient(app) as client:
        value = task.model_dump(mode="json")
        value.pop("constraints")
        value["goal"] = "private-value-do-not-echo"
        response = client.post("/v1/runs", headers=HEADERS, json=value)
        assert response.status_code == 422
        assert "private-value-do-not-echo" not in json.dumps(response.json())
