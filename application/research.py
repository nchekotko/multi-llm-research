"""Deterministic reference experiments, not an exchange execution simulator.

No generated Python, parameter optimization, continuous contracts, forward fill,
full-sample normalization, candle-high/low fills or model-calculated statistics.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

from .config import digest
from .contracts import (
    BacktestReport,
    DataAuditReport,
    DatasetSnapshot,
    ResearchAssessment,
    ResearchPackage,
    ResearchPlan,
    ResearchSpec,
    ReviewReport,
)
from .economic_calendar import EconomicCalendar
from .storage import BudgetExceeded, Conflict
from .validation import GateFailed, validate_review

MSK = timezone(timedelta(hours=3))
RULES = [
    "Freeze parameters, date range and OOS split before reading outcome data; no optimizer.",
    "Only dated contracts; synchronize all legs by exact bar timestamp, never forward fill.",
    "Signal uses closes available by bar start+3600; fill at a later observed open after positive latency.",
    "Rolling statistics use trailing observations only. Fixed quantities and scenario point values.",
    "Charge adverse slippage and fees on every changed contract, including both spread legs.",
    "Scheduled Moscow-hour flatten is a scenario, not a verified historical exchange calendar.",
    "Missing exit bars keep exposure and PnL; never discard a day based on its future completeness.",
    "No last-bar forced fill. Residual exposure is marked, not silently closed at an unknowable last price.",
    "LLM review cannot clear missing-data gates, calculate returns or authorize trading.",
]
GAPS = [
    (
        "publication_times",
        "Historical close/publication timestamps and ingestion latency are absent.",
        "Record bar start/end, exchange timestamp, received_at and versioned corrections.",
    ),
    (
        "bid_ask_depth",
        "Hourly OHLC cannot prove simultaneous executable leg prices or capacity.",
        "Load timestamped bid/ask, size, trades and legging/partial-fill data for these exact contracts and dates.",
    ),
    (
        "historical_sessions",
        "No versioned session, clearing, auction, holiday and tradability calendar.",
        "Join date-effective MOEX calendars; require an executable scheduled intraday exit.",
    ),
    (
        "contract_specs",
        "Point values, ticks and quantities are scenario inputs, not verified historical specifications.",
        "Load date-effective contract tick value, price step, settlement currency and expiry/delivery rules.",
    ),
    (
        "fees_margin",
        "Fees/slippage are assumptions; no broker tariffs, historical margin or cash ledger.",
        "Load effective tariffs, margin, variation-margin cash flows and funding assumptions.",
    ),
    (
        "selection_bias",
        "Local source coverage and a chosen contract pair are not a point-in-time market universe.",
        "Preserve all research attempts, PIT listings/delistings, missing periods and a separate untouched holdout.",
    ),
]


def joined(snapshot: DatasetSnapshot, spec: ResearchSpec):
    maps = {leg.symbol: {bar.ts: bar for bar in snapshot.series.get(leg.symbol, [])} for leg in spec.legs}
    common = set.intersection(*(set(values) for values in maps.values()))
    return [(ts, [maps[leg.symbol][ts] for leg in spec.legs]) for ts in sorted(common)]


def audit_data(snapshot: DatasetSnapshot, spec: ResearchSpec) -> DataAuditReport:
    payload = {key: [bar.model_dump(mode="json") for bar in bars] for key, bars in snapshot.series.items()}
    if digest(payload) != snapshot.content_sha256:
        raise GateFailed("dataset content digest mismatch")
    if set(snapshot.series) != {x.symbol for x in spec.legs}:
        raise GateFailed("dataset legs differ from the frozen protocol")
    start = int(datetime.combine(spec.start, datetime.min.time(), MSK).timestamp())
    end = int(datetime.combine(spec.end, datetime.min.time(), MSK).timestamp())
    expected_query = {
        "database": "tradfi_1h",
        "table": "bars",
        "source": "moex",
        "tf": "1h",
        "start_ts_inclusive": start,
        "end_ts_exclusive": end,
        "symbols": [x.symbol for x in spec.legs],
        "max_rows_per_leg": 40000,
    }
    if snapshot.query != expected_query:
        raise GateFailed("dataset query differs from frozen protocol")
    stats = {"series": {}}
    errors = []
    for symbol, bars in snapshot.series.items():
        timestamps = [bar.ts for bar in bars]
        if timestamps != sorted(set(timestamps)) or len(timestamps) > 40000:
            raise GateFailed("series contains duplicates, unordered timestamps or exceeds row cap")
        if any(not start <= ts < end for ts in timestamps):
            raise GateFailed("series outside frozen date range")
        stats["series"][symbol] = {
            "rows": len(bars),
            "first_ts": timestamps[0] if bars else None,
            "last_ts": timestamps[-1] if bars else None,
            "gaps_over_1h": sum(b - a > 3600 for a, b in zip(timestamps, timestamps[1:], strict=False)),
            "zero_volume_rows": sum(bar.volume == 0 for bar in bars),
        }
        if not bars:
            errors.append(
                {
                    "code": "empty_series",
                    "detail": symbol + " has no bars in requested period",
                    "required": "Acquire this dated contract; do not substitute a continuous series.",
                }
            )
    rows = joined(snapshot, spec)
    stats["synchronized_rows"] = len(rows)
    stats["unmatched_rows"] = {symbol: len(bars) - len(rows) for symbol, bars in snapshot.series.items()}
    split = int(datetime.combine(spec.test_start, datetime.min.time(), MSK).timestamp())
    stats["train_rows"] = sum(ts < split for ts, _ in rows)
    stats["oos_rows"] = sum(ts >= split for ts, _ in rows)
    if stats["train_rows"] < spec.lookback + 3 or stats["oos_rows"] < 30:
        errors.append(
            {
                "code": "insufficient_sample",
                "detail": "Too few synchronized training or OOS bars.",
                "required": "Acquire more data or preregister a different study. Do not optimize on OOS.",
            }
        )
    gaps = [
        {"code": code, "detail": detail, "required": required} for code, detail, required in GAPS
    ] + errors
    return DataAuditReport(
        status="blocked" if errors or spec.mode == "execution" else "reference_only",
        statistics=stats,
        gaps=gaps,
        assumptions=[
            "1h timestamp means interval start; close available no earlier than ts+3600.",
            "Date bounds use Moscow time; end is exclusive.",
            "Open is a price scenario, not a proven executable quote.",
            "Observed hourly opens may delay a one-second-latency order by a full hour.",
            "Current exchange schedules are never retroactively applied to historical data.",
        ],
    )


def simulate(
    snapshot: DatasetSnapshot, spec: ResearchSpec, *, test: bool, cost_factor=1.0, check=lambda: None
) -> dict:
    """Process each synchronized OPEN, then its CLOSE. No later bar enters a signal."""
    rows = joined(snapshot, spec)
    split = int(datetime.combine(spec.test_start, datetime.min.time(), MSK).timestamp())
    history = deque(maxlen=spec.lookback)
    pending = []
    position = 0
    cash = peak = drawdown = total_cost = 0.0
    fills = []
    equity = []
    overnight = late_exits = 0
    entry_day = None
    last_close_value = 0.0
    for index, (ts, bars) in enumerate(rows):
        if index % 100 == 0:
            check()
        active = (ts >= split) if test else (ts < split)
        dt = datetime.fromtimestamp(ts, MSK)
        open_value = sum(
            leg.quantity * leg.point_value_rub * bar.open for leg, bar in zip(spec.legs, bars, strict=True)
        )
        close_value = sum(
            leg.quantity * leg.point_value_rub * bar.close for leg, bar in zip(spec.legs, bars, strict=True)
        )
        if active:
            ready = [item for item in pending if item[0] <= ts]
            pending = [item for item in pending if item[0] > ts]
            target = ready[-1][1] if ready else position
            signal_time = ready[-1][2] if ready else None
            reason = "lagged_signal"
            # This rule is known before the sample. Missing bars do not erase positions.
            if position and entry_day != dt.date():
                overnight += 1
            if (
                dt.hour >= spec.flatten_hour_msk
                or dt.hour < spec.entry_hour_msk
                or (position and entry_day != dt.date())
            ):
                if position and (dt.hour > spec.flatten_hour_msk or entry_day != dt.date()):
                    late_exits += 1
                target, reason, signal_time = 0, "scheduled_flatten_at_first_observed_open", None
            elif not spec.entry_hour_msk <= dt.hour < spec.last_entry_hour_msk:
                target = 0 if target == 0 else position
            if target != position:
                delta = target - position
                costs = (
                    abs(delta)
                    * sum(
                        abs(leg.quantity)
                        * (
                            spec.fee_rub_per_contract
                            + spec.slippage_ticks * leg.tick_size * leg.point_value_rub
                        )
                        for leg in spec.legs
                    )
                    * cost_factor
                )
                cash -= delta * open_value + costs
                total_cost += costs
                fills.append(
                    {
                        "ts": ts,
                        "signal_available_at": signal_time,
                        "position_before": position,
                        "position_after": target,
                        "reason": reason,
                        "cost_rub": round(costs, 8),
                        "reference_prices": {
                            leg.symbol: bar.open for leg, bar in zip(spec.legs, bars, strict=True)
                        },
                        "legs": [
                            {
                                "symbol": leg.symbol,
                                "contracts_delta": delta * leg.quantity,
                                "scenario_fill_price": bar.open
                                + math.copysign(
                                    spec.slippage_ticks * cost_factor * leg.tick_size, delta * leg.quantity
                                ),
                                "fee_rub": abs(delta * leg.quantity)
                                * spec.fee_rub_per_contract
                                * cost_factor,
                            }
                            for leg, bar in zip(spec.legs, bars, strict=True)
                        ],
                    }
                )
                if position == 0 and target:
                    entry_day = dt.date()
                position = target
            last_close_value = close_value
            marked = cash + position * close_value
            peak = max(peak, marked)
            drawdown = max(drawdown, peak - marked)
            equity.append({"ts": max(bar.available_at for bar in bars), "pnl_rub": round(marked, 8)})
        history.append(close_value)
        available = max(bar.available_at for bar in bars)
        # A late publication cannot be used to reorder information into the past.
        if index + 1 < len(rows) and available > rows[index + 1][0] + 3600:
            raise GateFailed("out-of-order bar publication requires event-time engine")
        if len(history) == spec.lookback:
            average = fmean(history)
            if spec.strategy == "calendar_spread_zscore":
                sd = pstdev(history)
                z = (close_value - average) / sd if sd else 0.0
                target = -1 if z >= spec.entry_z else 1 if z <= -spec.entry_z else position
                if abs(z) <= spec.exit_z:
                    target = 0
            else:
                target = 1 if close_value > average else -1 if close_value < average else 0
            # Keep OOS warmup signals but never training exposure or future-fitted parameters.
            pending.append((available + spec.latency_seconds, target, available))
    pnl = cash + position * last_close_value
    numbers = [cash, pnl, total_cost, drawdown]
    if not all(math.isfinite(value) for value in numbers):
        raise GateFailed("non-finite numerical result")
    return {
        "label": "out_of_sample" if test else "training_diagnostic",
        "scenario_marked_pnl_rub": round(pnl, 8),
        "cost_rub": round(total_cost, 8),
        "max_marked_drawdown_rub": round(drawdown, 8),
        "fills_count": len(fills),
        "round_trips": sum(x["position_before"] != 0 for x in fills),
        "residual_position": position,
        "overnight_breaches": overnight,
        "late_exit_events": late_exits,
        "cash_baseline_pnl_rub": 0.0,
        "fills": fills,
        "equity": equity,
        "return_or_sharpe": None,
        "capital_basis": "No validated margin/capital ledger; PnL is a scenario only.",
    }


def backtest(snapshot, spec, audit, check=lambda: None) -> BacktestReport:
    blocked = audit.status == "blocked"
    train = {} if blocked else simulate(snapshot, spec, test=False, check=check)
    oos = {} if blocked else simulate(snapshot, spec, test=True, check=check)
    sensitivity = []
    if not blocked:
        for factor in (1.0, 2.0, 4.0):
            result = (
                oos if factor == 1 else simulate(snapshot, spec, test=True, cost_factor=factor, check=check)
            )
            sensitivity.append(
                {
                    "cost_multiplier": factor,
                    "scenario_marked_pnl_rub": result["scenario_marked_pnl_rub"],
                    "cost_rub": result["cost_rub"],
                }
            )
    blockers = [item["code"] for item in audit.gaps]
    if not blocked and oos["round_trips"] < 30:
        blockers.append("too_few_oos_round_trips")
    if not blocked and (oos["residual_position"] or oos["overnight_breaches"]):
        blockers.append("intraday_exit_not_proven")
    return BacktestReport(
        decision="blocked" if blocked else "inconclusive",
        protocol_sha256=digest(spec.model_dump(mode="json")),
        dataset_sha256=snapshot.content_sha256,
        train=train,
        out_of_sample=oos,
        cost_sensitivity=sensitivity,
        blockers=blockers,
    )


def render_research(plan, snapshot, audit, result, run_id, review=None, assessment=None) -> str:
    spec = plan.protocol
    lines = [
        "# MOEX: исследовательский расчёт",
        "",
        f"Запуск: `{run_id}`.",
        f"Статус: **{result.decision}**. Достоверность исполнения и пригодность к торговле не подтверждены.",
        "",
        f"Стратегия: `{spec.strategy}`. Контракты: {', '.join(x.symbol for x in spec.legs)}.",
        f"Период: {spec.start} — {spec.end} (правая граница исключена); OOS с {spec.test_start}.",
        f"Синхронных часовых наблюдений: {audit.statistics['synchronized_rows']}.",
        f"Параметры: lookback={spec.lookback}, entry_z={spec.entry_z}, exit_z={spec.exit_z}; оптимизатора нет.",
        f"Комиссия: {spec.fee_rub_per_contract} RUB/контракт/сторону; проскальзывание {spec.slippage_ticks} тика/сторону.",
        f"Стоимость пункта и тики заданы сценарием: {', '.join(f'{x.symbol}: {x.point_value_rub} RUB/пункт, тик {x.tick_size}' for x in spec.legs)}.",
        f"Окно новых входов MSK [{spec.entry_hour_msk}, {spec.last_entry_hour_msk}); закрытие с {spec.flatten_hour_msk}:00 на первом доступном open.",
        "",
        "## Программные результаты",
        "",
        "| Период | PnL сценария, RUB | Издержки, RUB | Просадка, RUB | Изменения позиции | Завершённые сделки |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, values in [("Train", result.train), ("OOS", result.out_of_sample)]:
        if values:
            lines.append(
                f"| {name} | {values['scenario_marked_pnl_rub']:.2f} | {values['cost_rub']:.2f} | {values['max_marked_drawdown_rub']:.2f} | {values['fills_count']} | {values['round_trips']} |"
            )
    if not result.out_of_sample:
        lines.append("\nРасчёт заблокирован аудитом; торговые результаты не вычислялись.")
    else:
        oos = result.out_of_sample
        lines += [
            "",
            f"Незакрытая позиция OOS: {oos['residual_position']}; нарушений внутридневного выхода: {oos['overnight_breaches']}; задержанных выходов: {oos['late_exit_events']}.",
            "База сравнения — отсутствие позиции (PnL 0). Доходность на ГО и Sharpe не рассчитываются без проверенного капитального счёта.",
            "PnL включает переоценку открытой позиции; последняя свеча не считается гарантированным выходом.",
            "",
            "Чувствительность к издержкам:",
        ]
        lines += [
            f"- Издержки ×{x['cost_multiplier']:g}: PnL {x['scenario_marked_pnl_rub']:.2f} RUB."
            for x in result.cost_sensitivity
        ]
    lines += ["", "## Что необходимо для достоверного бэктеста", ""]
    lines += [f"- **{item['code']}**: {item['detail']} Требуется: {item['required']}" for item in audit.gaps]
    lines += ["", "## Контроль подглядывания", ""] + [f"- {rule}" for rule in plan.rules]
    lines += ["", "## Границы расчёта", ""] + [f"- {item}" for item in audit.assumptions]
    if plan.hypothesis:
        hyp = plan.hypothesis
        lines += [
            "",
            f"Реестр: {hyp['hypothesis_id']}, статус {hyp['status']}, ревизия {hyp['revision']}, строки {hyp['line_start']}–{hyp['line_end']}. Старые результаты не являются доказательством нового эксперимента.",
        ]
    lines += [
        "",
        f"SHA256 параметров: `{result.protocol_sha256}`.",
        f"SHA256 строк данных: `{snapshot.content_sha256}`.",
        "Источник: Economic Calendar / tradfi_1h.db / bars / source=moex. Снимок и журнал сделок сохранены в артефактах запуска.",
        "Модель не вычисляет PnL и не снимает блокировки данных.",
    ]
    if assessment:
        lines += ["", "## Интерпретация LLM (требует проверки)", "", assessment.summary]
        lines += [f"- Ограничение: {x}" for x in assessment.concerns]
        lines += [f"- Новый непроверенный эксперимент: {x}" for x in assessment.next_experiments]
        lines += [
            "",
            "Квалификация моделей по контрактам JSON не является проверкой качества финансовых исследований.",
        ]
    if review:
        lines += [
            "",
            f"LLM-рецензия: {review.status}; замечаний {len(review.findings)}; открытых вопросов {len(review.unresolved_questions)}.",
        ]
        lines += [f"- {x.severity}: {x.description} Действие: {x.required_action}" for x in review.findings]
        lines += [f"- Открытый вопрос: {x}" for x in review.unresolved_questions]
    else:
        lines += [
            "LLM-рецензия не запрашивалась; это локальный программный расчёт на реальных исторических данных."
        ]
    return "\n".join(lines) + "\n"


def run_research(engine, run_id, task, config, owner):
    spec = task.research

    def check():
        engine._check(run_id, owner)

    if not {"evidence", "aggregate"} <= set(task.policy.allowed_tools):
        from .security import PolicyDenied

        raise PolicyDenied("research requires evidence and aggregate tool permissions")
    review_config = config.model_copy(update={"allow_same_family_review": False})
    if spec.llm_review:
        if task.limits.max_calls < 2:
            raise BudgetExceeded("research analysis and independent review require two calls")
        # Preserve existing provider/data qualification restrictions. Never downgrade internal data.
        analyst_candidate = config.route("executor", task, 0)[0]
        review_config.route("reviewer", task, 0, author_family=analyst_candidate.family)
    saved = engine.store.checkpoint(run_id, "research_plan")
    if saved:
        plan_meta, body = saved
        plan = ResearchPlan.model_validate(body)
    else:
        hypothesis = EconomicCalendar().hypothesis(spec) if spec.hypothesis_id else None
        plan = ResearchPlan(protocol=spec, hypothesis=hypothesis, rules=RULES)
        plan_meta = engine._artifact(run_id, "research_plan", "ResearchPlan", plan, [], owner)
    if plan.protocol != spec or plan.rules != RULES:
        raise Conflict("frozen research plan differs from task or implementation")
    check()
    saved = engine.store.checkpoint(run_id, "research_data")
    snapshot = DatasetSnapshot.model_validate(saved[1]) if saved else EconomicCalendar().snapshot(spec, check)
    data_meta = engine._artifact(
        run_id, "research_data", "DatasetSnapshot", snapshot, [plan_meta.artifact_id], owner
    )
    audit = audit_data(snapshot, spec)
    audit_meta = engine._artifact(
        run_id, "research_audit", "DataAuditReport", audit, [data_meta.artifact_id], owner
    )
    result = backtest(snapshot, spec, audit, check)
    result_meta = engine._artifact(
        run_id,
        "research_backtest",
        "BacktestReport",
        result,
        [plan_meta.artifact_id, data_meta.artifact_id, audit_meta.artifact_id],
        owner,
    )
    review = review_meta = assessment = assessment_meta = None
    if spec.llm_review:
        # Only bounded computed results and audit are sent, never raw registry prose, paths or table dumps.
        summary = result.model_dump(mode="json", exclude={"train", "out_of_sample"})
        summary["oos"] = {k: v for k, v in result.out_of_sample.items() if k not in {"fills", "equity"}}
        context = {
            "protocol": spec.model_dump(
                mode="json", exclude={"hypothesis_id", "hypothesis_revision", "revisit_reason"}
            ),
            "results": summary,
            "data_audit": audit.model_dump(mode="json"),
            "rules": RULES,
        }
        assessment, family = engine._llm(
            run_id,
            "research_assessment",
            "executor",
            task,
            config,
            ResearchAssessment,
            context,
            owner,
            prompt_variant="research_analyst",
        )
        assessment_meta = engine._artifact(
            run_id,
            "research_assessment",
            "ResearchAssessment",
            assessment,
            [result_meta.artifact_id, audit_meta.artifact_id],
            owner,
        )
        review, _ = engine._llm(
            run_id,
            "research_review",
            "reviewer",
            task,
            review_config,
            ReviewReport,
            {
                "task": {
                    "goal": "Review the MOEX reference methodology and data gaps. Do not authorize trading."
                },
                "candidate": assessment.model_dump(mode="json"),
                "evidence": context,
                "validation": {"execution_certified": False},
                "artifact_refs": [
                    "artifact://" + x.artifact_id for x in [assessment_meta, result_meta, audit_meta]
                ],
                "artifact_map": {
                    name: {
                        "ref": "artifact://" + meta.artifact_id,
                        "sha256": meta.sha256,
                        "created_at": meta.created_at,
                    }
                    for name, meta in [
                        ("ResearchPlan", plan_meta),
                        ("DatasetSnapshot", data_meta),
                        ("DataAuditReport", audit_meta),
                        ("BacktestReport", result_meta),
                        ("ResearchAssessment", assessment_meta),
                    ]
                },
                "preregistration_scope": {
                    "protocol_committed_at": plan_meta.created_at,
                    "data_captured_at": snapshot.captured_at,
                    "proof_scope": "Local durable commit order only, not independent external timestamping.",
                    "holdout_status": "Control pipeline experiment; prior human access to these prices is not ruled out. New strategy choices require a new untouched holdout.",
                },
            },
            owner,
            author_family=family,
            prompt_variant="research_reviewer",
        )
        validate_review(
            review, {"artifact://" + x.artifact_id for x in [assessment_meta, result_meta, audit_meta]}
        )
        review_meta = engine._artifact(
            run_id,
            "research_review",
            "ReviewReport",
            review,
            [assessment_meta.artifact_id, result_meta.artifact_id, audit_meta.artifact_id],
            owner,
        )
    final = ResearchPackage(
        task_id=task.task_id,
        run_id=run_id,
        decision=result.decision,
        markdown=render_research(plan, snapshot, audit, result, run_id, review, assessment),
        plan_artifact_id=plan_meta.artifact_id,
        dataset_artifact_id=data_meta.artifact_id,
        audit_artifact_id=audit_meta.artifact_id,
        backtest_artifact_id=result_meta.artifact_id,
        assessment_artifact_id=assessment_meta.artifact_id if assessment_meta else None,
        review_artifact_id=review_meta.artifact_id if review_meta else None,
    )
    parents = [plan_meta.artifact_id, data_meta.artifact_id, audit_meta.artifact_id, result_meta.artifact_id]
    if review_meta:
        parents.extend([assessment_meta.artifact_id, review_meta.artifact_id])
    final_meta = engine._artifact(run_id, "research_final", "ResearchPackage", final, parents, owner)
    check()
    engine.store.set_final(run_id, final_meta.artifact_id, owner)
    engine.store.status(run_id, "waiting_approval", "research decision: " + result.decision, owner)


def replay_research(store, run_id, task) -> dict:
    """Read immutable artifacts only; never connect to the mutable source databases."""
    from .contracts import CONTRACTS

    artifacts = store.artifacts(run_id)
    metadata = {x.artifact_id: x for x in artifacts}
    for meta in artifacts:
        _, body = store.artifact(run_id, meta.artifact_id)
        if meta.kind not in CONTRACTS or not set(meta.parent_ids) <= set(metadata):
            raise Conflict("invalid research artifact lineage")
        CONTRACTS[meta.kind].model_validate(body)
    run = store.run(run_id)
    if not run["final_id"]:
        return {"run_id": run_id, "status": "partial", "integrity": "passed", "model_calls": 0}
    final_meta, body = store.artifact(run_id, run["final_id"])
    final = ResearchPackage.model_validate(body)
    if final.task_id != task.task_id or final.run_id != run_id:
        raise Conflict("research package belongs to a different task")

    def load(artifact_id, contract):
        meta, value = store.artifact(run_id, artifact_id)
        if meta.kind != contract.__name__ or artifact_id not in final_meta.parent_ids:
            raise Conflict("research final references wrong artifact kind or lineage")
        return contract.model_validate(value)

    plan = load(final.plan_artifact_id, ResearchPlan)
    snapshot = load(final.dataset_artifact_id, DatasetSnapshot)
    audit = load(final.audit_artifact_id, DataAuditReport)
    result = load(final.backtest_artifact_id, BacktestReport)
    expected_parents = {
        final.plan_artifact_id: [],
        final.dataset_artifact_id: [final.plan_artifact_id],
        final.audit_artifact_id: [final.dataset_artifact_id],
        final.backtest_artifact_id: [
            final.plan_artifact_id,
            final.dataset_artifact_id,
            final.audit_artifact_id,
        ],
    }
    if plan.protocol != task.research or plan.rules != RULES:
        raise Conflict("research plan does not match frozen task")
    if audit_data(snapshot, plan.protocol) != audit or backtest(snapshot, plan.protocol, audit) != result:
        raise GateFailed("deterministic research replay mismatch")
    review = load(final.review_artifact_id, ReviewReport) if final.review_artifact_id else None
    assessment = (
        load(final.assessment_artifact_id, ResearchAssessment) if final.assessment_artifact_id else None
    )
    if review:
        validate_review(
            review,
            {
                "artifact://" + x
                for x in [final.assessment_artifact_id, final.backtest_artifact_id, final.audit_artifact_id]
            },
        )
        expected_parents[final.assessment_artifact_id] = [final.backtest_artifact_id, final.audit_artifact_id]
        expected_parents[final.review_artifact_id] = [
            final.assessment_artifact_id,
            final.backtest_artifact_id,
            final.audit_artifact_id,
        ]
    if bool(review) != plan.protocol.llm_review or bool(assessment) != plan.protocol.llm_review:
        raise GateFailed("required model review missing")
    if review:
        import json

        families = []
        for node, contract, value in [
            ("research_assessment", ResearchAssessment, assessment),
            ("research_review", ReviewReport, review),
        ]:
            calls = [call for call in store.calls(run_id, node) if call["status"] == "succeeded"]
            if not calls:
                raise Conflict("research model artifact has no saved receipt")
            call = calls[-1]
            receipt = json.loads(call["response_json"])
            if (
                receipt["termination"] != "completed"
                or contract.model_validate_json(receipt["text"]) != value
            ):
                raise Conflict("research model artifact differs from saved receipt")
            families.append(call["family"])
        if families[0] == families[1]:
            raise GateFailed("research review must use a different model family")
    if set(final_meta.parent_ids) != set(expected_parents):
        raise Conflict("unexpected research final lineage")
    for artifact_id, parents in expected_parents.items():
        if metadata[artifact_id].parent_ids != parents:
            raise Conflict("research node lineage mismatch")
    if final.decision != result.decision or final.markdown != render_research(
        plan, snapshot, audit, result, run_id, review, assessment
    ):
        raise GateFailed("research report differs from program-calculated results")
    return {
        "run_id": run_id,
        "status": "passed",
        "integrity": "passed",
        "numeric_replay": "passed",
        "decision": result.decision,
        "model_calls": 0,
        "source_database_reads": 0,
    }
