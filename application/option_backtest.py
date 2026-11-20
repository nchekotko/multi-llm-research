"""Chronological, bounded replay of recorded books. No broker orders or LLM calls."""

import hashlib
import json
import math
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .option_models import YEAR, delta, implied, price
from .options_math import rows
from .options_scanner import MSK, OptionsSettings, book, fill

FAMILIES = {
    "lag": "Опцион запаздывает за базовым активом",
    "continuation": "Продолжение движения",
    "volatility": "Недооценённая будущая волатильность",
    "skew": "Перекос между страйками",
    "term": "Перекос между сроками",
}


class HedgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    kind: Literal["future", "stock"]
    point_rub: float = Field(gt=0)
    tick_rub: float = Field(gt=0)
    exchange_fee: float = Field(ge=0)
    capital_per_unit: float = Field(gt=0)
    broker_fee: float = Field(ge=0)
    # Quantity is in exchange lots / contracts, not in shares.
    available_at: datetime
    provenance: str = Field(min_length=5, max_length=300)


class ReplaySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    family: Literal["lag", "continuation", "volatility", "skew", "term"] = "lag"
    max_cycles: int = Field(120, ge=5, le=500)
    warmup: int = Field(12, ge=3, le=60)
    horizon_seconds: int = Field(300, ge=30, le=3600)
    latency_seconds: float = Field(2, ge=0.1, le=120)
    max_gap_seconds: int = Field(120, ge=10, le=600)
    max_age_seconds: int = Field(30, ge=5, le=120)
    max_skew_seconds: float = Field(3, ge=0.1, le=30)
    min_move_bps: float = Field(10, ge=0, le=1000)
    min_edge_ticks: float = Field(2, ge=0, le=100)
    iv_gap: float = Field(0.05, ge=0.001, le=2)
    volatility_multiplier: float = Field(1, ge=0.1, le=5)
    slippage_ticks: int = Field(1, ge=0, le=20)
    broker_exchange_multiplier: float = Field(1, ge=0, le=10)
    capital_rate: float = Field(0.4, ge=0, le=5)
    pricing_rate: float = Field(0.15, ge=0, le=2)
    dividend_yield: float | None = Field(None, ge=0, le=2)
    expiry_hour_msk: int = Field(19, ge=0, le=23)
    require_news: bool = True
    news_window_seconds: int = Field(900, ge=30, le=3600)
    hedge_mode: Literal["none", "static", "dynamic"] = "none"
    hedge_interval_seconds: int = Field(60, ge=15, le=600)
    hedge_specs: dict[str, HedgeSpec] = Field(default_factory=dict, max_length=30)
    max_signals: int = Field(50, ge=1, le=100)
    # Declared cutoff, never automatically labelled independent OOS.
    evaluation_start: datetime | None = None


