import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from application.algopack import MSK, AlgoPack, DownloadSpec, row_time
from application.contracts import FuturesLeg
from application.liquidity import AsOf, LiquiditySpec, available, execution_quote, replay_targets
from application.market_scanner import ScanSpec, features, robust_z, scan


def policy():
    return LiquiditySpec(dataset_id="a" * 64, source_run_id="b" * 32)


def row(stamp="2026-04-01 10:05:00", **kwargs):
    return {"secid": "SiM6", "tradedate": stamp[:10], "tradetime": stamp[11:], "SYSTIME": stamp, **kwargs}


def test_spread_bps_conversion_and_side_depth():
    leg = FuturesLeg(symbol="SiM6", quantity=1, tick_size=1, point_value_rub=1)
    ob = row(mid_price=80000, spread_l1=2, vol_b_l1=10, vol_s_l1=100)
    trade = row(vol=10000, im=15000)
    q, reason = execution_quote(leg, 1, ob, trade, policy())
    assert reason is None
    assert q["half_spread_points"] == 8
    assert q["cost_rub"] == pytest.approx(14.01)
    assert execution_quote(leg, -1, ob, trade, policy())[1] == "depth_capacity"
    assert execution_quote(leg, 1, ob, row(vol=1), policy())[1] == "volume_capacity"
    assert execution_quote(leg, 1, None, trade, policy())[1] == "missing_or_stale_liquidity"


def test_asof_enforces_publication_lag_and_does_not_rewind_on_correction():
    a = row("2026-04-01 10:05:00", mid_price=100)
    b = row("2026-04-01 10:10:00", mid_price=101)
    old_correction = row("2026-04-01 10:00:00", mid_price=999)
    old_correction["SYSTIME"] = "2026-04-01 10:15:00"
    index = AsOf([a, b, old_correction], 60)
    assert index.get(row_time(a) + 59, 600) is None
    assert index.get(row_time(a) + 60, 600)["mid_price"] == 100
    assert index.get(row_time(b) + 301, 600)["mid_price"] == 101
    assert index.get(row_time(b) + 601, 600) is None
    assert available(old_correction, 60) == row_time(old_correction) + 900


@pytest.mark.parametrize(
    "updates",
    [
        {"start": "2026-06-16"},
        {"symbols": ["../secret"]},
        {"products": ["users"]},
        {"whole_futures_market": True},
    ],
)
def test_download_scope_validation(updates):
    value = {"start": "2026-01-01", "end": "2026-06-15", "symbols": ["SiM6"]} | updates
    with pytest.raises(ValidationError):
        DownloadSpec.model_validate(value)


def test_pagination_uses_actual_size_and_empty_page():
    client = AlgoPack.__new__(AlgoPack)
    starts = []

    def page(path, params):
        starts.append(params["start"])
        data = [[i] for i in range(params["start"], min(params["start"] + 2, 5))]
        return {"payload": {"data": {"columns": ["n"], "data": data}}}

    client.page = page
    rows, _ = client.table("test", "data", {})
    assert starts == [0, 2, 4, 5]
    assert [r["n"] for r in rows] == list(range(5))


def test_repeated_page_is_error_not_silent_truncation():
    client = AlgoPack.__new__(AlgoPack)
    client.page = lambda path, params: {"payload": {"data": {"columns": ["n"], "data": [[1]]}}}
    with pytest.raises(ValueError, match="repeated"):
        client.table("test", "data", {})


def scanner_data():
    data = {p: [] for p in ("tradestats", "obstats", "orderstats")}
    first = datetime(2026, 11, 2, 10, 0, tzinfo=MSK)
    for day in range(6):
        for n in range(12):
            stamp = (first + timedelta(days=day, minutes=5 * n)).strftime("%Y-%m-%d %H:%M:%S")
            shock = day == 4 and n in (4, 5, 6, 7)
            data["tradestats"].append(
                row(
                    stamp,
                    pr_open=100,
                    pr_close=100.001,
                    vol=10000 if shock else 100 + n * 5,
                    vol_b=9000 if shock else 50 + n,
                    vol_s=1000 if shock else 50,
                    oi_open=10000,
                    oi_close=10200 if shock else 10001,
                )
            )
            data["obstats"].append(
                row(
                    stamp,
                    mid_price=100,
                    spread_l1=2 + n / 10,
                    vol_b_l1=9000 if shock else 100 + n,
                    vol_s_l1=100 if shock else 100,
                )
            )
            data["orderstats"].append(
                row(stamp, put_vol_b=100 + n, put_vol_s=100, cancel_vol_b=30 + n, cancel_vol_s=30)
            )
    return data


def test_scanner_future_invariance_and_causal_baseline():
    data = scanner_data()
    spec = ScanSpec(dataset_id="a" * 64, as_of=datetime(2026, 11, 6, 23, tzinfo=MSK))
    original = scan(data, spec)
    assert original["events_count"] > 0
    assert all(e["baseline_dates"] >= 3 for e in original["events"])
    assert all(e["available_at"] >= e["interval_end"] + 60 for e in original["events"])
    changed = json.loads(json.dumps(data))
    for rows in changed.values():
        for r in rows:
            if r["tradedate"] == "2026-11-07":
                for k in ("pr_close", "mid_price", "vol_b_l1", "vol"):
                    if k in r:
                        r[k] *= 100
    assert scan(changed, spec)["events"] == original["events"]
    # Current shocks must be measured against previous dates, not diluted into their own baseline.
    assert any(e["family"] == "volume_and_oi_shock" for e in original["events"])


