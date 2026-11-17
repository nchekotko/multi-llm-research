from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid4().hex


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    schema_version: Literal["1.0"] = "1.0"


Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")]
NonEmpty = Annotated[str, Field(min_length=1, max_length=20000)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
ProviderName = Literal["demo", "openai", "anthropic", "gemini", "openrouter"]


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    VALIDATION_FAILED = "validation_failed"
    TIMED_OUT = "timed_out"
    INCONCLUSIVE = "inconclusive"


class Limits(Contract):
    max_cost_usd: Money = Decimal("1")
    max_calls: int = Field(default=12, ge=1, le=100)
    max_seconds: int = Field(default=300, ge=1, le=86400)
    max_repairs: int = Field(default=2, ge=0, le=2)
    max_escalations: int = Field(default=1, ge=0, le=1)
    transport_retries: int = Field(default=2, ge=0, le=3)


class DataPolicy(Contract):
    classification: Literal["public", "internal", "confidential"] = "internal"
    allow_external: bool = False
    allowed_providers: list[ProviderName] = Field(default_factory=lambda: ["demo"])
    allowed_tools: list[Literal["evidence", "aggregate", "python_sandbox"]] = Field(
        default_factory=lambda: ["evidence", "aggregate"]
    )
    require_human_release: bool = True


class Source(Contract):
    source_id: Identifier
    text: NonEmpty
    uri: str = Field(default="user-provided", min_length=1, max_length=2048)
    version: str = Field(default="1", min_length=1, max_length=100)
    available_at: datetime
    rights: NonEmpty

    @field_validator("available_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("available_at must have a timezone")
        return value


class Calculation(Contract):
    metric_id: Identifier
    operation: Literal["sum", "mean", "min", "max", "count"]
    values: list[Annotated[Decimal, Field(allow_inf_nan=False, max_digits=30, decimal_places=12)]] = Field(
        min_length=1, max_length=10000
    )
    unit: str = Field(default="", max_length=100)
    source_ids: list[Identifier] = Field(min_length=1)


class FuturesLeg(Contract):
    symbol: str = Field(pattern=r"^[A-Za-z]{1,10}[FGHJKMNQUVXZ][0-9]{1,2}$")
    quantity: int = Field(ge=-100, le=100)
    point_value_rub: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    tick_size: float = Field(gt=0, le=1000000, allow_inf_nan=False)

    @field_validator("quantity")
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("leg quantity cannot be zero")
        return value


class ResearchSpec(Contract):
    profile: Literal["moex_futures_1h_reference"] = "moex_futures_1h_reference"
    strategy: Literal["calendar_spread_zscore", "futures_trend"]
    legs: list[FuturesLeg] = Field(min_length=1, max_length=2)
    start: date
    end: date
    test_start: date
    lookback: int = Field(default=60, ge=3, le=1000)
    entry_z: float = Field(default=2, gt=0, le=10, allow_inf_nan=False)
    exit_z: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)
    fee_rub_per_contract: float = Field(default=5, ge=0, le=100000, allow_inf_nan=False)
    slippage_ticks: float = Field(default=2, ge=0, le=10000, allow_inf_nan=False)
    latency_seconds: int = Field(default=1, ge=1, le=3600)
    entry_hour_msk: int = Field(default=10, ge=0, le=22)
    last_entry_hour_msk: int = Field(default=16, ge=1, le=23)
    flatten_hour_msk: int = Field(default=17, ge=1, le=23)
    mode: Literal["exploratory", "execution"] = "exploratory"
    hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[a-f0-9]{16}$")
    hypothesis_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    revisit_reason: str | None = Field(default=None, min_length=10, max_length=2000)
    llm_review: bool = False

    @model_validator(mode="after")
    def valid_protocol(self):
        if not self.start < self.test_start < self.end:
            raise ValueError("require start < test_start < end (end exclusive)")
        if (self.end - self.start).days > 1096:
            raise ValueError("bounded reference research supports at most three years")
        if not self.entry_hour_msk < self.last_entry_hour_msk < self.flatten_hour_msk:
            raise ValueError("entry window must end before scheduled flatten")
        if self.exit_z >= self.entry_z:
            raise ValueError("exit_z must be below entry_z")
        if len({x.symbol for x in self.legs}) != len(self.legs):
            raise ValueError("duplicate legs")
        if self.strategy == "calendar_spread_zscore":
            if len(self.legs) != 2 or self.legs[0].quantity * self.legs[1].quantity >= 0:
                raise ValueError("calendar spread requires two opposite signed legs")
            roots = [leg.symbol[:-2] if leg.symbol[-2].isalpha() else leg.symbol[:-3] for leg in self.legs]
            if roots[0] != roots[1]:
                raise ValueError("calendar spread legs must share an underlying")
        elif len(self.legs) != 1:
            raise ValueError("trend requires one dated contract")
        if bool(self.hypothesis_id) != bool(self.hypothesis_revision):
            raise ValueError("registry selection requires ID and pinned revision")
        return self


class TaskSpec(Contract):
    task_id: Identifier = Field(default_factory=lambda: "task_" + new_id())
    goal: NonEmpty
    task_type: Literal["evidence_report", "quant_research"] = "evidence_report"
    sources: list[Source] = Field(default_factory=list, max_length=50)
    research: ResearchSpec | None = None
    calculations: list[Calculation] = Field(default_factory=list, max_length=100)
    constraints: list[NonEmpty] = Field(min_length=1, max_length=100)
    acceptance_criteria: list[NonEmpty] = Field(min_length=1, max_length=100)
    risk: Literal["low", "medium", "high"] = "low"
    policy: DataPolicy = Field(default_factory=DataPolicy)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def references(self) -> TaskSpec:
        if self.task_type == "evidence_report" and (not self.sources or self.research is not None):
            raise ValueError("evidence_report needs sources and cannot contain a research protocol")
        if self.task_type == "quant_research" and (
            self.research is None or self.sources or self.calculations
        ):
            raise ValueError("quant_research needs a research protocol; inputs come from the connector")
        if self.task_type == "quant_research" and not self.policy.require_human_release:
            raise ValueError("research packages require human review")
        ids = [s.source_id for s in self.sources]
        mids = [c.metric_id for c in self.calculations]
        if len(set(ids)) != len(ids) or len(set(mids)) != len(mids):
            raise ValueError("duplicate source or metric identifiers")
        for calc in self.calculations:
            if not set(calc.source_ids) <= set(ids):
                raise ValueError("calculation references unknown sources")
        if sum(len(s.text.encode()) for s in self.sources) > 200000:
            raise ValueError("source package exceeds 200000 bytes; chunk it explicitly")
        return self


class PlanNode(Contract):
    node_id: Identifier
    role: Literal["reader", "executor", "validator", "reviewer", "reporter"]
    depends_on: list[Identifier]
    tools: list[Literal["evidence", "aggregate"]]
    output_schema: Literal[
        "EvidenceBundle", "CandidateContent", "ValidationReport", "ReviewReport", "ReportSelection"
    ]
    gates: list[Literal["schema", "evidence", "numeric_lineage", "independent_review"]] = Field(min_length=1)


class ExecutionPlan(Contract):
    nodes: list[PlanNode] = Field(min_length=5, max_length=5)
    rationale: NonEmpty


def default_plan() -> ExecutionPlan:
    return ExecutionPlan(
        nodes=[
            PlanNode(
                node_id="prepare",
                role="reader",
                depends_on=[],
                tools=["evidence", "aggregate"],
                output_schema="EvidenceBundle",
                gates=["schema"],
            ),
            PlanNode(
                node_id="execute",
                role="executor",
                depends_on=["prepare"],
                tools=[],
                output_schema="CandidateContent",
                gates=["schema", "evidence"],
            ),
            PlanNode(
                node_id="validate",
                role="validator",
                depends_on=["execute", "prepare"],
                tools=[],
                output_schema="ValidationReport",
                gates=["evidence", "numeric_lineage"],
            ),
            PlanNode(
                node_id="review",
                role="reviewer",
                depends_on=["validate", "execute", "prepare"],
                tools=[],
                output_schema="ReviewReport",
                gates=["independent_review"],
            ),
            PlanNode(
                node_id="report",
                role="reporter",
                depends_on=["review", "execute", "prepare"],
                tools=[],
                output_schema="ReportSelection",
                gates=["evidence", "numeric_lineage"],
            ),
        ],
        rationale="Bounded evidence-report workflow with deterministic gates.",
    )


class EvidenceSource(Source):
    sha256: str
    retrieved_at: str


class Metric(Contract):
    metric_id: Identifier
    value: str
    unit: str
    operation: str
    source_ids: list[str]
    input_sha256: str


class EvidenceBundle(Contract):
    sources: list[EvidenceSource]
    metrics: list[Metric]
    gaps: list[str]


class QuoteClaim(Contract):
    claim_id: Identifier
    source_id: Identifier
    quote: NonEmpty


class CandidateContent(Contract):
    claims: list[QuoteClaim] = Field(min_length=1, max_length=100)
    metric_ids: list[Identifier]
    # Limitations are registered codes, so reporter cannot invent facts in prose.
    limitation_codes: list[
        Literal["source_truth_unverified", "coverage_limited", "offline_demo", "human_review_required"]
    ] = Field(min_length=1)


class Check(Contract):
    check_id: str
    passed: bool
    detail: str


class ValidationReport(Contract):
    validator_version: Literal["1.0"] = "1.0"
    status: Literal["passed", "failed"]
    checks: list[Check]


class Finding(Contract):
    finding_id: Identifier
    severity: Literal["critical", "high", "medium", "low"]
    rule_id: Identifier
    evidence_ref: str = Field(min_length=1)
    description: NonEmpty
    required_action: NonEmpty


class FindingResolution(Contract):
    finding_id: Identifier
    evidence_ref: str = Field(min_length=1)
    reason: NonEmpty


class ReviewReport(Contract):
    status: Literal["approved", "changes_required", "inconclusive"]
    findings: list[Finding]
    unresolved_questions: list[NonEmpty]
    resolutions: list[FindingResolution] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_self_clear(self) -> ReviewReport:
        if self.status == "approved" and (
            self.unresolved_questions or any(f.severity in {"critical", "high"} for f in self.findings)
        ):
            raise ValueError("approved review has blocking findings or questions")
        ids = [f.finding_id for f in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate finding IDs")
        resolved = [r.finding_id for r in self.resolutions]
        if len(resolved) != len(set(resolved)) or set(resolved) & set(ids):
            raise ValueError("ambiguous review resolutions")
        return self


class ReportSelection(Contract):
    claim_ids: list[Identifier] = Field(min_length=1)
    metric_ids: list[Identifier]


class CandidateArtifact(Contract):
    artifact_id: str
    kind: str
    version: int = 1
    uri: str
    sha256: str
    parent_ids: list[str]
    created_by: str
    created_at: str


class FinalPackage(Contract):
    task_id: str
    run_id: str
    status: Literal["pending_approval", "accepted"]
    markdown: str
    evidence_refs: list[str]
    numeric_lineage: list[Metric]
    limitations: list[str]
    candidate_artifact_id: str
    validation_artifact_id: str
    review_artifact_id: str


class RunManifest(Contract):
    run_id: str
    status: RunStatus
    workflow_version: str
    code_version: str
    schema_digest: str
    config_sha256: str
    task_sha256: str
    environment: dict
    artifacts: list[CandidateArtifact]
    timeline: list[dict]
    calls: list[dict]
    approvals: list[dict]
    accounting: dict
    limitations: list[str]


class ResearchPlan(Contract):
    engine_version: Literal["moex-reference/1.0"] = "moex-reference/1.0"
    protocol: ResearchSpec
    hypothesis: dict | None = None
    rules: list[str]


class HourBar(Contract):
    ts: int = Field(ge=0)
    available_at: int = Field(ge=0)
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    volume: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_bar(self):
        if self.available_at < self.ts + 3600:
            raise ValueError("hourly close is not available before the end of its interval")
        if (
            self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or self.low > self.high
        ):
            raise ValueError("invalid OHLC bounds")
        return self


class DatasetSnapshot(Contract):
    connector: Literal["economic-calendar/tradfi_1h"] = "economic-calendar/tradfi_1h"
    captured_at: str
    query: dict
    source_metadata: dict
    series: dict[str, list[HourBar]]
    content_sha256: str


class DataAuditReport(Contract):
    status: Literal["reference_only", "blocked"]
    statistics: dict
    gaps: list[dict]
    assumptions: list[str]


class BacktestReport(Contract):
    engine_version: Literal["moex-reference/1.0"] = "moex-reference/1.0"
    decision: Literal["inconclusive", "blocked"]
    protocol_sha256: str
    dataset_sha256: str
    train: dict
    out_of_sample: dict
    cost_sensitivity: list[dict]
    blockers: list[str]


class ResearchAssessment(Contract):
    summary: NonEmpty
    concerns: list[NonEmpty] = Field(max_length=20)
    next_experiments: list[NonEmpty] = Field(max_length=10)


class ResearchPackage(Contract):
    task_id: str
    run_id: str
    status: Literal["pending_approval"] = "pending_approval"
    decision: Literal["inconclusive", "blocked"]
    markdown: str
    plan_artifact_id: str
    dataset_artifact_id: str
    audit_artifact_id: str
    backtest_artifact_id: str
    assessment_artifact_id: str | None = None
    review_artifact_id: str | None = None


CONTRACTS = {
    c.__name__: c
    for c in [
        TaskSpec,
        ExecutionPlan,
        EvidenceBundle,
        CandidateContent,
        CandidateArtifact,
        ValidationReport,
        ReviewReport,
        ReportSelection,
        FinalPackage,
        RunManifest,
        ResearchPlan,
        DatasetSnapshot,
        DataAuditReport,
        BacktestReport,
        ResearchAssessment,
        ResearchPackage,
    ]
}
