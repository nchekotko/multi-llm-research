"""Synthetic HTTP -> validation -> disk -> console API regression scenarios."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from application.algopack import AlgoPack, DownloadSpec, read_dataset
from application.api import create_app
from application.config import digest
from application.demo import demo_config

TOKEN = "pipeline-test-" + "x" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}
COLUMNS = ["secid", "tradedate", "tradetime", "vol"]
SPEC = {
    "symbols": ["SiM6"],
    "start": "2026-04-01",
    "end": "2026-04-03",
    "products": ["tradestats"],
}


class OneJob:
    """Run one worker iteration synchronously, without clocks or background threads."""

    def __init__(self):
        self.calls = 0

    def wait(self, timeout):
        self.calls += 1
        return self.calls > 1


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr("application.algopack.PROJECT", tmp_path)
    monkeypatch.setattr("application.console.PROJECT", tmp_path)
    monkeypatch.setattr("application.algopack.time.sleep", lambda _: None)

    def setup(handler):
        def initialize(self, root=None):
            self.root = tmp_path / "var/algopack"
            self.client = httpx.Client(
                base_url="https://example.test/iss/", transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(AlgoPack, "__init__", initialize)
        return TestClient(create_app(tmp_path / "state", demo_config(), token=TOKEN, start_worker=False))

    return setup


def submit_and_run(client, spec):
    response = client.post(
        "/v1/console/jobs", headers=HEADERS, json={"kind": "download", "parameters": spec}
    )
    assert response.status_code == 202
    client.app.state.console_jobs.work(OneJob())
    jobs = client.get("/v1/console/jobs", headers=HEADERS).json()
    return next(job for job in jobs if job["id"] == response.json()["id"])


@pytest.mark.parametrize("invalid_data", [None, {}, "", False, 0])
def test_malformed_empty_page_never_becomes_visible_snapshot(pipeline, tmp_path, invalid_data):
    def handler(request):
        return httpx.Response(200, json={"data": {"columns": COLUMNS, "data": invalid_data}})

    with pipeline(handler) as client:
        job = submit_and_run(client, SPEC)
        assert job["status"] == "failed"
        assert client.get("/v1/console/overview", headers=HEADERS).json()["datasets"] == []
        assert not list((tmp_path / "var/algopack/datasets").glob("*/manifest.json"))


def test_daily_market_response_must_match_requested_day(pipeline):
    def handler(request):
        # The server incorrectly returns April 2 for the April 1 request,
        # then no rows for April 2. Global range and duplicate checks cannot detect this.
        data = []
        if request.url.params["date"] == "2026-04-01" and request.url.params["start"] == "0":
            data = [["SiM6", "2026-04-02", "10:05:00", 7]]
        return httpx.Response(200, json={"data": {"columns": COLUMNS, "data": data}})

    with pipeline(handler) as client:
        spec = {**SPEC, "symbols": [], "whole_futures_market": True}
        job = submit_and_run(client, spec)
        assert job["status"] == "failed"
        assert client.get("/v1/console/overview", headers=HEADERS).json()["datasets"] == []


@pytest.mark.parametrize(
    "columns,data",
    [
        (["n", "n"], [[1, 2]]),
        ("n", [[1]]),
        ([None], [[1]]),
        (["n"], ["x"]),
        (["n"], [[1, 2]]),
        ([], [[]]),
    ],
)
def test_table_rejects_ambiguous_columns_and_non_array_rows(columns, data):
    client = AlgoPack.__new__(AlgoPack)
    client.page = lambda *args: {"payload": {"data": {"columns": columns, "data": data}}}
    with pytest.raises(ValueError, match="invalid AlgoPack table"):
        client.table("test", "data", {}, max_pages=1)


def test_valid_empty_market_day_remains_a_success(pipeline):
    def handler(request):
        return httpx.Response(200, json={"data": {"columns": COLUMNS, "data": []}})

    with pipeline(handler) as client:
        job = submit_and_run(client, SPEC)
        assert job["status"] == "completed"
        saved = client.get("/v1/console/overview", headers=HEADERS).json()["datasets"][0]
        assert saved["products"]["tradestats"]["rows"] == 0


def test_download_retry_pagination_storage_and_console_result(pipeline, tmp_path):
    requests = []

    def handler(request):
        requests.append(int(request.url.params["start"]))
        if len(requests) == 1:
            return httpx.Response(503)
        offset = requests[-1]
        data = [["SiM6", "2026-04-01", "10:05:00", 7]] if offset == 0 else []
        return httpx.Response(200, json={"data": {"columns": COLUMNS, "data": data}})

    with pipeline(handler) as client:
        job = submit_and_run(client, SPEC)
        assert job["status"] == "completed"
        identity = digest(DownloadSpec.model_validate(SPEC).model_dump(mode="json"))
        manifest, data = read_dataset(tmp_path / "var/algopack", identity)
        assert data["tradestats"][0]["vol"] == 7
        assert requests == [0, 0, 1]
        overview = client.get("/v1/console/overview", headers=HEADERS).json()
        saved = overview["datasets"][0]
        assert saved["dataset_id"] == job["result"]["dataset_id"] == identity
        assert saved["products"]["tradestats"]["rows"] == 1
        assert saved["products"]["tradestats"]["first"] == "2026-04-01 10:05:00"
        assert "receipts" not in saved
        assert client.get("/v1/algopack/datasets", headers=HEADERS).json() == [saved]
        # Repeating a completed request must preserve the snapshot without HTTP calls.
        assert submit_and_run(client, SPEC)["status"] == "completed"
        assert requests == [0, 0, 1]
        assert read_dataset(tmp_path / "var/algopack", identity)[0] == manifest
        raw = tmp_path / "var/algopack/datasets" / identity / "tradestats.json"
        raw.write_text(json.dumps([]), encoding="utf-8")
        with pytest.raises(ValueError, match="content mismatch"):
            read_dataset(tmp_path / "var/algopack", identity)
        failed = submit_and_run(client, SPEC)
        assert failed["error_code"] == "snapshot_integrity"
        assert requests == [0, 0, 1]


@pytest.mark.parametrize(
    "status,code,fragment,attempts",
    [
        (401, "unauthorized", "ключ", 1),
        (403, "forbidden", "подписку", 1),
        (429, "rate_limited", "Подождите", 3),
        (503, "source_unavailable", "позже", 3),
        (404, "http_error", "инструменты", 1),
    ],
)
def test_http_failures_have_safe_actionable_messages(pipeline, tmp_path, status, code, fragment, attempts):
    calls = []
    secret = "SECRET-provider-body-and-path"

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=secret)

    with pipeline(handler) as client:
        job = submit_and_run(client, SPEC)
        assert job["status"] == "failed"
        assert job["error_code"] == code
        assert fragment in job["error"]
        assert len(calls) == attempts
        overview = client.get("/v1/console/overview", headers=HEADERS).json()
        assert overview["jobs"][0]["error"] == job["error"]
        assert secret not in json.dumps(overview)
        saved = tmp_path / "state/console-jobs" / (job["id"] + ".json")
        assert secret not in saved.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "error,code",
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network"), (RuntimeError, "unexpected")],
)
def test_transport_and_unexpected_errors_do_not_leak_details(pipeline, error, code):
    def handler(request):
        raise error("SECRET-auth-header-and-local-path")

    with pipeline(handler) as client:
        job = submit_and_run(client, SPEC)
        assert job["error_code"] == code
        assert "SECRET" not in json.dumps(job)
        assert job["status"] == "failed"


def test_cache_corruption_reports_repair_instead_of_retry(pipeline, tmp_path):
    def handler(request):
        return httpx.Response(200, json={"data": {"columns": COLUMNS, "data": None}})

    with pipeline(handler) as client:
        first = submit_and_run(client, SPEC)
        assert first["error_code"] == "invalid_response"
        cached = next((tmp_path / "var/algopack/pages").glob("*.json"))
        cached.write_text("SECRET-invalid-json", encoding="utf-8")
        second = submit_and_run(client, SPEC)
        assert second["error_code"] == "cache_integrity"
        assert "обычный повтор не устранит" in second["error"]
        assert "SECRET" not in json.dumps(second)


@pytest.mark.parametrize("config", [None, [], {}, {"credential_name": []}])
def test_invalid_configuration_has_safe_specific_error(tmp_path, monkeypatch, config):
    from application.algopack import DownloadError, download_failure

    monkeypatch.setattr("application.algopack.PROJECT", tmp_path)
    if config is not None:
        (tmp_path / "config").mkdir()
        (tmp_path / "config/algopack.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(DownloadError) as caught:
        AlgoPack()
    code, message = download_failure(caught.value)
    assert code == "configuration"
    assert str(tmp_path) not in message


@pytest.mark.parametrize("error,code", [
    (PermissionError("SECRET-path"), "storage_permission"),
    (OSError(28, "SECRET-path"), "storage_full"),
    (OSError("SECRET-path"), "storage"),
])
def test_storage_errors_are_safe_and_actionable(pipeline, monkeypatch, error, code):
    def handler(request):
        return httpx.Response(200, json={"data": {"columns": COLUMNS, "data": []}})

    def fail_download(*args):
        raise error

    with pipeline(handler) as client:
        monkeypatch.setattr(AlgoPack, "download", fail_download)
        job = submit_and_run(client, SPEC)
        assert job["error_code"] == code
        assert "SECRET" not in json.dumps(job)


@pytest.mark.parametrize("with_key,code", [(False, "credentials"), (True, "tls_configuration")])
def test_missing_key_and_invalid_ca_have_separate_messages(tmp_path, monkeypatch, with_key, code):
    from application.algopack import DownloadError, download_failure

    monkeypatch.setattr("application.algopack.PROJECT", tmp_path)
    monkeypatch.delenv("PIPELINE_TEST_MOEX_KEY", raising=False)
    if with_key:
        monkeypatch.setenv("PIPELINE_TEST_MOEX_KEY", "SECRET-test-key")
    (tmp_path / "config").mkdir()
    config = {
        "credential_name": "PIPELINE_TEST_MOEX_KEY",
        "credential_file": str(tmp_path / "SECRET-missing-credential-file"),
        "ca_bundle": str(tmp_path / "SECRET-missing-ca"),
    }
    (tmp_path / "config/algopack.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(DownloadError) as caught:
        AlgoPack()
    actual, message = download_failure(caught.value)
    assert actual == code
    assert "SECRET" not in message
