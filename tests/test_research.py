import json
import math
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from application.api import create_app
from application.config import digest
from application.contracts import DatasetSnapshot, ResearchSpec, TaskSpec
from application.demo import demo_config
from application.economic_calendar import EconomicCalendar
from application.engine import Engine
from application.providers import ProviderResult
from application.replay import replay
from application.research import MSK, audit_data, backtest, simulate
from application.security import PolicyDenied
from application.storage import Store
from application.validation import GateFailed


@pytest.fixture
def research_env(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "data").mkdir(parents=True)
    (source / "docs").mkdir()
    registry = source / "docs/strategies-register.md"
    registry.write_text(
        "## Tests\n### Calendar — `ОТКЛОНЕНА`\nNegative result.\n### Trend — `КАНДИДАТ`\nNeeds testing.\n",
        encoding="utf-8",
    )
    db = source / "data/tradfi_1h.db"
    start = int(datetime(2026, 1, 1, tzinfo=MSK).timestamp())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE bars(source TEXT,symbol TEXT,ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL, PRIMARY KEY(source,symbol,ts))"
        )
        for i in range(600):
            for symbol, price in [("SiM6", 10000 + 100 * math.sin(i / 5)), ("SiU6", 10100)]:
                conn.execute(
                    "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                    ("moex", symbol, start + i * 3600, price, price + 3, price - 3, price + 1, 100),
                )
    config = tmp_path / "connector.json"
    config.write_text(json.dumps({"root": str(source)}), encoding="utf-8")
    connector = EconomicCalendar(config)
    monkeypatch.setattr("application.research.EconomicCalendar", lambda: connector)
    monkeypatch.setattr("application.economic_calendar.EconomicCalendar", lambda: connector)
    raw = json.loads((Path(__file__).parents[1] / "examples/moex-spread.json").read_text(encoding="utf-8"))
    raw["research"].update(
        start="2026-01-01", end="2026-02-01", test_start="2026-01-09", lookback=12, entry_z=1
    )
    return connector, TaskSpec.model_validate(raw), db, registry


def replace_series(snapshot, series):
    raw = snapshot.model_dump(mode="json")
    raw["series"] = series
    raw["content_sha256"] = digest(series)
    return DatasetSnapshot.model_validate(raw)


def test_read_only_and_allowlist(research_env):
    connector, _, db, _ = research_env
    before = db.read_bytes()
    with connector.connection("tradfi_1h") as conn:
        for sql in [
            "DELETE FROM bars",
            "CREATE TABLE surprise(x)",
            "ATTACH ':memory:' AS other",
            "PRAGMA query_only=OFF",
        ]:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
    assert db.read_bytes() == before
    with pytest.raises(PolicyDenied):
        connector.preview("users", "users")
    with pytest.raises(PolicyDenied):
        connector.preview("tradfi_1h", "bars; DROP TABLE bars")
    assert len(connector.preview("tradfi_1h", "bars", 3)["rows"]) == 3


def test_registry_pinning_and_rejected_hypothesis(research_env):
    connector, task, _, registry = research_env
    entries = connector.hypotheses(True)
    rejected = entries[0]
    spec = task.research.model_copy(
        update={"hypothesis_id": rejected["hypothesis_id"], "hypothesis_revision": rejected["revision"]}
    )
    with pytest.raises(GateFailed, match="reason"):
        connector.hypothesis(spec)
    spec.revisit_reason = "New bid/ask data changes execution assumptions."
    assert connector.hypothesis(spec)["status"] == "rejected"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace("Negative result.", "Updated result."), encoding="utf-8"
    )
    with pytest.raises(GateFailed, match="revision"):
        connector.hypothesis(spec)


