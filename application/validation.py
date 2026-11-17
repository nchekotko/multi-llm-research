from __future__ import annotations

import hashlib
import re
from decimal import Decimal, localcontext
from html import escape

from .config import digest
from .contracts import (
    CandidateContent,
    Check,
    EvidenceBundle,
    EvidenceSource,
    ExecutionPlan,
    Metric,
    ReportSelection,
    ReviewReport,
    TaskSpec,
    ValidationReport,
    default_plan,
    utcnow,
)
from .security import PolicyDenied


class GateFailed(ValueError):
    pass


def validate_plan(plan: ExecutionPlan, task: TaskSpec) -> None:
    nodes = {node.node_id: node for node in plan.nodes}
    if len(nodes) != len(plan.nodes):
        raise GateFailed("duplicate plan nodes")
    done = set()
    while len(done) < len(nodes):
        ready = {
            node_id for node_id, node in nodes.items() if node_id not in done and set(node.depends_on) <= done
        }
        if not ready:
            raise GateFailed("plan has a cycle or unknown dependency")
        done.update(ready)
    for node in nodes.values():
        if not set(node.tools) <= set(task.policy.allowed_tools):
            raise PolicyDenied("plan asks for a tool outside TaskSpec policy")
    expected = {n.node_id: n.model_dump(exclude={"schema_version"}) for n in default_plan().nodes}
    actual = {n.node_id: n.model_dump(exclude={"schema_version"}) for n in plan.nodes}
    if actual != expected:
        raise GateFailed("plan changes the registered evidence_report workflow or its gates")


def prepare_evidence(task: TaskSpec) -> EvidenceBundle:
    metrics = []
    with localcontext() as ctx:
        ctx.prec = 50
        for calc in task.calculations:
            values = calc.values
            if calc.operation == "sum":
                result = sum(values, Decimal(0))
            elif calc.operation == "mean":
                result = sum(values, Decimal(0)) / len(values)
            elif calc.operation == "min":
                result = min(values)
            elif calc.operation == "max":
                result = max(values)
            else:
                result = Decimal(len(values))
            metrics.append(
                Metric(
                    metric_id=calc.metric_id,
                    value=str(result),
                    unit=calc.unit,
                    operation=calc.operation,
                    source_ids=calc.source_ids,
                    input_sha256=digest(calc.model_dump(mode="json")),
                )
            )
    return EvidenceBundle(
        sources=[
            EvidenceSource(
                **s.model_dump(), sha256=hashlib.sha256(s.text.encode()).hexdigest(), retrieved_at=utcnow()
            )
            for s in task.sources
        ],
        metrics=metrics,
        gaps=[
            "Source assertions are not independently verified.",
            "Calculation inputs are explicitly supplied by the task author; they are not extracted or authenticated automatically.",
        ],
    )


def validate_candidate(candidate: CandidateContent, evidence: EvidenceBundle) -> ValidationReport:
    sources = {s.source_id: s for s in evidence.sources}
    ids = [c.claim_id for c in candidate.claims]
    known_metrics = {m.metric_id for m in evidence.metrics}
    checks = [
        Check(
            check_id="unique_claim_ids", passed=len(set(ids)) == len(ids), detail="claim IDs must be unique"
        ),
        Check(
            check_id="metric_coverage",
            passed=set(candidate.metric_ids) == known_metrics
            and len(candidate.metric_ids) == len(known_metrics),
            detail="include every registered computed metric exactly once",
        ),
        Check(
            check_id="limitations",
            passed={"source_truth_unverified", "coverage_limited"} <= set(candidate.limitation_codes),
            detail="source and coverage limits must be explicit",
        ),
    ]
    for claim in candidate.claims:
        source = sources.get(claim.source_id)
        checks.append(
            Check(
                check_id="quote_" + claim.claim_id,
                passed=source is not None and claim.quote in source.text,
                detail="verbatim quotation must match its referenced source",
            )
        )
    return ValidationReport(status="passed" if all(c.passed for c in checks) else "failed", checks=checks)


def validate_review(review: ReviewReport, refs: set[str]) -> None:
    for finding in review.findings:
        if finding.evidence_ref not in refs:
            raise GateFailed("review finding references evidence outside the supplied package")
    if review.status == "changes_required" and not review.findings and not review.unresolved_questions:
        raise GateFailed("changes_required must explain the blocking concern")
    for resolution in review.resolutions:
        if resolution.evidence_ref not in refs:
            raise GateFailed("resolution references evidence outside the supplied package")


