from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import AppConfig, canonical, digest
from .contracts import (
    CONTRACTS,
    CandidateContent,
    ExecutionPlan,
    FinalPackage,
    ReportSelection,
    ReviewReport,
    RunManifest,
    TaskSpec,
    default_plan,
    new_id,
)
from .providers import Gateway, ProviderFailure, actual_cost, estimate_reservation
from .roles import PROMPT_VERSION, PROMPTS, prompt_hash
from .security import PolicyDenied, ensure_no_secrets, safe_error
from .storage import BudgetExceeded, Conflict, Store
from .validation import (
    GateFailed,
    prepare_evidence,
    render_report,
    validate_candidate,
    validate_plan,
    validate_review,
    validate_review_transition,
    validate_selection,
)

WORKFLOW_VERSION = "evidence-report/1.0"


def code_digest() -> str:
    root = Path(__file__).parent
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))})


def schemas_digest() -> str:
    return digest({name: c.model_json_schema() for name, c in CONTRACTS.items()})


class Engine:
    def __init__(self, store: Store, config: AppConfig, gateway: Gateway | None = None):
        self.store, self.config, self.gateway = store, config, gateway or Gateway()

    def submit(self, task: TaskSpec, idempotency_key: str | None = None) -> str:
        return self.store.create_run(
            task, self.config.model_dump(mode="json"), code_digest(), schemas_digest(), idempotency_key
        )

    def _check(self, run_id: str, owner: str):
        run = self.store.run(run_id)
        if run["cancel_requested"]:
            raise InterruptedError("cancel requested")
        if run["owner"] != owner or run["lease_until"] < time.time():
            raise Conflict("worker lost lease")
        if time.time() >= run["deadline"]:
            raise TimeoutError("run deadline reached")
        calls = self.store.calls(run_id)
        spent = sum(
            c["actual_microusd"] if c["actual_microusd"] is not None else c["reserved_microusd"]
            for c in calls
        )
        task = TaskSpec.model_validate_json(run["task_json"])
        if spent > int(task.limits.max_cost_usd * 1000000):
            raise BudgetExceeded("accounted cost exceeds budget")

    def _llm(
        self,
        run_id: str,
        node_id: str,
        role: str,
        task: TaskSpec,
        config: AppConfig,
        output_type,
        data: dict,
        owner: str,
        author_family: str | None = None,
        prompt_variant: str | None = None,
    ):
        base_request = {
            "role": role,
            "prompt_version": PROMPT_VERSION,
            "system": PROMPTS[prompt_variant or role],
            "output_type": output_type.__name__,
            "output_schema": output_type.model_json_schema(),
            "input": data,
        }
        logical_hash = digest(base_request)
        ensure_no_secrets(base_request)
        retryable_codes = {
            "http_408",
            "http_429",
            "http_500",
            "http_502",
            "http_503",
            "http_504",
            "transport_timeout",
            "transport_error",
            "worker_lost",
            "interrupted_or_rejected",
        }
        while True:
            self._check(run_id, owner)
            history = self.store.calls(run_id, node_id)
            if any(
                json.loads(c["request_json"]).get("logical_request_hash") != logical_hash for c in history
            ):
                raise Conflict("node input differs from its durable call history")
            last = history[-1] if history else None
            if last and last["status"] == "succeeded":
                # Revalidate the saved receipt, including one persisted immediately before a crash.
                receipt = json.loads(last["response_json"])
                if receipt["termination"] == "refused":
                    raise ProviderFailure("model_refusal")
                if receipt["termination"] == "model_mismatch":
                    raise PolicyDenied("OpenRouter returned a different model than requested")
                try:
                    if receipt["termination"] != "completed":
                        raise GateFailed("incomplete generation")
                    return output_type.model_validate_json(receipt["text"]), last["family"]
                except (ValidationError, GateFailed):
                    self.store.invalidate_call(last["id"], "contract_validation")
                    continue
            request = {**base_request, "logical_request_hash": logical_hash}
            semantic_failures = sum(c["status"] == "invalid" for c in history)
            transport_failures = sum(c["error_code"] in retryable_codes for c in history)
            if semantic_failures:
                request["input"] = {
                    **data,
                    "format_feedback": "Previous generation failed local contract validation. Return a complete conforming JSON object.",
                }
            remaining = self.store.run(run_id)["deadline"] - time.time()
            candidates = config.route(
                role,
                task,
                len(canonical(request).encode()),
                author_family=author_family,
                max_latency_seconds=remaining,
            )
            by_id = {c.candidate_id: c for c in candidates}
            model = by_id.get(last["candidate_id"]) if last else candidates[0]
            if model is None:
                raise PolicyDenied("previous candidate is no longer eligible for this node")
            fallback_needed = False
            semantic_escalation = False
            if last:
                if last["status"] == "invalid":
                    fallback_needed = semantic_failures > task.limits.max_repairs
                    semantic_escalation = fallback_needed
                else:
                    retryable = last["error_code"] in retryable_codes
                    fallback_needed = not retryable or transport_failures > task.limits.transport_retries
                    if not fallback_needed:
                        wait_until = time.monotonic() + min(0.25 * 2 ** min(transport_failures - 1, 3), 2)
                        while time.monotonic() < wait_until:
                            self._check(run_id, owner)
                            time.sleep(0.05)
            if fallback_needed:
                used = {c["candidate_id"] for c in history}
                alternatives = [
                    c
                    for c in candidates
                    if c.candidate_id not in used
                    and (c.tier > model.tier if semantic_escalation else c.tier >= model.tier)
                ]
                if len(used) - 1 >= task.limits.max_escalations or not alternatives:
                    if semantic_escalation:
                        raise GateFailed(
                            "output contract failed after bounded repairs and qualified escalation"
                        )
                    raise ProviderFailure(last["error_code"] or "node_attempts_exhausted")
                model = alternatives[0]
                self.store.event(
                    run_id,
                    "model_escalated" if semantic_escalation else "qualified_fallback",
                    {"node_id": node_id, "candidate_id": model.candidate_id},
                )
            reserved = estimate_reservation(model, request)
            call_id = self.store.reserve_call(
                run_id, node_id, model, request, prompt_hash(prompt_variant or role), reserved, owner
            )
            try:
                result = self.gateway.generate(
                    model,
                    request,
                    min(model.timeout_seconds, remaining),
                    lambda: bool(self.store.run(run_id)["cancel_requested"]),
                )
                ensure_no_secrets(result.text)
                receipt = {
                    "text": result.text,
                    "termination": result.termination,
                    "resolved_model": result.resolved_model,
                    "pricing_uncertain": result.pricing_uncertain,
                    "reported_cost_usd": str(result.reported_cost_usd)
                    if result.reported_cost_usd is not None
                    else None,
                    "upstream_provider": result.upstream_provider,
                }
                cost = actual_cost(model, result)
                self.store.finish_call(
                    call_id,
                    status="succeeded",
                    actual=None if result.pricing_uncertain else cost,
                    response=receipt,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                    provider_request_id=result.request_id,
                    minimum_reservation=cost if result.pricing_uncertain else 0,
                )
                self._check(run_id, owner)
            except ProviderFailure as exc:
                self.store.finish_call(
                    call_id,
                    status="failed" if exc.known_zero_cost else "unknown",
                    actual=0 if exc.known_zero_cost else None,
                    error_code=exc.code,
                )
            except BaseException:
                # Unknown costs stay reserved if cancellation, secret filtering or process loss occurs.
                self.store.finish_call(call_id, status="unknown", error_code="interrupted_or_rejected")
                raise

    def _artifact(self, run_id, node_id, kind, value, parents, owner):
        existing = self.store.checkpoint(run_id, node_id)
        if existing:
            return existing[0]
        return self.store.commit_artifact(
            run_id, node_id, kind, value.model_dump(mode="json"), parents, owner
        )

    def run(self, run_id: str) -> dict:
        owner = new_id()
        if not self.store.claim(run_id, owner):
            return self.store.run(run_id)
        stop = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not stop.wait(3):
                try:
                    self.store.heartbeat(run_id, owner)
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            run = self.store.run(run_id)
            if run["code_hash"] != code_digest() or run["schema_hash"] != schemas_digest():
                raise PolicyDenied("code or schemas changed; replay/migrate before resuming this workflow")
            task = TaskSpec.model_validate_json(run["task_json"])
            config = AppConfig.model_validate_json(run["config_json"])
            self._check(run_id, owner)
            if task.task_type == "quant_research":
                from .research import run_research

                run_research(self, run_id, task, config, owner)
                return self.store.run(run_id)
            if task.limits.max_calls < 4:
                raise BudgetExceeded("evidence_report needs at least four model calls")
            if task.risk == "high" and not task.policy.require_human_release:
                raise PolicyDenied("high-risk task requires human release")
            # Preflight every model role and tool permission before any external transmission.
            validate_plan(default_plan(), task)
            for role in ("planner", "executor", "reviewer", "reporter"):
                config.route(role, task, 0)
            plan_saved = self.store.checkpoint(run_id, "plan")
            if plan_saved:
                plan_meta, plan_body = plan_saved
                plan = ExecutionPlan.model_validate(plan_body)
            else:
                plan, _ = self._llm(
                    run_id,
                    "plan",
                    "planner",
                    task,
                    config,
                    ExecutionPlan,
                    {
                        "task": task.model_dump(mode="json"),
                        "template": default_plan().model_dump(mode="json"),
                    },
                    owner,
                )
                validate_plan(plan, task)
                plan_meta = self._artifact(run_id, "plan", "ExecutionPlan", plan, [], owner)
            validate_plan(plan, task)
            prepared = self.store.checkpoint(run_id, "prepare")
            if prepared:
                from .contracts import EvidenceBundle

                evidence_meta, body = prepared
                evidence = EvidenceBundle.model_validate(body)
            else:
                evidence = prepare_evidence(task)
                evidence_meta = self._artifact(
                    run_id, "prepare", "EvidenceBundle", evidence, [plan_meta.artifact_id], owner
                )
            feedback = None
            previous_review = previous_candidate_hash = None
            review_meta = validation_meta = candidate_meta = candidate = review = None
            for attempt in range(task.limits.max_repairs + 1):
                if lost.is_set():
                    raise Conflict("worker lost heartbeat")
                self._check(run_id, owner)
                execute_id, validate_id, review_id = [
                    f"{name}_{attempt}" for name in ("execute", "validate", "review")
                ]
                saved = self.store.checkpoint(run_id, execute_id)
                if saved:
                    candidate_meta, body = saved
                    candidate = CandidateContent.model_validate(body)
                    calls = [c for c in self.store.calls(run_id, execute_id) if c["status"] == "succeeded"]
                    author_family = calls[-1]["family"]
                else:
                    candidate, author_family = self._llm(
                        run_id,
                        execute_id,
                        "executor",
                        task,
                        config,
                        CandidateContent,
                        {
                            "task": task.model_dump(mode="json"),
                            "evidence": evidence.model_dump(mode="json"),
                            "feedback": feedback,
                        },
                        owner,
                    )
                    parents = [evidence_meta.artifact_id] + ([review_meta.artifact_id] if review_meta else [])
                    candidate_meta = self._artifact(
                        run_id, execute_id, "CandidateContent", candidate, parents, owner
                    )
                validation = validate_candidate(candidate, evidence)
                validation_meta = self._artifact(
                    run_id,
                    validate_id,
                    "ValidationReport",
                    validation,
                    [candidate_meta.artifact_id, evidence_meta.artifact_id],
                    owner,
                )
                if validation.status != "passed":
                    feedback = validation.model_dump(mode="json")
                    if attempt == task.limits.max_repairs:
                        raise GateFailed("candidate failed deterministic evidence gates")
                    continue
                refs = {
                    "artifact://" + a.artifact_id for a in (candidate_meta, validation_meta, evidence_meta)
                }
                saved_review = self.store.checkpoint(run_id, review_id)
                if saved_review:
                    review_meta, body = saved_review
                    review = ReviewReport.model_validate(body)
                else:
                    review, _ = self._llm(
                        run_id,
                        review_id,
                        "reviewer",
                        task,
                        config,
                        ReviewReport,
                        {
                            "task": task.model_dump(mode="json"),
                            "candidate": candidate.model_dump(mode="json"),
                            "evidence": evidence.model_dump(mode="json"),
                            "validation": validation.model_dump(mode="json"),
                            "artifact_refs": sorted(refs),
                            "validation_artifact_ref": "artifact://" + validation_meta.artifact_id,
                            "previous_review": previous_review.model_dump(mode="json")
                            if previous_review
                            else None,
                        },
                        owner,
                        author_family,
                    )
                    validate_review(review, refs)
                    validate_review_transition(
                        previous_review,
                        review,
                        previous_candidate_hash,
                        candidate_meta.sha256,
                        validation_meta.artifact_id,
                    )
                    review_meta = self._artifact(
                        run_id,
                        review_id,
                        "ReviewReport",
                        review,
                        [candidate_meta.artifact_id, validation_meta.artifact_id, evidence_meta.artifact_id],
                        owner,
                    )
                validate_review(review, refs)
                validate_review_transition(
                    previous_review,
                    review,
                    previous_candidate_hash,
                    candidate_meta.sha256,
                    validation_meta.artifact_id,
                )
                if review.status == "approved":
                    break
                feedback = review.model_dump(mode="json")
                previous_review, previous_candidate_hash = review, candidate_meta.sha256
                if review.status == "inconclusive":
                    self.store.status(run_id, "inconclusive", "review did not establish acceptance", owner)
                    return self.store.run(run_id)
                if attempt == task.limits.max_repairs:
                    self.store.status(run_id, "rejected", "blocking review findings remain", owner)
                    return self.store.run(run_id)
            self._check(run_id, owner)
            selection_saved = self.store.checkpoint(run_id, "report")
            if selection_saved:
                selection_meta, body = selection_saved
                selection = ReportSelection.model_validate(body)
            else:
                selection, _ = self._llm(
                    run_id,
                    "report",
                    "reporter",
                    task,
                    config,
                    ReportSelection,
                    {
                        "candidate": candidate.model_dump(mode="json"),
                        "approved_review": review.model_dump(mode="json"),
                    },
                    owner,
                )
                validate_selection(selection, candidate)
                selection_meta = self._artifact(
                    run_id,
                    "report",
                    "ReportSelection",
                    selection,
                    [candidate_meta.artifact_id, review_meta.artifact_id],
                    owner,
                )
            demo = any(c["provider"] == "demo" for c in self.store.calls(run_id))
            markdown, limitations = render_report(
                task, candidate, evidence, selection, demo, config.qualification_scope == "contract_pilot"
            )
            need_human = task.policy.require_human_release or demo
            final = FinalPackage(
                task_id=task.task_id,
                run_id=run_id,
                status="pending_approval" if need_human else "accepted",
                markdown=markdown,
                evidence_refs=["artifact://" + evidence_meta.artifact_id],
                numeric_lineage=evidence.metrics,
                limitations=limitations,
                candidate_artifact_id=candidate_meta.artifact_id,
                validation_artifact_id=validation_meta.artifact_id,
                review_artifact_id=review_meta.artifact_id,
            )
            final_meta = self._artifact(
                run_id,
                "final",
                "FinalPackage",
                final,
                [
                    selection_meta.artifact_id,
                    candidate_meta.artifact_id,
                    validation_meta.artifact_id,
                    review_meta.artifact_id,
                    evidence_meta.artifact_id,
                ],
                owner,
            )
            self._check(run_id, owner)
            self.store.set_final(run_id, final_meta.artifact_id, owner)
            self.store.status(run_id, "waiting_approval" if need_human else "completed", owner=owner)
        except InterruptedError:
            self.store.status(run_id, "cancelled", "cancel requested", owner)
        except BudgetExceeded:
            self.store.status(run_id, "budget_exceeded", "cost reservation or call limit reached", owner)
        except TimeoutError:
            self.store.status(run_id, "timed_out", "run deadline reached", owner)
        except PolicyDenied as exc:
            self.store.status(run_id, "blocked", str(exc), owner)
        except (GateFailed, ValidationError):
            self.store.status(run_id, "validation_failed", "artifact or plan failed acceptance gates", owner)
        except ProviderFailure as exc:
            self.store.status(run_id, "provider_unavailable", exc.code, owner)
        except Conflict:
            if self.store.run(run_id)["owner"] == owner:
                self.store.status(run_id, "blocked", "ownership or integrity conflict", owner)
        except Exception as exc:
            self.store.status(run_id, "blocked", "internal_error:" + safe_error(exc), owner)
            raise
        finally:
            stop.set()
            thread.join(timeout=4)
            self.store.release_lease(run_id, owner)
        return self.store.run(run_id)

    def manifest(self, run_id: str) -> RunManifest:
        run = self.store.run(run_id)
        calls = self.store.calls(run_id)
        actual = sum(c["actual_microusd"] or 0 for c in calls)
        uncertain = sum(c["reserved_microusd"] for c in calls if c["actual_microusd"] is None)
        return RunManifest(
            run_id=run_id,
            status=run["status"],
            workflow_version=(
                "moex-reference/1.0"
                if json.loads(run["task_json"])["task_type"] == "quant_research"
                else WORKFLOW_VERSION
            ),
            code_version=__version__ + ":" + run["code_hash"],
            schema_digest=run["schema_hash"],
            config_sha256=run["config_hash"],
            task_sha256=run["task_hash"],
            artifacts=self.store.artifacts(run_id),
            timeline=self.store.events(run_id),
            environment=json.loads(run["environment_json"]),
            calls=calls,
            approvals=self.store.approvals(run_id),
            accounting={
                "currency": "USD",
                "known_cost_microusd": actual,
                "uncertain_reserved_microusd": uncertain,
                "accounted_cost_microusd": actual + uncertain,
                "call_count": len(calls),
                "cost_basis": "OpenRouter reported usage.cost when available; otherwise configured token tariffs; unknown costs retain reservation; reconcile invoices",
            },
            limitations=[
                "SQLite/local artifact store; single-machine deployment.",
                "No exactly-once guarantee for provider billing after ambiguous transport failures.",
                "LLM review is evidence, not proof of semantic correctness.",
            ],
        )