def timestamp(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value is None or value.tzinfo is None:
        raise ValueError("timezone_required")
    return value


def midpoint(record, settings):
    bids, asks = book(record, settings)
    return (bids[0][0] + asks[0][0]) / 2


def parameters(ref, at, spec):
    # Stop before expiration day: exact fixing time and exercise not simulated.
    last_day = datetime.fromisoformat(ref["LASTTRADEDATE"]).replace(tzinfo=MSK)
    expiry = datetime.fromisoformat(ref.get("LASTDELDATE") or ref["LASTTRADEDATE"]).replace(
        hour=spec.expiry_hour_msk, tzinfo=MSK
    )
    years = (expiry - at).total_seconds() / YEAR
    if at >= last_day or years <= 0:
        raise ValueError("expiry_or_fixing_not_supported")
    if ref["exercise_style"] not in {"A", "E"} or ref["margin_style"] not in {"P", "M"}:
        raise ValueError("unsupported_contract_style")
    if ref["UNDERLYINGTYPE"] == "F":
        rate = 0.0 if ref["margin_style"] == "M" else spec.pricing_rate
        carry = 0.0
    elif ref["UNDERLYINGTYPE"] == "S" and ref["exercise_style"] == "E" and ref["margin_style"] == "P":
        if spec.dividend_yield is None:
            raise ValueError("dividend_assumption_missing")
        rate, carry = spec.pricing_rate, spec.pricing_rate - spec.dividend_yield
    else:
        raise ValueError("underlying_carry_or_tradable_proxy_missing")
    return float(ref["STRIKE"]), years, ref["OPTIONTYPE"] == "C", rate, carry, ref["exercise_style"] == "A"


def model(ref, at, spot, mid, spec):
    strike, years, call, rate, carry, american = parameters(ref, at, spec)
    vol = implied(mid, spot, strike, years, call, rate, carry, american)
    d = delta(spot, strike, years, vol, call, rate, carry, american)
    return {
        "iv": vol,
        "delta": d,
        "spot": spot,
        "mid": mid,
        "at": at,
        "reference_signature": tuple(
            ref.get(k)
            for k in (
                "STRIKE",
                "MINSTEP",
                "STEPPRICE",
                "LASTTRADEDATE",
                "UNDERLYINGASSET",
                "exercise_style",
                "margin_style",
            )
        ),
    }


def theoretical(ref, at, spot, vol, spec):
    strike, years, call, rate, carry, american = parameters(ref, at, spec)
    return price(spot, strike, years, vol, call, rate, carry, american)


def signal_candidates(states, histories, refs, spec):
    result = []
    for secid, current in sorted(states.items()):
        ref = refs[secid]
        history = histories[secid]
        if len(history) < spec.warmup:
            continue
        past = list(history)[-spec.warmup :]
        if any(p.get("reference_signature") != current.get("reference_signature") for p in past):
            continue
        # Overnight / outage windows are excluded, not compressed into one return.
        window = past + [current]
        if any(
            not 0 < (b["at"] - a["at"]).total_seconds() <= spec.max_gap_seconds
            or a["at"].astimezone(MSK).date() != b["at"].astimezone(MSK).date()
            for a, b in zip(window, window[1:], strict=False)
        ):
            continue
        move = current["spot"] / past[0]["spot"] - 1
        if spec.family == "lag":
            expected = theoretical(ref, current["at"], current["spot"], past[-1]["iv"], spec)
            edge = expected - current["mid"]
            if (
                abs(current["spot"] / past[-1]["spot"] - 1) * 1e4 >= spec.min_move_bps
                and edge >= spec.min_edge_ticks * ref["MINSTEP"]
            ):
                result.append(([(secid, 1)], {"fair_price_prior_iv": expected, "price_gap": edge}))
        elif spec.family == "continuation":
            direction = 1 if ref["OPTIONTYPE"] == "C" else -1
            if direction * move * 1e4 >= spec.min_move_bps:
                result.append(
                    ([(secid, 1)], {"past_underlying_move": move, "benchmark_direction": direction})
                )
        elif spec.family == "volatility" and ref["OPTIONTYPE"] == "C":
            duration = (current["at"] - past[0]["at"]).total_seconds()
            forecast = (
                math.sqrt(
                    sum(
                        math.log(b["spot"] / a["spot"]) ** 2 for a, b in zip(window, window[1:], strict=False)
                    )
                    * YEAR
                    / duration
                )
                * spec.volatility_multiplier
            )
            partner = next(
                (
                    s
                    for s, r in sorted(refs.items())
                    if s in states
                    and r["OPTIONTYPE"] == "P"
                    and compatible(ref, r)
                    and r["STRIKE"] == ref["STRIKE"]
                ),
                None,
            )
            if partner and forecast > max(current["iv"], states[partner]["iv"]) + spec.iv_gap:
                result.append(
                    (
                        [(secid, 1), (partner, 1)],
                        {
                            "realized_vol_forecast": forecast,
                            "observed_iv": max(current["iv"], states[partner]["iv"]),
                        },
                    )
                )
        elif spec.family == "skew":
            chain = sorted(
                [
                    s
                    for s, r in refs.items()
                    if s in states and compatible(ref, r) and r["OPTIONTYPE"] == ref["OPTIONTYPE"]
                ],
                key=lambda s: refs[s]["STRIKE"],
            )
            index = chain.index(secid)
            if 0 < index < len(chain) - 1:
                left, right = chain[index - 1], chain[index + 1]
                if math.isclose(ref["STRIKE"] - refs[left]["STRIKE"], refs[right]["STRIKE"] - ref["STRIKE"]):
                    gap = current["iv"] - (states[left]["iv"] + states[right]["iv"]) / 2
                    if gap >= spec.iv_gap:
                        result.append(([(left, 1), (secid, -2), (right, 1)], {"smile_iv_residual": gap}))
        elif spec.family == "term":
            later = [
                s
                for s, r in refs.items()
                if s in states
                and compatible(ref, r, expiry=False)
                and r["OPTIONTYPE"] == ref["OPTIONTYPE"]
                and r["STRIKE"] == ref["STRIKE"]
                and r["LASTTRADEDATE"] > ref["LASTTRADEDATE"]
            ]
            if later:
                far = min(later, key=lambda s: refs[s]["LASTTRADEDATE"])
                gap = current["iv"] - states[far]["iv"]
                if gap >= spec.iv_gap:
                    # Protect both short legs with vertical wings. An unprotected
                    # calendar is not universally bounded under carry/dividend changes.
                    direction = 1 if ref["OPTIONTYPE"] == "C" else -1
                    wings = sorted(
                        (
                            s
                            for s, r in refs.items()
                            if s in states
                            and compatible(ref, r)
                            and r["OPTIONTYPE"] == ref["OPTIONTYPE"]
                            and direction * (r["STRIKE"] - ref["STRIKE"]) > 0
                        ),
                        key=lambda s: abs(refs[s]["STRIKE"] - ref["STRIKE"]),
                    )
                    for near_wing in wings:
                        far_wing = next(
                            (
                                s
                                for s, r in sorted(refs.items())
                                if s in states
                                and compatible(refs[far], r)
                                and r["OPTIONTYPE"] == ref["OPTIONTYPE"]
                                and r["STRIKE"] == refs[near_wing]["STRIKE"]
                            ),
                            None,
                        )
                        if far_wing:
                            result.append(
                                (
                                    [(secid, -1), (near_wing, 1), (far, 1), (far_wing, -1)],
                                    {
                                        "term_iv_gap": gap,
                                        "wing_width": abs(refs[near_wing]["STRIKE"] - ref["STRIKE"]),
                                    },
                                )
                            )
                            break
    return result


def compatible(a, b, expiry=True):
    keys = ["UNDERLYINGASSET", "UNDERLYINGTYPE", "exercise_style", "margin_style"]
    if expiry:
        keys.append("LASTTRADEDATE")
    return all(a[k] == b[k] for k in keys) and math.isclose(
        a["STEPPRICE"] / a["MINSTEP"], b["STEPPRICE"] / b["MINSTEP"]
    )


def leg(ref, quantity):
    for key in ("STEPPRICE", "MINSTEP"):
        if not isinstance(ref.get(key), (int, float)) or not math.isfinite(ref[key]) or ref[key] <= 0:
            raise ValueError("contract_conversion_missing")
    if ref.get("BUYSELLFEE") is None or ref["BUYSELLFEE"] < 0:
        raise ValueError("exchange_fee_missing")
    if quantity < 0 and ref["exercise_style"] == "A":
        raise ValueError("early_assignment_engine_required_for_short_american")
    return {
        "secid": ref["SECID"],
        "quantity": quantity,
        "point": ref["STEPPRICE"] / ref["MINSTEP"],
        "tick": ref["STEPPRICE"],
        "fee": ref["BUYSELLFEE"],
        "ref": dict(ref),
    }


def execution(legs, frame, ready, spec, settings, reverse=False):
    cash, fees, slippage = 0.0, 0.0, 0.0
    received = []
    sources = []
    for item in legs:
        record = frame["books"].get(item["secid"])
        if record is None:
            raise ValueError("missing_book")
        at = timestamp(record["received_at"])
        started = timestamp(record["started_at"])
        data = rows(record["payload"]["dataversion"])[0]
        source = datetime.strptime(str(data["seqnum"]), "%Y%m%d%H%M%S").replace(tzinfo=MSK)
        if started < ready or source < ready:
            raise ValueError("book_predates_order")
        if (frame["at"] - at).total_seconds() > spec.max_age_seconds:
            raise ValueError("stale_cycle_leg")
        bids, asks = book(record, settings)
        q = item["quantity"] * (-1 if reverse else 1)
        cash -= math.copysign(1, q) * fill(asks if q > 0 else bids, abs(q)) * item["point"]
        fees += abs(q) * item["fee"] * (1 + spec.broker_exchange_multiplier)
        slippage += abs(q) * item["tick"] * spec.slippage_ticks
        received.append(at)
        sources.append(source)
    if (max(received) - min(received)).total_seconds() > spec.max_skew_seconds or (
        max(sources) - min(sources)
    ).total_seconds() > spec.max_skew_seconds:
        raise ValueError("asynchronous_legs")
    return {"cash": cash, "fees": fees, "slippage": slippage, "at": max(received), "cycle": frame["id"]}


def hedge_target(legs, frame, spec, settings):
    underlying = legs[0]["ref"]["UNDERLYINGASSET"]
    meta = spec.hedge_specs.get(underlying)
    if meta is None:
        raise ValueError("hedge_spec_missing")
    if timestamp(meta.available_at) > frame["at"]:
        raise ValueError("hedge_spec_not_yet_available")
    stamps = [
        timestamp(frame["books"][key]["received_at"])
        for key in [underlying] + [item["secid"] for item in legs]
    ]
    if (frame["at"] - min(stamps)).total_seconds() > spec.max_age_seconds:
        raise ValueError("stale_hedge_model_inputs")
    if (max(stamps) - min(stamps)).total_seconds() > spec.max_skew_seconds:
        raise ValueError("asynchronous_hedge_model_inputs")
    spot = midpoint(frame["books"][underlying], settings)
    exposure = 0.0
    for item in legs:
        ref = item["ref"]
        m = model(ref, frame["at"], spot, midpoint(frame["books"][item["secid"]], settings), spec)
        exposure += item["quantity"] * item["point"] * m["delta"]
    quantity = -int(math.copysign(math.floor(abs(exposure / meta.point_rub) + 0.5), exposure))
    if meta.kind == "stock" and quantity < 0:
        raise ValueError("stock_borrow_terms_missing")
    return underlying, quantity, exposure


def hedge_leg(symbol, quantity, spec):
    m = spec.hedge_specs[symbol]
    # execution expects exchange + multiplier; broker fee is specified explicitly for hedge.
    return {
        "secid": symbol,
        "quantity": quantity,
        "point": m.point_rub,
        "tick": m.tick_rub,
        "fee": (m.exchange_fee + m.broker_fee) / (1 + spec.broker_exchange_multiplier),
    }


def replay(frames, spec, events=()):
    settings = OptionsSettings(
        max_book_age_seconds=max(5, spec.max_age_seconds),
        max_receipt_skew_seconds=spec.max_skew_seconds if spec.max_skew_seconds <= 20 else 20,
    )
    begun = time.monotonic()
    halted = False
    history = defaultdict(lambda: deque(maxlen=spec.warmup))
    rejects = Counter()
    trades = []
    active = None
    manifest = []
    seen = set()
    last_at = None
    for frame in frames:
        if time.monotonic() - begun > 90:
            raise ValueError("compute_budget_exceeded")
        at = frame["at"] = timestamp(frame["at"])
        if last_at is not None and at <= last_at:
            raise ValueError("non_monotonic_replay")
        last_at = at
        manifest.append(
            {
                "id": frame["id"],
                "sha256": frame.get("sha256"),
                "discovery_sha256": frame.get("discovery_sha256"),
            }
        )
        states = {}
        refs = frame["refs"]
        for secid, ref in sorted(refs.items()):
            try:
                if len(states) >= 100:
                    raise ValueError("model_universe_budget")
                for key in (secid, ref["UNDERLYINGASSET"]):
                    if (
                        at - timestamp(frame["books"][key]["received_at"])
                    ).total_seconds() > spec.max_age_seconds:
                        raise ValueError("stale_cycle_leg")
                if (
                    abs(
                        (
                            timestamp(frame["books"][secid]["received_at"])
                            - timestamp(frame["books"][ref["UNDERLYINGASSET"]]["received_at"])
                        ).total_seconds()
                    )
                    > spec.max_skew_seconds
                ):
                    raise ValueError("asynchronous_underlying")
                spot = midpoint(frame["books"][ref["UNDERLYINGASSET"]], settings)
                states[secid] = model(ref, at, spot, midpoint(frame["books"][secid], settings), spec)
            except (ValueError, KeyError, IndexError, TypeError, OverflowError) as exc:
                rejects[str(exc) if isinstance(exc, ValueError) else "missing_model_data"] += 1
        if active:
            try:
                for held in active["legs"]:
                    current_ref = refs.get(held["secid"])
                    if current_ref is None or any(
                        current_ref.get(k) != held["ref"].get(k)
                        for k in ("STRIKE", "MINSTEP", "STEPPRICE", "LASTTRADEDATE", "UNDERLYINGASSET")
                    ):
                        raise ValueError("contract_metadata_changed_or_missing")
                target = (
                    active["ready"]
                    if active["state"] == "pending"
                    else active["entry"]["at"] + timedelta(seconds=spec.horizon_seconds)
                )
                if at.astimezone(MSK).date() != active["signal_at"].astimezone(MSK).date():
                    raise ValueError("overnight_vm_or_corporate_actions_not_supported")
                if (at - target).total_seconds() > spec.max_gap_seconds:
                    raise ValueError("execution_window_missed")
                if at >= target:
                    is_entry = active["state"] == "pending"
                    legs = active["legs"][:]
                    if spec.hedge_mode != "none" and active.get("hedge_quantity"):
                        legs.append(hedge_leg(active["underlying"], active["hedge_quantity"], spec))
                    result = execution(legs, frame, target, spec, settings, reverse=not is_entry)
                    active["evidence"].append(result["cycle"])
                    active["cash"] += result["cash"]
                    active["fees"] += result["fees"]
                    active["slippage"] += result["slippage"]
                    if is_entry:
                        active["entry"] = result
                        active["state"] = "open"
                        active["last_hedge_at"] = result["at"]
                        active["observed_latency_seconds"] = (
                            result["at"] - active["signal_at"]
                        ).total_seconds()
                        capital = sum(
                            abs(contract_leg["quantity"])
                            * max(
                                abs(
                                    midpoint(frame["books"][contract_leg["secid"]], settings)
                                    * contract_leg["point"]
                                ),
                                float(
                                    contract_leg["ref"].get(
                                        "IMSELL" if contract_leg["quantity"] < 0 else "IMBUY"
                                    )
                                    or 0
                                ),
                            )
                            for contract_leg in active["legs"]
                        )
                        if spec.hedge_mode != "none":
                            capital += (
                                abs(active["hedge_quantity"])
                                * spec.hedge_specs[active["underlying"]].capital_per_unit
                            )
                        active["capital"] = capital
                        if spec.family == "continuation":
                            try:
                                meta = spec.hedge_specs.get(active["underlying"])
                                if meta is None:
                                    raise ValueError("benchmark_contract_costs_missing")
                                if timestamp(meta.available_at) > active["signal_at"]:
                                    raise ValueError("benchmark_metadata_not_available")
                                direction = active["features"]["benchmark_direction"]
                                if meta.kind == "stock" and direction < 0:
                                    raise ValueError("benchmark_stock_borrow_missing")
                                benchmark_leg = hedge_leg(active["underlying"], direction, spec)
                                b = execution([benchmark_leg], frame, target, spec, settings)
                                active["benchmark"] = {
                                    "leg": benchmark_leg,
                                    "entry": b,
                                    "state": "open",
                                    "units": direction,
                                    "comparison": "one_unit_not_risk_matched",
                                }
                            except (ValueError, KeyError) as exc:
                                active["benchmark"] = {"state": "unavailable", "reason": str(exc)}
                    else:
                        duration = (result["at"] - active["entry"]["at"]).total_seconds()
                        active["funding"] = active["capital"] * spec.capital_rate * duration / YEAR
                        active["scenario_pnl_rub"] = (
                            active["cash"] - active["fees"] - active["slippage"] - active["funding"]
                        )
                        benchmark = active.get("benchmark", {})
                        if benchmark.get("state") == "open":
                            try:
                                b = execution([benchmark["leg"]], frame, target, spec, settings, reverse=True)
                                start = benchmark["entry"]
                                meta = spec.hedge_specs[active["underlying"]]
                                funding = (
                                    meta.capital_per_unit
                                    * spec.capital_rate
                                    * (b["at"] - start["at"]).total_seconds()
                                    / YEAR
                                )
                                benchmark.update(
                                    state="scenario_complete",
                                    exit=b,
                                    scenario_pnl_rub=start["cash"]
                                    + b["cash"]
                                    - start["fees"]
                                    - b["fees"]
                                    - start["slippage"]
                                    - b["slippage"]
                                    - funding,
                                )
                            except (ValueError, KeyError) as exc:
                                benchmark.update(state="censored", reason=str(exc))
                        active["state"] = "scenario_complete"
                        active["exit_at"] = result["at"]
                        trades.append(active)
                        active = None
                elif active["state"] == "open" and spec.hedge_mode == "dynamic":
                    pending = active.get("hedge_order")
                    if pending and (at - pending["ready"]).total_seconds() > spec.max_gap_seconds:
                        raise ValueError("hedge_window_missed")
                    if pending and at >= pending["ready"]:
                        change = pending["quantity"] - active["hedge_quantity"]
                        if change:
                            result = execution(
                                [hedge_leg(active["underlying"], change, spec)],
                                frame,
                                pending["ready"],
                                spec,
                                settings,
                            )
                            active["cash"] += result["cash"]
                            active["fees"] += result["fees"]
                            active["slippage"] += result["slippage"]
                            active["evidence"].append(result["cycle"])
                            active["capital"] = max(
                                active["capital"],
                                active["capital"]
                                + max(0, abs(pending["quantity"]) - abs(active["hedge_quantity"]))
                                * spec.hedge_specs[active["underlying"]].capital_per_unit,
                            )
                        active["hedge_quantity"] = pending["quantity"]
                        active["last_hedge_at"] = at
                        active["hedge_order"] = None
                    elif (
                        not pending
                        and (at - active["last_hedge_at"]).total_seconds() >= spec.hedge_interval_seconds
                    ):
                        _, quantity, _ = hedge_target(active["legs"], frame, spec, settings)
                        active["hedge_order"] = {
                            "quantity": quantity,
                            "ready": at + timedelta(seconds=spec.latency_seconds),
                        }
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else "missing_execution_data"
                # A pre-order book can be retried until the window expires. Other failures censor the package.
                if reason != "book_predates_order":
                    halted = halted or active["state"] == "open"
                    active["state"] = "censored"
                    active["failure"] = reason
                    trades.append(active)
                    active = None
                    rejects[reason] += 1
        if (
            not halted
            and active is None
            and len(trades) < spec.max_signals
            and (spec.evaluation_start is None or at >= timestamp(spec.evaluation_start))
        ):
            try:
                candidates = signal_candidates(states, history, refs, spec)
            except (ValueError, KeyError, OverflowError):
                candidates = []
                rejects["signal_model_unavailable"] += 1
            for selection, features in candidates:
                ref = refs[selection[0][0]]
                linked = [
                    e
                    for e in events
                    if ref["ASSETCODE"] in e["assets"]
                    and 0 <= (at - timestamp(e["available_at"])).total_seconds() <= spec.news_window_seconds
                ]
                if spec.require_news and not linked:
                    rejects["no_point_in_time_news_link"] += 1
                    continue
                event_id = linked[-1]["id"] if linked else None
                key = (tuple(selection), event_id or at.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    legs = [leg(refs[s], q) for s, q in selection]
                    for contract_leg in legs:
                        if contract_leg["quantity"] < 0 and not contract_leg["ref"].get("IMSELL"):
                            raise ValueError("short_margin_missing")
                    active = {
                        "family": spec.family,
                        "signal_at": at,
                        "ready": at + timedelta(seconds=spec.latency_seconds),
                        "state": "pending",
                        "legs": legs,
                        "features": features,
                        "event_id": event_id,
                        "underlying": ref["UNDERLYINGASSET"],
                        "evidence": [frame["id"]],
                        "cash": 0.0,
                        "fees": 0.0,
                        "slippage": 0.0,
                        "hedge_quantity": 0,
                    }
                    if spec.hedge_mode != "none":
                        _, quantity, exposure = hedge_target(legs, frame, spec, settings)
                        active.update(hedge_quantity=quantity, initial_delta_rub=exposure)
                    break
                except (ValueError, KeyError, IndexError) as exc:
                    rejects[str(exc) if isinstance(exc, ValueError) else "missing_hedge_data"] += 1
                    active = None
        for secid, state in states.items():
            history[secid].append(state)
    if active:
        active["state"] = "right_censored"
        trades.append(active)
    completed = [t for t in trades if t["state"] == "scenario_complete"]
    # Never aggregate survivors as a complete strategy when any execution was censored.
    censored = sum(t["state"] != "scenario_complete" for t in trades)
    return {
        "version": "option-replay-v1",
        "spec": spec.model_dump(mode="json"),
        "manifest": manifest,
        "model_frames": dict((k, len(v)) for k, v in history.items()),
        "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "trades": trades,
        "rejections": dict(rejects),
        "completed": len(completed),
        "censored": censored,
        "completed_scenario_pnl_rub": sum(t["scenario_pnl_rub"] for t in completed),
        "strategy_pnl_rub": None
        if censored or not completed
        else sum(t["scenario_pnl_rub"] for t in completed),
        "certified_edge": False,
        "validation": "exploratory_replay_not_independent_oos",
        "limitations": [
            "HTTP snapshots; simultaneous multi-leg fills are a scenario, not guaranteed execution.",
            "No passive queue, exercise, assignment, daily VM, corporate actions or cross-session positions.",
            "Pricing rates, continuous dividend yield and broker costs are scenario assumptions.",
            "One position at a time; censored trades prevent aggregate strategy PnL certification.",
            "No parameter optimization or independent holdout evidence; completed sample is not proof of edge.",
        ],
    }
