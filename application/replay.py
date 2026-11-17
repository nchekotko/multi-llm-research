from __future__ import annotations

import hashlib
import json

from .config import AppConfig, digest
from .contracts import (
    CONTRACTS,
    CandidateContent,
    EvidenceBundle,
    FinalPackage,
    ReportSelection,
    ReviewReport,
    TaskSpec,
    ValidationReport,
)
from .storage import Conflict, Store
from .validation import (
    GateFailed,
    prepare_evidence,
    render_report,
    validate_candidate,
    validate_plan,
    validate_review,
    validate_review_transition,
)


def replay(store: Store, run_id: str) -> dict:
    """Recompute local gates with saved inputs. This function never calls a model."""
    run = store.run(run_id)
    task = TaskSpec.model_validate_json(run["task_json"])
    if (
        digest(json.loads(run["task_json"])) != run["task_hash"]
        or digest(json.loads(run["config_json"])) != run["config_hash"]
    ):
        raise Conflict("task/config snapshot integrity failed")
    if task.task_type == "quant_research":
        from .research import replay_research

        return replay_research(store, run_id, task)
    artifacts = store.artifacts(run_id)
    ids = {a.artifact_id for a in artifacts}
    for meta in artifacts:
        if not set(meta.parent_ids) <= ids:
            raise Conflict("missing artifact lineage")
        _, body = store.artifact(run_id, meta.artifact_id)
        if meta.kind not in CONTRACTS:
            raise Conflict("unregistered contract in artifact")
        CONTRACTS[meta.kind].model_validate(body)
        if meta.kind == "ExecutionPlan":
            validate_plan(CONTRACTS[meta.kind].model_validate(body), task)
    previous_review = previous_candidate_hash = None
    metadata = {a.artifact_id: a for a in artifacts}
    for review_meta in (a for a in artifacts if a.kind == "ReviewReport"):
        current = ReviewReport.model_validate(store.artifact(run_id, review_meta.artifact_id)[1])
        candidates = [metadata[p] for p in review_meta.parent_ids if metadata[p].kind == "CandidateContent"]
        validations = [metadata[p] for p in review_meta.parent_ids if metadata[p].kind == "ValidationReport"]
        if len(candidates) != 1 or len(validations) != 1:
            raise Conflict("review lineage must identify one candidate and its validation")
        validate_review(current, {"artifact://" + p for p in review_meta.parent_ids})
        validate_review_transition(
            previous_review,
            current,
            previous_candidate_hash,
            candidates[0].sha256,
            validations[0].artifact_id,
        )
        previous_review, previous_candidate_hash = current, candidates[0].sha256
    if run["final_id"] is None:
        return {
            "run_id": run_id,
            "status": "partial",
            "integrity": "passed",
            "artifact_count": len(artifacts),
            "model_calls": 0,
        }
    final_meta, final_body = store.artifact(run_id, run["final_id"])
    final = FinalPackage.model_validate(final_body)
    if final.run_id != run_id or final.task_id != task.task_id:
        raise Conflict("final package belongs to another task/run")
    evidence_artifacts = [a for a in artifacts if a.kind == "EvidenceBundle"]
    if len(evidence_artifacts) != 1:
        raise Conflict("evidence bundle is ambiguous")
    evidence_meta = evidence_artifacts[0]
    evidence = EvidenceBundle.model_validate(store.artifact(run_id, evidence_meta.artifact_id)[1])
    recomputed = prepare_evidence(task)
    if [m.model_dump() for m in recomputed.metrics] != [m.model_dump() for m in evidence.metrics]:
        raise GateFailed("computed metric replay mismatch")
    for original, saved in zip(task.sources, evidence.sources, strict=True):
        if saved.sha256 != hashlib.sha256(
            original.text.encode()
        ).hexdigest() or original.model_dump() != saved.model_dump(exclude={"sha256", "retrieved_at"}):
            raise GateFailed("source replay mismatch")
    candidate = CandidateContent.model_validate(store.artifact(run_id, final.candidate_artifact_id)[1])
    validation = validate_candidate(candidate, evidence)
    saved_validation = ValidationReport.model_validate(
        store.artifact(run_id, final.validation_artifact_id)[1]
    )
    if validation.status != "passed" or validation != saved_validation:
        raise GateFailed("candidate no longer passes reproducible gates")
    review = ReviewReport.model_validate(store.artifact(run_id, final.review_artifact_id)[1])
    refs = {
        "artifact://" + aid
        for aid in (final.candidate_artifact_id, final.validation_artifact_id, evidence_meta.artifact_id)
    }
    validate_review(review, refs)
    if review.status != "approved":
        raise GateFailed("release without approved review")
    selection_saved = store.checkpoint(run_id, "report")
    if selection_saved is None:
        raise Conflict("missing report selection")
    selection = ReportSelection.model_validate(selection_saved[1])
    calls = store.calls(run_id)
    demo = any(c["provider"] == "demo" for c in calls)
    config = AppConfig.model_validate_json(run["config_json"])
    markdown, limitations = render_report(
        task, candidate, evidence, selection, demo, config.qualification_scope == "contract_pilot"
    )
    if (
        final.markdown != markdown
        or final.limitations != limitations
        or final.numeric_lineage != evidence.metrics
    ):
        raise GateFailed("final package or numeric lineage replay mismatch")
    if final.evidence_refs != ["artifact://" + evidence_meta.artifact_id]:
        raise GateFailed("final evidence refs mismatch")
    for approval in store.approvals(run_id):
        if approval["package_hash"] != final_meta.sha256:
            raise Conflict("approval package hash mismatch")
    return {
        "run_id": run_id,
        "status": "passed",
        "integrity": "passed",
        "artifact_count": len(artifacts),
        "metric_count": len(evidence.metrics),
        "model_calls": 0,
        "package_sha256": final_meta.sha256,
        "semantic_correctness": "requires independent review and human acceptance",
    }