@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2026-01-10"},
        {"entry_z": 0.2, "exit_z": 0.5},
        {"latency_seconds": 0},
        {"flatten_hour_msk": 10},
        {"legs": [{"symbol": "FUT:Si", "quantity": 1, "tick_size": 1, "point_value_rub": 1}]},
        {
            "legs": [
                {"symbol": "SiM6", "quantity": 1, "tick_size": 1, "point_value_rub": 1},
                {"symbol": "RIM6", "quantity": -1, "tick_size": 1, "point_value_rub": 1},
            ]
        },
    ],
)
def test_invalid_protocol(research_env, changes):
    _, task, _, _ = research_env
    with pytest.raises(ValidationError):
        ResearchSpec.model_validate(task.research.model_dump() | changes)


def test_signal_fills_follow_availability_and_charge_both_legs(research_env):
    connector, task, _, _ = research_env
    snapshot = connector.snapshot(task.research)
    result = simulate(snapshot, task.research, test=True)
    assert result["fills_count"] > 0
    for fill in result["fills"]:
        if fill["signal_available_at"] is not None:
            assert fill["ts"] >= fill["signal_available_at"] + task.research.latency_seconds
        assert fill["cost_rub"] == abs(fill["position_after"] - fill["position_before"]) * 14
    expensive = simulate(snapshot, task.research, test=True, cost_factor=4)
    assert expensive["scenario_marked_pnl_rub"] < result["scenario_marked_pnl_rub"]
    assert expensive["cost_rub"] == result["cost_rub"] * 4


def test_future_prices_do_not_change_past_fills(research_env):
    connector, task, _, _ = research_env
    snapshot = connector.snapshot(task.research)
    original = simulate(snapshot, task.research, test=True)
    cutoff = snapshot.series["SiM6"][400].ts
    series = snapshot.model_dump(mode="json")["series"]
    for bars in series.values():
        for bar in bars:
            if bar["ts"] >= cutoff:
                for key in ("open", "high", "low", "close"):
                    bar[key] *= 2
    changed = simulate(replace_series(snapshot, series), task.research, test=True)
    assert [x for x in changed["fills"] if x["ts"] < cutoff] == [
        x for x in original["fills"] if x["ts"] < cutoff
    ]
    assert [x for x in changed["equity"] if x["ts"] < cutoff] == [
        x for x in original["equity"] if x["ts"] < cutoff
    ]


def test_missing_leg_no_forward_fill(research_env):
    connector, task, _, _ = research_env
    snapshot = connector.snapshot(task.research)
    series = snapshot.model_dump(mode="json")["series"]
    missing = {x["ts"] for x in series["SiU6"][240:260]}
    series["SiU6"] = [x for x in series["SiU6"] if x["ts"] not in missing]
    changed = replace_series(snapshot, series)
    audit = audit_data(changed, task.research)
    assert audit.statistics["unmatched_rows"]["SiM6"] == 20
    assert not any(fill["ts"] in missing for fill in simulate(changed, task.research, test=True)["fills"])


def test_strict_execution_blocks_without_fabricating_profit(research_env):
    connector, task, _, _ = research_env
    spec = task.research.model_copy(update={"mode": "execution"})
    snapshot = connector.snapshot(spec)
    audit = audit_data(snapshot, spec)
    result = backtest(snapshot, spec, audit)
    assert result.decision == "blocked"
    assert result.out_of_sample == {}
    assert "bid_ask_depth" in result.blockers


def test_empty_and_corrupted_snapshots_fail(research_env):
    connector, task, _, _ = research_env
    snapshot = connector.snapshot(task.research)
    empty = replace_series(snapshot, {"SiM6": [], "SiU6": []})
    assert audit_data(empty, task.research).status == "blocked"
    raw = snapshot.model_dump(mode="json")
    raw["series"]["SiM6"][0]["volume"] = 200
    with pytest.raises(GateFailed, match="digest"):
        audit_data(DatasetSnapshot.model_validate(raw), task.research)
    series = snapshot.model_dump(mode="json")["series"]
    series["SiM6"].append(series["SiM6"][0])
    with pytest.raises(GateFailed, match="duplicates"):
        audit_data(replace_series(snapshot, series), task.research)


