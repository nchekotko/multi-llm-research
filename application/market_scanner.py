"""Whole-FO anomaly discovery. Alerts are research candidates, never trading advice."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from statistics import median

from pydantic import Field

from .algopack import MSK, PROJECT, job_lock, read_dataset, row_time, save_json
from .config import digest
from .contracts import Contract, utcnow
from .liquidity import available, finite_positive


class ScanSpec(Contract):
    dataset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    as_of: datetime
    publication_buffer_seconds: int = Field(default=60, ge=1, le=3600)
    min_baseline_observations: int = Field(default=24, ge=12, le=500)
    min_baseline_dates: int = Field(default=3, ge=2, le=30)
    z_threshold: float = Field(default=3.5, ge=2, le=10)
    imbalance_threshold: float = Field(default=0.65, ge=0.3, le=0.95)
    min_5m_contracts: int = Field(default=20, ge=1, le=100000)
    max_spread_bps: float = Field(default=50, gt=0, le=1000)
    persistence_intervals: int = Field(default=3, ge=2, le=12)
    cooldown_seconds: int = Field(default=1800, ge=300, le=86400)


def ratio(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if not math.isfinite(a + b) or a < 0 or b < 0 or a + b <= 0:
        return None
    return (a - b) / (a + b)


def features(trade, book, orders):
    orders = orders or {}
    keys = ("pr_open", "pr_close", "vol")
    if any(not finite_positive(trade.get(k)) for k in keys):
        return None
    if any(not finite_positive(book.get(k)) for k in ("mid_price", "spread_l1", "vol_b_l1", "vol_s_l1")):
        return None
    imbalance = ratio(book.get("vol_b_l1"), book.get("vol_s_l1"))
    flow = ratio(trade.get("vol_b"), trade.get("vol_s"))
    placement = ratio(orders.get("put_vol_b"), orders.get("put_vol_s"))
    cancel = ratio(orders.get("cancel_vol_b"), orders.get("cancel_vol_s"))
    if any(x is None for x in (imbalance, flow)):
        return None
    oi0, oi1 = trade.get("oi_open"), trade.get("oi_close")
    return {
        "book_imbalance": imbalance,
        "flow_imbalance": flow,
        "placement_imbalance": placement,
        "cancel_imbalance": cancel,
        "log_volume": math.log1p(trade["vol"]),
        "spread_bps": book["spread_l1"],
        "log_depth": math.log1p(book["vol_b_l1"] + book["vol_s_l1"]),
        "return_bps": (trade["pr_close"] / trade["pr_open"] - 1) * 10000,
        "oi_change_pct": 100 * (oi1 / oi0 - 1) if finite_positive(oi0) and finite_positive(oi1) else None,
        "volume_contracts": trade["vol"],
        "depth_bid": book["vol_b_l1"],
        "depth_ask": book["vol_s_l1"],
    }


def robust_z(value, history):
    center = median(history)
    mad = median(abs(x - center) for x in history)
    if mad <= 1e-9:
        return None  # no artificial infinite scores on constant baselines
    return (value - center) / (1.4826 * mad)


def scan(data, spec: ScanSpec):
    if spec.as_of.tzinfo is None:
        raise ValueError("scanner as_of must include timezone")
    cutoff = int(spec.as_of.timestamp())
    maps = {
        p: {(r["secid"], row_time(r)): r for r in data[p]} for p in ("tradestats", "obstats", "orderstats")
    }
    for p, index in maps.items():
        if len(index) != len(data[p]):
            raise ValueError("duplicate scanner instrument/interval")
    universe = sorted({key[0] for index in maps.values() for key in index})
    common = set(maps["tradestats"]) & set(maps["obstats"])
    # Sort by availability, not interval timestamp; corrections cannot leak into old baselines.
    sequence = sorted(
        (
            max(available(maps[p][key], spec.publication_buffer_seconds) for p in ("tradestats", "obstats")),
            key,
        )
        for key in common
    )
    history = defaultdict(lambda: deque(maxlen=600))
    recent = defaultdict(lambda: deque(maxlen=spec.persistence_intervals))
    last_alert = {}
    latest = {}
    exclusions = Counter()
    events = []
    tested = 0
    for at, (symbol, end) in sequence:
        if at > cutoff:
            continue
        if symbol in latest and end <= latest[symbol]:
            exclusions["late_old_interval"] += 1
            continue
        latest[symbol] = end
        trade, book = (maps[p][(symbol, end)] for p in ("tradestats", "obstats"))
        orders = maps["orderstats"].get((symbol, end))
        if orders and available(orders, spec.publication_buffer_seconds) > at:
            orders = None
        if orders is None:
            exclusions["orderstats_unavailable_cancel_detector_disabled"] += 1
        f = features(trade, book, orders)
        if not f:
            exclusions["invalid_or_missing_fields"] += 1
            continue
        if f["volume_contracts"] < spec.min_5m_contracts or f["spread_bps"] > spec.max_spread_bps:
            exclusions["liquidity_filter"] += 1
            recent[symbol].clear()
            continue
        dt = datetime.fromtimestamp(end, MSK)
        key = (symbol, dt.hour)
        past = [item for item in history[key] if item[0] < dt.date().isoformat()]
        # Same instrument / Moscow hour, earlier dates only. Current observation is appended AFTER scoring.
        if len(past) < spec.min_baseline_observations or len({x[0] for x in past}) < spec.min_baseline_dates:
            exclusions["baseline_too_short"] += 1
            history[key].append((dt.date().isoformat(), f))
            recent[symbol].append((end, f))
            continue
        zs = {}
        for name in (
            "book_imbalance",
            "log_volume",
            "spread_bps",
            "log_depth",
            "placement_imbalance",
            "cancel_imbalance",
        ):
            baseline = [item for item in past if item[1][name] is not None]
            zs[name] = (
                robust_z(f[name], [item[1][name] for item in baseline])
                if f[name] is not None
                and len(baseline) >= spec.min_baseline_observations
                and len({item[0] for item in baseline}) >= spec.min_baseline_dates
                else None
            )
        recent[symbol].append((end, f))
        trail = list(recent[symbol])
        persistent = len(trail) == spec.persistence_intervals and all(
            b[0] - a[0] == 300 for a, b in zip(trail, trail[1:], strict=False)
        )
        persistent = persistent and all(
            abs(x[1]["book_imbalance"]) >= spec.imbalance_threshold
            and x[1]["book_imbalance"] * f["book_imbalance"] > 0
            for x in trail
        )
        candidates = []
        threshold = spec.z_threshold
        if (
            persistent
            and zs["book_imbalance"] is not None
            and abs(zs["book_imbalance"]) >= threshold
            and f["flow_imbalance"] * f["book_imbalance"] > 0.15
        ):
            candidates.append(
                ("persistent_book_and_flow", "continuation_candidate", abs(zs["book_imbalance"]))
            )
        if (
            zs["log_volume"] is not None
            and zs["log_volume"] >= threshold
            and abs(f["flow_imbalance"]) >= 0.5
            and abs(f["return_bps"]) <= f["spread_bps"]
        ):
            candidates.append(("absorption_proxy", "reversal_or_continuation_unresolved", zs["log_volume"]))
        if (
            zs["cancel_imbalance"] is not None
            and f["placement_imbalance"] is not None
            and abs(zs["cancel_imbalance"]) >= threshold
            and f["cancel_imbalance"] * f["placement_imbalance"] < -0.2
        ):
            candidates.append(
                ("asymmetric_cancel_replace", "liquidity_migration_candidate", abs(zs["cancel_imbalance"]))
            )
        if (
            zs["spread_bps"] is not None
            and zs["log_depth"] is not None
            and zs["spread_bps"] >= threshold
            and zs["log_depth"] <= -threshold
        ):
            candidates.append(
                ("liquidity_vacuum", "risk_alert_no_direction", min(zs["spread_bps"], -zs["log_depth"]))
            )
        if (
            zs["log_volume"] is not None
            and zs["log_volume"] >= threshold
            and f["oi_change_pct"] is not None
            and abs(f["oi_change_pct"]) >= 0.1
        ):
            candidates.append(("volume_and_oi_shock", "new_or_closing_exposure_candidate", zs["log_volume"]))
        tested += 4 + int(zs["cancel_imbalance"] is not None and f["placement_imbalance"] is not None)
        for family, interpretation, score in candidates:
            cooldown_key = (symbol, family)
            if at - last_alert.get(cooldown_key, -(10**15)) < spec.cooldown_seconds:
                continue
            last_alert[cooldown_key] = at
            events.append(
                {
                    "symbol": symbol,
                    "asset_code": trade.get("asset_code"),
                    "interval_end": end,
                    "available_at": at,
                    "family": family,
                    "interpretation": interpretation,
                    "score": round(score, 4),
                    "features": f,
                    "robust_z": zs,
                    "baseline_observations": len(past),
                    "baseline_dates": len({x[0] for x in past}),
                    "status": "unvalidated_candidate",
                    "not_spoofing_evidence": True,
                }
            )
        history[key].append((dt.date().isoformat(), f))
    events.sort(key=lambda x: (x["available_at"], x["symbol"], x["family"]))
    return {
        "universe": universe,
        "universe_size": len(universe),
        "synchronized_intervals": len(common),
        "evaluated_hypotheses": tested,
        "exclusions": dict(exclusions),
        "events_count": len(events),
        "events": events,
        "family_counts": dict(Counter(x["family"] for x in events)),
        "product_rows_by_date": {p: dict(Counter(r["tradedate"] for r in rows)) for p, rows in data.items()},
        "missing_product_intervals": {p: len(common - set(index)) for p, index in maps.items()},
        "latest_seen": latest,
        "status": "research_candidates_only",
        "multiple_testing": "Scores are not p-values. All tested families and candidates are retained; no significance or profitability claim.",
        "coverage": "All instruments returned by FO AlgoPack, not all MOEX asset classes or a certified PIT listing universe.",
    }


def run_scan(spec: ScanSpec, root=None):
    root = Path(root) if root else PROJECT / "var/algopack"
    with job_lock(root / "scans" / digest(spec.model_dump(mode="json"))):
        return _run_scan(spec, root)


def _run_scan(spec: ScanSpec, root):
    root = Path(root) if root else PROJECT / "var/algopack"
    identity = digest(spec.model_dump(mode="json"))
    folder = root / "scans" / identity
    if (folder / "result.json").exists():
        return verify_scan(root, identity)
    manifest, data = read_dataset(root, spec.dataset_id)
    if not manifest["request"]["whole_futures_market"]:
        raise ValueError("market scanner requires a whole-futures-market dataset")
    if not {"tradestats", "obstats", "orderstats"} <= set(data):
        raise ValueError("scanner needs TradeStats, OBStats, OrderStats")
    save_json(
        folder / "protocol.json",
        {
            "created_at": utcnow(),
            "spec": spec.model_dump(mode="json"),
            "dataset_manifest_sha256": digest(manifest),
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    result = scan(data, spec)
    save_json(folder / "result.json", result)
    save_json(
        folder / "integrity.json",
        {
            "result_sha256": digest(result),
            "protocol_sha256": hashlib.sha256((folder / "protocol.json").read_bytes()).hexdigest(),
        },
    )
    top = sorted(result["events"], key=lambda x: -x["score"])[:30]
    lines = [
        "# Сканер срочного рынка MOEX",
        "",
        f"Инструментов в выгрузке: {result['universe_size']}; кандидатов: {result['events_count']}; проверенных условий: {result['evaluated_hypotheses']}.",
        "Все события — непроверенные гипотезы. Рейтинг не является вероятностью успеха или сигналом купить/продать.",
        "Сравнение: тот же контракт и московский час, только предыдущие даты; медиана/MAD; текущий интервал исключён из базы.",
        "Пятиминутные агрегаты не позволяют идентифицировать отдельную крупную заявку, её владельца или spoofing.",
        "При отсутствии OrderStats детектор отмен отключается для интервала; остальные семьи продолжают работать на TradeStats и OBStats.",
        "",
        "| Контракт | Время доступности MSK | Семейство | Robust score |",
        "|---|---|---|---:|",
    ]
    lines += [
        f"| {x['symbol']} | {datetime.fromtimestamp(x['available_at'], MSK).isoformat()} | {x['family']} | {x['score']:.2f} |"
        for x in top
    ]
    lines += [
        "",
        "## Полнота и отказы",
        "",
        json.dumps(result["exclusions"], ensure_ascii=False),
        "",
        "История короткая: первый прогон проверяет охват и механику сканера. Для устойчивого baseline нужно 20–60 торговых дней, отдельные режимы и неприкосновенный OOS.",
        "Все кандидаты, включая не вошедшие в топ, сохранены в result.json. Будущая доходность при отборе не используется.",
        "Сканер работает по всему доступному FO-разделу; акции, облигации и опционы требуют отдельных схем и модулей.",
    ]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "scan_id": identity,
        "report": str(folder / "report.md"),
        **{k: v for k, v in result.items() if k not in {"events", "latest_seen", "universe"}},
    }


def verify_scan(root, identity):
    import re

    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("invalid scan ID")
    folder = Path(root) / "scans" / identity
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    integrity = json.loads((folder / "integrity.json").read_text(encoding="utf-8"))
    spec = ScanSpec.model_validate(protocol["spec"])
    if (
        digest(spec.model_dump(mode="json")) != identity
        or digest(saved) != integrity["result_sha256"]
        or hashlib.sha256((folder / "protocol.json").read_bytes()).hexdigest() != integrity["protocol_sha256"]
    ):
        raise ValueError("scanner integrity mismatch")
    manifest, data = read_dataset(root, spec.dataset_id)
    if digest(manifest) != protocol["dataset_manifest_sha256"] or scan(data, spec) != saved:
        raise ValueError("scanner replay mismatch")
    return {
        "scan_id": identity,
        "numeric_replay": "passed",
        "network_calls": 0,
        "events_count": saved["events_count"],
        "report": str(folder / "report.md"),
    }
