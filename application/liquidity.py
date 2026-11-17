"""Frozen-target replay with minute reference prices and lagged AlgoPack capacity.

This is a cost attribution / capacity study. It does not invent historical ticks,
queue position, guaranteed fills or a new untouched holdout.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from pydantic import Field

from .algopack import MSK, PROJECT, job_lock, parse_time, read_dataset, row_time, save_json
from .config import digest
from .contracts import Contract, DatasetSnapshot, ResearchPlan, utcnow
from .research import audit_data, simulate
from .storage import Store


class LiquiditySpec(Contract):
    dataset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    publication_buffer_seconds: int = Field(default=60, ge=1, le=3600)
    max_quote_age_seconds: int = Field(default=600, ge=60, le=1800)
    max_depth_fraction: float = Field(default=0.05, gt=0, le=0.2)
    max_minute_volume_fraction: float = Field(default=0.01, gt=0, le=0.1)
    fee_rub_per_contract: float = Field(default=5, ge=0, le=1000)
    impact_ticks: float = Field(default=1, ge=0, le=100)
    capital_rub: float = Field(default=1000000, gt=0, le=1000000000)
    size_multipliers: list[int] = Field(default_factory=lambda: [1, 5, 20], min_length=1, max_length=5)
    cost_multipliers: list[float] = Field(default_factory=lambda: [1, 2], min_length=1, max_length=3)


def available(row, buffer):
    stamp = row_time(row)
    systime = row.get("SYSTIME")
    return max(stamp + buffer, parse_time(systime) if systime else stamp + buffer)


class AsOf:
    def __init__(self, rows, buffer):
        self.rows = sorted(((available(r, buffer), row_time(r), r) for r in rows), key=lambda x: (x[0], x[1]))
        # A late correction of an older interval must not overwrite a newer interval.
        self.times, self.values = [], []
        latest = -1
        for at, end, row in self.rows:
            if end >= latest:
                self.times.append(at)
                self.values.append(row)
                latest = end

    def get(self, ts, age):
        index = bisect.bisect_right(self.times, ts) - 1
        if index < 0:
            return None
        row = self.values[index]
        return row if 0 <= ts - row_time(row) <= age else None


def finite_positive(value):
    return isinstance(value, (float, int)) and math.isfinite(value) and value > 0


def execution_quote(leg, delta, ob, trade, policy: LiquiditySpec, factor=1):
    """All inputs must already be available. FO spread_l1 is bps, not price units."""
    if not ob or not trade:
        return None, "missing_or_stale_liquidity"
    if not all(finite_positive(ob.get(k)) for k in ("mid_price", "spread_l1")):
        return None, "invalid_spread"
    side = "s" if delta > 0 else "b"
    quantity = abs(delta)
    volume = trade.get("vol")
    if not finite_positive(volume) or quantity > volume / 5 * policy.max_minute_volume_fraction:
        return None, "volume_capacity"
    depth = ob.get(f"vol_{side}_l1")
    if not finite_positive(depth) or quantity > depth * policy.max_depth_fraction:
        return None, "depth_capacity"
    spread_points = ob["mid_price"] * ob["spread_l1"] / 10000
    # Stress half spread + explicit latency/impact allowance; never infer L1 from VWAP L20.
    adverse = (spread_points / 2 + policy.impact_ticks * leg.tick_size * (1 + quantity / depth)) * factor
    fees = quantity * policy.fee_rub_per_contract * factor
    return {
        "half_spread_points": spread_points / 2,
        "adverse_points": adverse,
        "fee_rub": fees,
        "cost_rub": quantity * leg.point_value_rub * adverse + fees,
        "depth_contracts": depth,
        "lagged_5m_volume": volume,
        "ob_interval_end": row_time(ob),
        "trade_interval_end": row_time(trade),
        "ob_available_at": available(ob, policy.publication_buffer_seconds),
        "trade_available_at": available(trade, policy.publication_buffer_seconds),
    }, None


def replay_targets(data, plan, snapshot, policy, *, test, size=1, factor=1):
    spec = plan.protocol
    if not 1 <= size <= 1000 or not 1 <= factor <= 10:
        raise ValueError("invalid size or cost stress")
    original = simulate(snapshot, spec, test=test)
    targets = original["fills"]  # frozen decisions from the original registered protocol
    symbols = [leg.symbol for leg in spec.legs]
    candles = {s: {} for s in symbols}
    for row in data["candles_1m"]:
        if row["secid"] not in candles:
            continue
        ts = parse_time(row["begin"])
        if not all(finite_positive(row.get(k)) for k in ("open", "high", "low", "close")):
            raise ValueError("invalid minute OHLC")
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
            raise ValueError("minute OHLC bounds invalid")
        if ts in candles[row["secid"]]:
            raise ValueError("duplicate minute candle")
        candles[row["secid"]][ts] = row
    indices = {
        product: {
            s: AsOf([r for r in data[product] if r["secid"] == s], policy.publication_buffer_seconds)
            for s in symbols
        }
        for product in ("tradestats", "obstats")
    }
    common = sorted(set.intersection(*(set(candles[s]) for s in symbols)))
    split = parse_time(str(spec.test_start) + " 00:00:00")
    start = parse_time(str(spec.start) + " 00:00:00")
    end = parse_time(str(spec.end) + " 00:00:00")
    common = [ts for ts in common if start <= ts < end and ((ts >= split) if test else (ts < split))]
    if not common:
        raise ValueError("no synchronized minute bars for this partition")
    cash = costs = peak = drawdown = 0.0
    position = target = intent_index = 0
    intent_at = None
    signal_at = None
    entry_day = None
    fills, rejects, equity = [], {}, []
    late_exit = 0
    margin_breaches = 0
    marked = 0.0
    for ts in common:
        now = datetime.fromtimestamp(ts, MSK)
        while intent_index < len(targets) and targets[intent_index]["ts"] <= ts:
            intent = targets[intent_index]
            target = intent["position_after"] * size
            intent_at, signal_at = intent["ts"], intent["signal_available_at"]
            intent_index += 1
        # Enforce the same known time window on actual, potentially delayed exposure.
        flatten = (
            now.hour >= spec.flatten_hour_msk
            or now.hour < spec.entry_hour_msk
            or (position and entry_day != now.date())
        )
        desired = 0 if flatten else target
        if desired != 0 and not spec.entry_hour_msk <= now.hour < spec.last_entry_hour_msk:
            desired = position
        obs = {s: indices["obstats"][s].get(ts, policy.max_quote_age_seconds) for s in symbols}
        trades = {s: indices["tradestats"][s].get(ts, policy.max_quote_age_seconds) for s in symbols}
        if desired != position:
            estimates = []
            reason = None
            for leg in spec.legs:
                q, failure = execution_quote(
                    leg,
                    (desired - position) * leg.quantity,
                    obs[leg.symbol],
                    trades[leg.symbol],
                    policy,
                    factor,
                )
                if failure:
                    reason = failure
                    break
                estimates.append(q)
            gross_margin = sum(
                abs(desired * leg.quantity) * (trades[leg.symbol].get("im") or 0)
                for leg in spec.legs
                if trades[leg.symbol]
            )
            reducing = desired == 0 or (desired * position > 0 and abs(desired) < abs(position))
            if not reason and not reducing:
                if any(not finite_positive(trades[leg.symbol].get("im")) for leg in spec.legs):
                    reason = "missing_margin"
                elif gross_margin > policy.capital_rub + marked:
                    reason = "insufficient_margin"
            if reason:
                rejects[reason] = rejects.get(reason, 0) + 1
            else:
                delta = desired - position
                cost = sum(q["cost_rub"] for q in estimates)
                value = sum(
                    leg.quantity * leg.point_value_rub * candles[leg.symbol][ts]["open"] for leg in spec.legs
                )
                cash -= delta * value + cost
                costs += cost
                if position and (
                    entry_day != now.date()
                    or now.hour > spec.flatten_hour_msk
                    or (now.hour == spec.flatten_hour_msk and now.minute > 0)
                ):
                    late_exit += 1
                fills.append(
                    {
                        "ts": ts,
                        "intent_at": intent_at,
                        "signal_available_at": signal_at,
                        "position_before": position,
                        "position_after": desired,
                        "cost_rub": cost,
                        "gross_margin_estimate_rub": gross_margin,
                        "legs": [
                            {
                                "symbol": leg.symbol,
                                "contracts_delta": delta * leg.quantity,
                                "reference_open": candles[leg.symbol][ts]["open"],
                                "scenario_fill_price": candles[leg.symbol][ts]["open"]
                                + math.copysign(q["adverse_points"], delta * leg.quantity),
                                **q,
                            }
                            for leg, q in zip(spec.legs, estimates, strict=True)
                        ],
                    }
                )
                if desired and (not position or desired * position < 0):
                    entry_day = now.date()
                position = desired
        marked = cash + position * sum(
            leg.quantity * leg.point_value_rub * candles[leg.symbol][ts]["close"] for leg in spec.legs
        )
        margin = sum(
            abs(position * leg.quantity) * (trades[leg.symbol].get("im") or 0)
            for leg in spec.legs
            if trades[leg.symbol]
        )
        if margin > policy.capital_rub + marked:
            margin_breaches += 1
        peak = max(peak, marked)
        drawdown = max(drawdown, peak - marked)
        equity.append({"ts": ts + 60, "pnl_rub": round(marked, 6)})
    return {
        "partition": "OOS" if test else "train",
        "size_multiplier": size,
        "cost_multiplier": factor,
        "scenario_marked_pnl_rub": round(marked, 6),
        "cost_rub": round(costs, 6),
        "max_drawdown_rub": round(drawdown, 6),
        "residual_position": position,
        "late_exit_events": late_exit,
        "margin_breach_minutes": margin_breaches,
        "rejected_execution_minutes": rejects,
        "fills_count": len(fills),
        "round_trips": sum(x["position_before"] != 0 for x in fills),
        "common_minutes": len(common),
        "original_reference_pnl_rub": original["scenario_marked_pnl_rub"],
        "fills": fills,
        "equity": equity,
    }


LIMITATIONS = [
    "Cost attribution replay of previously inspected hourly targets; this is not a new untouched holdout.",
    "Lagged five-minute OBStats is aggregate liquidity, not a historical executable L1 quote.",
    "Positive publication buffer and SYSTIME are enforced; SYSTIME is not certified historical received_at.",
    "FO spread_l1 is basis points, converted using mid_price/10000; fees and impact remain explicit scenarios.",
    "Volume capacity uses only prior 5m volume/5; no current minute final volume is used to approve a fill.",
    "Both legs must pass the capacity filter; simultaneous all-or-none execution remains an unverified assumption.",
    "Missing exits retain exposure and mark-to-market. Residual position is not forcibly closed on the last candle.",
    "Margin uses lagged per-contract im without spread credit; a complete variation-margin/funding ledger is absent.",
    "Historical session/contract economics verification and tick/legging tests remain required before paper/live claims.",
]


def run_liquidity(policy: LiquiditySpec, root=None, source_state=None):
    root = Path(root) if root else PROJECT / "var/algopack"
    with job_lock(root / "studies" / digest(policy.model_dump(mode="json"))):
        return _run_liquidity(policy, root, source_state)


def _run_liquidity(policy: LiquiditySpec, root, source_state=None):
    root = Path(root) if root else PROJECT / "var/algopack"
    if any(not 1 <= n <= 1000 for n in policy.size_multipliers) or any(
        not 1 <= n <= 10 for n in policy.cost_multipliers
    ):
        raise ValueError("invalid stress grid")
    identity = digest(policy.model_dump(mode="json"))
    folder = root / "studies" / identity
    if (folder / "result.json").exists():
        return verify_study(root, identity)
    folder.mkdir(parents=True, exist_ok=True)
    store = Store(source_state or PROJECT / "var/openrouter")
    plan_pair = store.checkpoint(policy.source_run_id, "research_plan")
    data_pair = store.checkpoint(policy.source_run_id, "research_data")
    if not plan_pair or not data_pair:
        raise ValueError("source run lacks a frozen research plan/dataset")
    plan = ResearchPlan.model_validate(plan_pair[1])
    snapshot = DatasetSnapshot.model_validate(data_pair[1])
    audit_data(snapshot, plan.protocol)
    save_json(
        folder / "protocol.json",
        {
            "created_at": utcnow(),
            "policy": policy.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
            "source_artifacts": [plan_pair[0].sha256, data_pair[0].sha256],
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    manifest, data = read_dataset(root, policy.dataset_id)
    if not {"candles_1m", "tradestats", "obstats"} <= set(data):
        raise ValueError("liquidity replay needs minute candles, TradeStats and OBStats")
    request = manifest["request"]
    if request["start"] > str(plan.protocol.start) or request["end"] < str(plan.protocol.end):
        raise ValueError("requested data interval does not cover the source protocol")
    results = [
        replay_targets(data, plan, snapshot, policy, test=test, size=size, factor=factor)
        for test in (False, True)
        for size in policy.size_multipliers
        for factor in policy.cost_multipliers
    ]
    result = {
        "study_id": identity,
        "policy": policy.model_dump(mode="json"),
        "decision": "inconclusive",
        "dataset_manifest_sha256": digest(manifest),
        "results": results,
        "limitations": LIMITATIONS,
    }
    save_json(folder / "result.json", result)
    save_json(
        folder / "integrity.json",
        {
            "result_sha256": digest(result),
            "protocol_sha256": hashlib.sha256((folder / "protocol.json").read_bytes()).hexdigest(),
        },
    )
    lines = [
        "# AlgoPack: ликвидность и издержки SiM6/SiU6",
        "",
        "Статус: **inconclusive**. Сценарий исполнения на реальных агрегатах ликвидности.",
        "Исходные часовые решения сохранены; меняются цены исполнения, издержки и возможность исполнения. Подбора параметров по OOS нет.",
        "",
        "| Период | Размер | Издержки × | PnL, RUB | Издержки, RUB | Просадка | Исполнения | Остаток |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['partition']} | {r['size_multiplier']} | {r['cost_multiplier']} | {r['scenario_marked_pnl_rub']:.2f} | {r['cost_rub']:.2f} | {r['max_drawdown_rub']:.2f} | {r['fills_count']} | {r['residual_position']} |"
        )
    lines += ["", "## Ограничения", ""] + ["- " + x for x in LIMITATIONS]
    lines += [
        "",
        f"Dataset: `{policy.dataset_id}`; study: `{identity}`.",
        "Журнал каждого исполнения, отказы, временные метки статистики и кривые PnL: result.json.",
    ]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "study_id": identity,
        "decision": "inconclusive",
        "report": str(folder / "report.md"),
        "summaries": [{k: v for k, v in r.items() if k not in {"fills", "equity"}} for r in results],
    }


def verify_study(root, identity):
    import re

    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("invalid study ID")
    folder = Path(root) / "studies" / identity
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    integrity = json.loads((folder / "integrity.json").read_text(encoding="utf-8"))
    if (
        digest(saved) != integrity["result_sha256"]
        or hashlib.sha256((folder / "protocol.json").read_bytes()).hexdigest() != integrity["protocol_sha256"]
    ):
        raise ValueError("study integrity mismatch")
    policy = LiquiditySpec.model_validate(protocol["policy"])
    if digest(policy.model_dump(mode="json")) != identity:
        raise ValueError("study protocol ID mismatch")
    manifest, data = read_dataset(root, policy.dataset_id)
    if digest(manifest) != saved["dataset_manifest_sha256"]:
        raise ValueError("study dataset manifest changed")
    plan = ResearchPlan.model_validate(protocol["plan"])
    snapshot = DatasetSnapshot.model_validate(protocol["snapshot"])
    audit_data(snapshot, plan.protocol)
    actual = [
        replay_targets(data, plan, snapshot, policy, test=test, size=size, factor=factor)
        for test in (False, True)
        for size in policy.size_multipliers
        for factor in policy.cost_multipliers
    ]
    if actual != saved["results"]:
        raise ValueError("liquidity numeric replay mismatch")
    return {
        "study_id": identity,
        "numeric_replay": "passed",
        "network_calls": 0,
        "decision": saved["decision"],
        "report": str(folder / "report.md"),
    }
