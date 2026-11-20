import copy
from datetime import datetime, timedelta, timezone

import pytest

from application.option_backtest import (
    ReplaySpec,
    execution,
    leg,
    parameters,
    replay,
    signal_candidates,
)
from application.option_models import implied, price
from application.options_scanner import MSK, OptionsSettings

START = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)


def ref(secid="C", strike=100, kind="C", expiry="2026-12-17"):
    return {
        "SECID": secid,
        "ASSETCODE": "TEST",
        "UNDERLYINGASSET": "FUT",
        "UNDERLYINGTYPE": "F",
        "STRIKE": strike,
        "OPTIONTYPE": kind,
        "LASTTRADEDATE": expiry,
        "margin_style": "P",
        "exercise_style": "E",
        "STEPPRICE": 0.01,
        "MINSTEP": 0.01,
        "BUYSELLFEE": 0.1,
        "IMSELL": 50,
    }


def record(at, mid, depth=10):
    source = at.astimezone(MSK)
    return {
        "started_at": at.isoformat(),
        "received_at": at.isoformat(),
        "payload": {
            "dataversion": {
                "columns": ["trade_date", "seqnum"],
                "data": [[source.date().isoformat(), int(source.strftime("%Y%m%d%H%M%S"))]],
            },
            "orderbook": {
                "columns": ["BUYSELL", "PRICE", "QUANTITY"],
                "data": [["B", mid - 0.01, depth], ["S", mid + 0.01, depth]],
            },
        },
    }


def frames(count=6):
    output = []
    for i in range(count):
        at = START + timedelta(seconds=i * 30)
        spot = 100 + i * 0.1
        r = ref()
        k, t, c, rate, carry, a = parameters(r, at, ReplaySpec())
        mid = price(spot, k, t, 0.3, c, rate, carry, a)
        output.append(
            {
                "id": str(i),
                "at": at,
                "refs": {"C": r},
                "books": {"C": record(at, mid), "FUT": record(at, spot)},
            }
        )
    return output


def spec(**kwargs):
    return ReplaySpec(
        family="continuation", require_news=False, warmup=3, horizon_seconds=30, max_signals=1, **kwargs
    )


def hedge_spec():
    return {
        "FUT": {
            "kind": "future",
            "point_rub": 1,
            "tick_rub": 0.01,
            "exchange_fee": 0.1,
            "broker_fee": 0.1,
            "capital_per_unit": 10,
            "available_at": START,
            "provenance": "test specification",
        }
    }


def test_black_scholes_golden_and_iv_roundtrip():
    assert price(100, 100, 1, 0.2, True, 0.05, 0.05) == pytest.approx(10.45058357)
    target = price(100, 105, 0.3, 0.37, False, 0.12, 0)
    assert implied(target, 100, 105, 0.3, False, 0.12, 0, False) == pytest.approx(0.37, abs=1e-6)
    assert price(100, 110, 1, 0.2, False, 0.05, 0.05, True) >= price(100, 110, 1, 0.2, False, 0.05, 0.05)
    assert price(100, 100, 1, 0.2, True, 0, 0, True) == pytest.approx(
        price(100, 100, 1, 0.2, True, 0, 0), rel=0.02
    )


def test_no_same_snapshot_entry_and_exact_costs():
    result = replay(frames(), spec())
    assert result["completed"] == 1
    trade = result["trades"][0]
    assert trade["signal_at"] == START + timedelta(seconds=90)
    assert trade["entry"]["cycle"] == "4"
    assert trade["observed_latency_seconds"] == 30
    assert trade["fees"] == pytest.approx(0.4)
    assert trade["slippage"] == pytest.approx(0.02)
    assert trade["scenario_pnl_rub"] == pytest.approx(trade["cash"] - 0.42 - trade["funding"])
    assert not result["certified_edge"]


def test_future_prices_cannot_change_past_signal():
    original = frames(7)
    changed = copy.deepcopy(original)
    changed[-1]["books"]["C"] = record(changed[-1]["at"], 90)
    a = replay(original, spec())["trades"][0]
    b = replay(changed, spec())["trades"][0]
    assert (a["signal_at"], a["features"], a["entry"]) == (b["signal_at"], b["features"], b["entry"])


def test_unknown_dividends_and_short_american_block():
    r = ref()
    r["UNDERLYINGTYPE"] = "S"
    with pytest.raises(ValueError, match="dividend"):
        parameters(r, START, ReplaySpec())
    r["exercise_style"] = "A"
    with pytest.raises(ValueError, match="assignment"):
        leg(r, -1)


def test_multileg_no_partial_fill_and_preorder_book_rejection():
    f = frames()[0]
    settings = OptionsSettings()
    with pytest.raises(ValueError, match="predates"):
        execution([leg(ref(), 1)], f, START + timedelta(seconds=1), spec(), settings)
    f["books"]["C"] = record(START, 5, depth=1)
    with pytest.raises(ValueError, match="depth"):
        execution([leg(ref(), 2)], f, START, spec(), settings)
    with pytest.raises(ValueError, match="missing_book"):
        execution([leg(ref(), 1), leg(ref("P"), 1)], f, START, spec(), settings)


def test_missing_exit_is_censored_not_zero_or_survivor_pnl():
    f = frames()
    del f[-1]["books"]["C"]
    result = replay(f, spec())
    assert result["censored"] == 1
    assert result["strategy_pnl_rub"] is None
    assert "scenario_pnl_rub" not in result["trades"][0]


def test_news_availability_not_publication_time():
    settings = spec().model_copy(update={"require_news": True})
    event = {"id": "news", "assets": ["TEST"], "available_at": START + timedelta(seconds=1000)}
    assert replay(frames(), settings, [event])["trades"] == []
    event["available_at"] = START + timedelta(seconds=80)
    assert replay(frames(), settings, [event])["completed"] == 1