def test_durable_run_replay_after_source_removed(research_env, tmp_path):
    _, task, db, _ = research_env
    engine = Engine(Store(tmp_path / "state"), demo_config())
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "waiting_approval"
    assert len(engine.store.artifacts(run_id)) == 5
    assert engine.store.calls(run_id) == []
    assert engine.manifest(run_id).workflow_version == "moex-reference/1.0"
    db.unlink()
    result = replay(engine.store, run_id)
    assert result["numeric_replay"] == "passed"
    assert result["source_database_reads"] == 0


def test_api_research_catalog_and_access_controls(research_env, tmp_path):
    _, task, _, _ = research_env
    token = "research-test-token-at-least-24"
    with TestClient(create_app(tmp_path / "api", token=token, start_worker=False)) as client:
        assert client.get("/v1/research/catalog").status_code == 401
        headers = {"Authorization": "Bearer " + token}
        assert client.get("/v1/research/catalog", headers=headers).status_code == 200
        assert len(client.get("/v1/research/hypotheses", headers=headers).json()) == 2
        assert client.get("/v1/research/preview/users/users", headers=headers).status_code == 403
        assert (
            client.get("/v1/research/preview/tradfi_1h/bars?limit=1000", headers=headers).status_code == 422
        )
        response = client.post("/v1/runs", headers=headers, json=task.model_dump(mode="json"))
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        client.app.state.engine.run(run_id)
        result = client.get(f"/v1/runs/{run_id}/result", headers=headers)
        assert result.status_code == 200
        assert result.json()["package"]["decision"] == "inconclusive"


def test_legacy_task_still_requires_sources():
    with pytest.raises(ValidationError):
        TaskSpec(goal="No sources", constraints=["x"], acceptance_criteria=["x"])


def test_internal_data_policy_is_not_downgraded(research_env, tmp_path):
    _, task, _, _ = research_env
    task.research.llm_review = True
    task.limits.max_calls = 2
    engine = Engine(
        Store(tmp_path / "policy"), demo_config().model_copy(update={"qualification_scope": "contract_pilot"})
    )
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "blocked"
    assert engine.store.calls(run_id) == []


def test_two_model_roles_preserve_gates_and_minimize_transmitted_data(research_env, tmp_path):
    class ResearchGateway:
        def __init__(self):
            self.requests = []

        def generate(self, model, request, timeout, cancelled):
            self.requests.append((model.family, request))
            if request["output_type"] == "ResearchAssessment":
                value = {
                    "summary": "Данных недостаточно для вывода об исполнении.",
                    "concerns": ["Нет bid/ask"],
                    "next_experiments": [
                        "Проверить фиксированный протокол на независимом периоде с котировками."
                    ],
                }
            else:
                value = {"status": "approved", "findings": [], "unresolved_questions": []}
            return ProviderResult(text=json.dumps(value), input_tokens=10, output_tokens=10)

    _, task, _, _ = research_env
    task.research.llm_review = True
    task.limits.max_calls = 2
    task.limits.max_cost_usd = Decimal("0.1")
    gateway = ResearchGateway()
    engine = Engine(Store(tmp_path / "models"), demo_config(), gateway)
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "waiting_approval"
    assert len(gateway.requests) == 2
    assert gateway.requests[0][0] != gateway.requests[1][0]
    for _, request in gateway.requests:
        text = json.dumps(request, ensure_ascii=False)
        assert "Negative result" not in text
        assert "strategies-register" not in text
        assert "reference_prices" not in text
        assert "source_metadata" not in text
        assert "bars" not in request["input"]
    package = engine.store.artifact(run_id, engine.store.run(run_id)["final_id"])[1]
    # Even an approving model cannot certify the deterministic reference result.
    assert package["decision"] == "inconclusive"
    assert replay(engine.store, run_id)["numeric_replay"] == "passed"