def validate_review_transition(
    previous: ReviewReport | None,
    current: ReviewReport,
    previous_hash: str | None,
    current_hash: str,
    validation_id: str,
) -> None:
    required = (
        {f.finding_id for f in previous.findings if f.severity in {"critical", "high"}} if previous else set()
    )
    retained = {f.finding_id for f in current.findings if f.severity in {"critical", "high"}}
    resolved = {r.finding_id for r in current.resolutions}
    if resolved - required:
        raise GateFailed("review resolves an unknown or non-blocking finding")
    if required - retained - resolved:
        raise GateFailed("previous blocking finding disappeared without a verified resolution")
    for resolution in current.resolutions:
        if current_hash == previous_hash or resolution.evidence_ref != "artifact://" + validation_id:
            raise GateFailed(
                "blocking resolution needs a changed candidate and its current validation artifact"
            )


def validate_selection(selection: ReportSelection, candidate: CandidateContent) -> None:
    ids = {c.claim_id for c in candidate.claims}
    if set(selection.claim_ids) != ids or len(selection.claim_ids) != len(ids):
        raise GateFailed("reporter omitted, duplicated or invented a claim")
    mids = set(candidate.metric_ids)
    if set(selection.metric_ids) != mids or len(selection.metric_ids) != len(mids):
        raise GateFailed("reporter omitted, duplicated or invented a metric")


LIMITATIONS = {
    "source_truth_unverified": "Цитаты сверены с переданными материалами; достоверность самих источников независимо не подтверждалась.",
    "coverage_limited": "Выводы ограничены переданными источниками и зарегистрированными расчётами. Полнота и соответствие предметным критериям требуют проверки человеком.",
    "offline_demo": "Офлайн-демонстрация на детерминированных ответах: качество реальных LLM и независимость их ошибок не измерялись.",
    "human_review_required": "Для предметной приёмки требуется проверка человеком.",
}


def markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escape(value))


def render_report(
    task: TaskSpec,
    candidate: CandidateContent,
    evidence: EvidenceBundle,
    selection: ReportSelection,
    demo: bool,
    contract_pilot: bool = False,
) -> tuple[str, list[str]]:
    validate_selection(selection, candidate)
    claims = {c.claim_id: c for c in candidate.claims}
    metrics = {m.metric_id: m for m in evidence.metrics}
    codes = set(candidate.limitation_codes) | {
        "source_truth_unverified",
        "coverage_limited",
        "human_review_required",
    }
    if demo:
        codes.add("offline_demo")
    limitations = [LIMITATIONS[c] for c in sorted(codes)]
    if contract_pilot:
        limitations.append(
            "Пилот реальных моделей: проверена только совместимость контрактов на синтетическом примере; предметное качество и польза маршрутизации ещё не подтверждены."
        )
    if evidence.metrics:
        limitations.append(
            "Входные значения расчётов и их связь с источниками заданы автором задачи; автоматическое извлечение и независимая проверка этих значений не выполнялись."
        )
    parts = [
        "# Результат по материалам задачи",
        "",
        markdown_text(task.goal),
        "",
        "## Подтверждённые цитаты",
        "",
    ]
    for claim_id in selection.claim_ids:
        claim = claims[claim_id]
        parts.extend(
            [
                f"Источник: {markdown_text(claim.source_id)}; цитата: {markdown_text(claim_id)}",
                "",
                "\n".join("> " + markdown_text(line) for line in claim.quote.splitlines()),
                "",
            ]
        )
    if selection.metric_ids:
        parts.extend(
            [
                "## Программные расчёты",
                "",
                "| Показатель | Значение | Операция | Источники |",
                "| --- | --- | --- | --- |",
            ]
        )
        for mid in selection.metric_ids:
            metric = metrics[mid]
            parts.append(
                "| "
                + " | ".join(
                    markdown_text(v)
                    for v in [
                        metric.metric_id,
                        metric.value + " " + metric.unit,
                        metric.operation,
                        ", ".join(metric.source_ids),
                    ]
                )
                + " |"
            )
        parts.append("")
    parts.extend(["## Ограничения", ""] + ["- " + line for line in limitations])
    return "\n".join(parts) + "\n", limitations
