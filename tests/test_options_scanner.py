import json
from datetime import datetime, timedelta, timezone

import pytest

from application.options_scanner import (
    MSK,
    OptionsScanner,
    OptionsSettings,
    SettingsChange,
    book,
    fill,
    package_cost,
)


def record(bid=10, ask=11, depth=5, age=1, received=None):
    received = received or datetime.now(timezone.utc)
    stamp = received.astimezone(MSK) - timedelta(seconds=age)
    return {
        "received_at": received.isoformat(),
        "started_at": received.isoformat(),
        "payload": {
            "dataversion": {
                "columns": ["trade_date", "seqnum"],
                "data": [[stamp.date().isoformat(), int(stamp.strftime("%Y%m%d%H%M%S"))]],
            },
            "orderbook": {
                "columns": ["BUYSELL", "PRICE", "QUANTITY"],
                "data": [["B", bid, depth], ["S", ask, depth]],
            },
        },
    }


def leg(secid="a", quantity=1):
    return {"secid": secid, "quantity": quantity, "price_point_rub": 2, "fee": 0.2, "stepprice": 0.1}


def test_depth_and_correct_sides():
    settings = OptionsSettings()
    assert package_cost([leg()], {"a": record()}, settings) == 22
    assert package_cost([leg()], {"a": record()}, settings, reverse=True) == -20
    assert fill([(10, 2), (11, 3)], 4) == 42
    with pytest.raises(ValueError, match="depth"):
        fill([(10, 2)], 3)


def test_reject_stale_crossed_and_asynchronous_books():
    settings = OptionsSettings()
    with pytest.raises(ValueError, match="stale_feed"):
        book(record(age=50), settings)
    with pytest.raises(ValueError, match="crossed"):
        book(record(bid=12, ask=11), settings)
    with pytest.raises(ValueError, match="asynchronous"):
        package_cost(
            [leg("a"), leg("b")],
            {"a": record(), "b": record(received=datetime.now(timezone.utc) - timedelta(seconds=10))},
            settings,
        )


def seed(scanner, age=10, horizon=30, broker=None):
    data = {
        "at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
        "cycle": "signal",
        "legs": [leg()],
        "capital_proxy_rub": 100,
        "settings": OptionsSettings(horizon_seconds=horizon, broker_rub_per_contract=broker).model_dump(),
    }
    with scanner.connect() as db:
        db.execute("INSERT INTO evaluations VALUES('x','x','await_entry',?)", (json.dumps(data),))


def outcome(scanner):
    with scanner.connect() as db:
        row = db.execute("SELECT * FROM evaluations WHERE id='x'").fetchone()
        return row["state"], json.loads(row["body"])


def test_no_same_snapshot_execution_and_gap_not_zero_pnl(tmp_path):
    scanner = OptionsScanner(tmp_path)
    seed(scanner)
    scanner.evaluate_pending({"a": record()}, "signal")
    state, data = outcome(scanner)
    assert state == "entry_unobservable"
    assert "scenario_pnl_rub" not in data


def test_forward_entry_and_restart_exit_preserve_cost_settings(tmp_path):
    scanner = OptionsScanner(tmp_path)
    seed(scanner)
    scanner.evaluate_pending({"a": record()}, "later")
    state, data = outcome(scanner)
    assert state == "await_exit"
    assert data["simulated_entry_cost"] == 22
    data["entry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
    with scanner.connect() as db:
        db.execute("UPDATE evaluations SET body=?", (json.dumps(data),))
    restarted = OptionsScanner(tmp_path)
    restarted.evaluate_pending({"a": record(bid=13, ask=14)}, "exit")
    state, data = outcome(restarted)
    assert state == "cost_incomplete"
    assert data["gross_pnl"] == 4
    assert 3 < data["scenario_pnl_rub"] < 3.4


def test_settings_revisions_and_storage_guard(tmp_path):
    scanner = OptionsScanner(tmp_path)
    scanner.configure(SettingsChange(revision=0, settings=OptionsSettings(enabled=True)))
    with pytest.raises(ValueError, match="revision"):
        scanner.configure(SettingsChange(revision=0, settings=OptionsSettings()))
    settings = OptionsSettings().model_copy(update={"max_storage_mb": 0})
    with pytest.raises(ValueError, match="storage_limit"):
        scanner.store_cycle("books", {"x": 1}, settings)


def test_early_observation_cannot_fill_after_latency_by_late_processing(tmp_path):
    scanner = OptionsScanner(tmp_path)
    seed(scanner)
    old = datetime.now(timezone.utc) - timedelta(seconds=12)
    scanner.evaluate_pending({"a": record(received=old)}, "later")
    assert outcome(scanner)[0] == "await_entry"


def test_options_api_authorization_revision_and_evidence(tmp_path):
    from fastapi.testclient import TestClient

    from application.api import create_app
    from application.demo import demo_config

    token = "options-test-" + "z" * 32
    app = create_app(tmp_path, demo_config(), token=token, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/v1/options").status_code == 401
        assert client.put("/v1/options/settings", json={}).status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        snapshot = app.state.options_scanner.store_cycle("test", {"hello": "world"}, OptionsSettings())
        result = client.get("/v1/options/cycles/" + snapshot)
        assert result.status_code == 200
        assert result.json()["payload"] == {"hello": "world"}
        assert client.get("/v1/options/cycles/missing").status_code == 404
        config = client.get("/v1/options").json()
        config["settings"]["broker_rub_per_contract"] = 1.2
        update = {"revision": config["revision"], "settings": config["settings"]}
        assert client.put("/v1/options/settings", json=update).status_code == 200
        assert client.put("/v1/options/settings", json=update).status_code == 409


def test_disappeared_signal_cancels_before_entry(tmp_path):
    scanner = OptionsScanner(tmp_path)
    seed(scanner)
    _, data = outcome(scanner)
    data["guaranteed_payoff_pv_scenario_rub"] = 20
    with scanner.connect() as db:
        db.execute("UPDATE evaluations SET body=?", (json.dumps(data),))
    scanner.evaluate_pending({"a": record()}, "later")
    assert outcome(scanner)[0] == "signal_expired"