def test_missing_day_exit_does_not_delete_trade_or_overnight_loss(research_env):
    connector, task, _, _ = research_env
    snapshot = connector.snapshot(task.research)
    baseline = simulate(snapshot, task.research, test=True)
    opening = next(x for x in baseline["fills"] if x["position_before"] == 0)
    opened = datetime.fromtimestamp(opening["ts"], MSK)
    next_morning = (
        int(datetime.combine(opened.date() + timedelta(days=1), datetime.min.time(), MSK).timestamp())
        + 10 * 3600
    )
    series = snapshot.model_dump(mode="json")["series"]
    for symbol in series:
        series[symbol] = [bar for bar in series[symbol] if not opening["ts"] < bar["ts"] < next_morning]
    changed = simulate(replace_series(snapshot, series), task.research, test=True)
    assert opening in changed["fills"]
    assert changed["overnight_breaches"] > 0
    assert changed["late_exit_events"] > 0


def test_no_last_bar_liquidation(research_env):
    connector, task, _, _ = research_env
    snapshot = connector.snapshot(task.research)
    baseline = simulate(snapshot, task.research, test=True)
    opening = next(x for x in baseline["fills"] if x["position_before"] == 0)
    series = snapshot.model_dump(mode="json")["series"]
    for symbol in series:
        series[symbol] = [bar for bar in series[symbol] if bar["ts"] <= opening["ts"]]
    changed = simulate(replace_series(snapshot, series), task.research, test=True)
    assert changed["residual_position"] == opening["position_after"]
    assert changed["fills"][-1] == opening


def test_legacy_replay_uses_original_task_serialization(engine, task):
    run_id = engine.submit(task)
    engine.run(run_id)
    raw = json.loads(engine.store.run(run_id)["task_json"])
    raw.pop("research")
    with engine.store.connection() as conn:
        conn.execute(
            "UPDATE runs SET task_json=?,task_hash=? WHERE id=?", (json.dumps(raw), digest(raw), run_id)
        )
    assert replay(engine.store, run_id)["integrity"] == "passed"


def test_resume_uses_snapshot_when_source_unavailable(research_env, tmp_path, monkeypatch):
    _, task, db, _ = research_env
    engine = Engine(Store(tmp_path / "resume"), demo_config())
    run_id = engine.submit(task)
    commit = engine.store.commit_artifact

    def crash_after_snapshot(*args, **kwargs):
        result = commit(*args, **kwargs)
        if args[1] == "research_data":
            raise RuntimeError("simulated process failure after durable snapshot")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(engine.store, "commit_artifact", crash_after_snapshot)
        with pytest.raises(RuntimeError, match="simulated"):
            engine.run(run_id)
    assert engine.store.run(run_id)["status"] == "blocked"
    db.unlink()

    def no_connector():
        raise AssertionError("recovery must not reopen source data")

    monkeypatch.setattr("application.research.EconomicCalendar", no_connector)
    engine.store.resume(run_id)
    assert engine.run(run_id)["status"] == "waiting_approval"
    assert replay(engine.store, run_id)["numeric_replay"] == "passed"


def test_research_honors_tool_permissions(research_env, tmp_path):
    _, task, _, _ = research_env
    task.policy.allowed_tools = []
    engine = Engine(Store(tmp_path / "permissions"), demo_config())
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "blocked"
    assert engine.store.artifacts(run_id) == []


def test_research_rejects_same_family_even_when_generic_workflow_allows_it(research_env, tmp_path):
    _, task, _, _ = research_env
    task.research.llm_review = True
    task.limits.max_calls = 2
    engine = Engine(Store(tmp_path / "same-family"), demo_config("single"))
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "blocked"
    assert engine.store.calls(run_id) == []


def test_explicit_demo_models_are_marked_synthetic(research_env, tmp_path):
    _, task, _, _ = research_env
    task.research.llm_review = True
    task.limits.max_calls = 2
    engine = Engine(Store(tmp_path / "demo-models"), demo_config())
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "waiting_approval"
    package = engine.store.artifact(run_id, engine.store.run(run_id)["final_id"])[1]
    assert "Офлайн-синтетический" in package["markdown"]
    assert replay(engine.store, run_id)["numeric_replay"] == "passed"