def test_scanner_no_fabricated_scores_and_no_spoofing_claim():
    assert robust_z(100, [1, 1, 1]) is None
    data = scanner_data()
    spec = ScanSpec(dataset_id="a" * 64, as_of=datetime(2026, 11, 7, 23, tzinfo=MSK))
    result = scan(data, spec)
    assert all(
        e["status"] == "unvalidated_candidate" and e["not_spoofing_evidence"] for e in result["events"]
    )
    bad = dict(data["obstats"][0], spread_l1=-1)
    assert features(data["tradestats"][0], bad, data["orderstats"][0]) is None


def test_missing_orderstats_does_not_hide_other_anomalies():
    data = scanner_data()
    data["orderstats"] = []
    spec = ScanSpec(dataset_id="a" * 64, as_of=datetime(2026, 11, 7, 23, tzinfo=MSK))
    result = scan(data, spec)
    assert result["events_count"] > 0
    assert not any(x["family"] == "asymmetric_cancel_replace" for x in result["events"])
    assert result["exclusions"]["orderstats_unavailable_cancel_detector_disabled"] == 72


def test_algopack_api_authorization_validation_and_result_integrity(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from application.algopack import save_json
    from application.api import create_app
    from application.config import digest

    monkeypatch.setattr("application.algopack.PROJECT", tmp_path)
    token = "algopack-test-token-at-least-24"
    headers = {"Authorization": "Bearer " + token}
    folder = tmp_path / "var/algopack/scans" / ("a" * 64)
    result = {"events": [], "status": "research_candidates_only"}
    save_json(folder / "result.json", result)
    save_json(folder / "integrity.json", {"result_sha256": digest(result)})
    with TestClient(create_app(tmp_path / "state", token=token, start_worker=False)) as client:
        assert client.get("/v1/algopack/datasets").status_code == 401
        assert client.get("/v1/algopack/datasets", headers=headers).json() == []
        schema = client.get("/v1/schemas", headers=headers).json()
        assert {"DownloadSpec", "LiquiditySpec", "ScanSpec"} <= set(schema)
        assert client.get("/v1/algopack/scans/" + "a" * 64, headers=headers).status_code == 200
        assert client.get("/v1/algopack/secrets/" + "a" * 64, headers=headers).status_code == 404
        assert (
            client.post(
                "/v1/algopack/market-scan", headers=headers, json={"dataset_id": "../secret"}
            ).status_code
            == 422
        )
        save_json(folder / "result.json", {"events": ["tampered"]})
        assert client.get("/v1/algopack/scans/" + "a" * 64, headers=headers).status_code == 409


def test_job_lock_releases_after_failure(tmp_path):
    from application.algopack import job_lock

    with pytest.raises(RuntimeError):
        with job_lock(tmp_path / "job"):
            with pytest.raises(ValueError, match="already running"):
                with job_lock(tmp_path / "job"):
                    pass
            raise RuntimeError("simulated process exception")
    with job_lock(tmp_path / "job"):
        pass


def test_liquidity_replay_does_not_use_future_minute_volume_or_cancel_bad_exit(monkeypatch):
    from application.contracts import ResearchPlan, ResearchSpec

    spec = ResearchSpec(
        strategy="calendar_spread_zscore",
        legs=[
            FuturesLeg(symbol="SiM6", quantity=1, point_value_rub=1, tick_size=1),
            FuturesLeg(symbol="SiU6", quantity=-1, point_value_rub=1, tick_size=1),
        ],
        start="2026-03-01",
        test_start="2026-04-01",
        end="2026-05-01",
    )
    plan = ResearchPlan(protocol=spec, rules=["test"])
    stamp = "2026-04-01 10:05:00"
    end = row_time(row(stamp))
    data = {p: [] for p in ("candles_1m", "tradestats", "obstats")}
    for s in ("SiM6", "SiU6"):
        data["obstats"].append(
            dict(row(stamp, mid_price=80000, spread_l1=2, vol_b_l1=1000, vol_s_l1=1000), secid=s)
        )
        data["tradestats"].append(dict(row(stamp, vol=10000, im=10000), secid=s))
        for minutes in (6, 7, 17 * 60 - 10 * 60):
            dt = datetime(2026, 4, 1, 10, 0, tzinfo=MSK) + timedelta(minutes=minutes)
            data["candles_1m"].append(
                {
                    "secid": s,
                    "begin": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": 80000,
                    "close": 80000,
                    "low": 79999,
                    "high": 80001,
                    "volume": 0,
                }
            )
    intent = {"ts": end + 60, "signal_available_at": end - 3600, "position_after": 1}
    monkeypatch.setattr(
        "application.liquidity.simulate", lambda *a, **k: {"fills": [intent], "scenario_marked_pnl_rub": 0}
    )
    result = replay_targets(data, plan, None, policy(), test=True)
    assert result["fills_count"] == 1
    assert result["residual_position"] == 1  # stale exit liquidity cannot erase the exposure
    assert result["rejected_execution_minutes"]["missing_or_stale_liquidity"] > 0
    changed = json.loads(json.dumps(data))
    for r in changed["candles_1m"]:
        r["volume"] = 100000000
    assert replay_targets(changed, plan, None, policy(), test=True) == result