def test_hedge_missing_metadata_and_future_metadata():
    assert replay(frames(), spec(hedge_mode="static"))["rejections"]["hedge_spec_missing"] > 0
    data = hedge_spec()
    data["FUT"]["available_at"] = START + timedelta(days=1)
    assert (
        replay(frames(), spec(hedge_mode="static", hedge_specs=data))["rejections"][
            "hedge_spec_not_yet_available"
        ]
        > 0
    )


def test_hedge_costs_and_underlying_benchmark():
    result = replay(frames(), spec(hedge_mode="static", hedge_specs=hedge_spec()))
    t = result["trades"][0]
    assert t["state"] == "scenario_complete"
    assert t["hedge_quantity"] == -1
    assert t["fees"] == pytest.approx(0.8)
    assert t["benchmark"]["state"] == "scenario_complete"
    assert t["benchmark"]["units"] == 1


def test_gap_and_metadata_change_invalidate_execution():
    f = frames()
    f[-1]["at"] += timedelta(minutes=20)
    assert replay(f, spec())["trades"][0]["failure"] == "execution_window_missed"
    f = frames()
    f[-1]["refs"]["C"]["STEPPRICE"] = 2
    assert replay(f, spec())["trades"][0]["failure"] == "contract_metadata_changed_or_missing"


@pytest.mark.parametrize("family", ["lag", "continuation", "volatility", "skew", "term"])
def test_each_signal_model_generates_frozen_legs(family):
    refs = {
        "C": ref(),
        "P": ref("P", kind="P"),
        "L": ref("L", 95),
        "R": ref("R", 105),
        "FAR": ref("FAR", expiry="2027-01-21"),
        "FAR_R": ref("FAR_R", 105, expiry="2027-01-21"),
    }
    at = START + timedelta(seconds=90)
    states = {s: {"iv": 0.2, "delta": 0.5, "spot": 102, "mid": 1, "at": at} for s in refs}
    states["C"]["iv"] = 0.6 if family in {"skew", "term"} else 0.2
    history = {
        s: [
            {"iv": 0.3, "spot": 100 + i * 0.1, "mid": 1, "at": START + timedelta(seconds=i * 30)}
            for i in range(3)
        ]
        for s in refs
    }
    settings = ReplaySpec(family=family, warmup=3, require_news=False, min_move_bps=1)
    output = signal_candidates(states, history, refs, settings)
    assert output, family
    for legs, _ in output:
        assert all(s in refs and isinstance(q, int) for s, q in legs)
        if family == "term":
            assert len(legs) == 4
            assert sum(q for _, q in legs) == 0


def test_unordered_replay_rejected():
    f = frames()
    f[1]["at"] = f[0]["at"]
    with pytest.raises(ValueError, match="non_monotonic"):
        replay(f, spec())


def test_dynamic_hedge_order_waits_for_later_book():
    data = frames(10)
    settings = spec(hedge_mode="dynamic", hedge_specs=hedge_spec(), hedge_interval_seconds=15).model_copy(
        update={"horizon_seconds": 150}
    )
    for frame in data[5:]:
        at = frame["at"]
        r = ref()
        k, t, c, rate, carry, a = parameters(r, at, settings)
        frame["books"] = {"C": record(at, price(95, k, t, 0.3, c, rate, carry, a)), "FUT": record(at, 95)}
    trade = replay(data, settings)["trades"][0]
    assert trade["state"] == "scenario_complete"
    assert "6" in trade["evidence"]
    assert "5" not in trade["evidence"]
    assert trade["hedge_quantity"] == 0
    assert trade["fees"] == pytest.approx(0.8)


def test_stock_short_hedge_requires_borrow_terms():
    data = hedge_spec()
    data["FUT"]["kind"] = "stock"
    result = replay(frames(), spec(hedge_mode="static", hedge_specs=data))
    assert result["rejections"]["stock_borrow_terms_missing"] > 0


def test_snapshot_hash_checked():
    import hashlib
    import json
    import zlib

    from application.option_research import decode

    raw = json.dumps({"hello": "world"}).encode()
    item = {"raw": zlib.compress(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert decode(item)[0] == {"hello": "world"}
    item["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="integrity"):
        decode(item)


def test_research_routes_require_authentication(tmp_path):
    from fastapi.testclient import TestClient

    from application.api import create_app
    from application.demo import demo_config

    token = "test-options-" + "a" * 32
    with TestClient(create_app(tmp_path, demo_config(), token=token, start_worker=False)) as client:
        assert client.get("/v1/options/research").status_code == 401
        assert client.post("/v1/options/research", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + token}
        data = client.get("/v1/options/research", headers=headers)
        assert data.status_code == 200
        assert len(data.json()["families"]) == 5
        assert (
            client.post("/v1/options/research", headers=headers, json={"max_cycles": 99999}).status_code
            == 422
        )


def test_collector_keeps_underlying_near_chain_without_extra_requests():
    from application.options_scanner import adjacent_request_order

    universe = {
        "a": {"UNDERLYINGASSET": "X"},
        "b": {"UNDERLYINGASSET": "X"},
        "c": {"UNDERLYINGASSET": "Y"},
        "d": {"UNDERLYINGASSET": "Y"},
    }
    paths = {key: key for key in ["a", "b", "c", "d", "X", "Y"]}
    order = [key for key, _ in adjacent_request_order(universe, paths)]
    assert order == ["a", "X", "b", "c", "Y", "d"]
    assert len(order) == len(paths)
