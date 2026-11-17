"""One-key onboarding. Actual contract tests enable only a restricted public pilot."""

from __future__ import annotations

import json
import os
import threading
from decimal import Decimal
from pathlib import Path

import httpx

from .config import AppConfig, ModelConfig, OpenRouterRouting, Qualification, canonical, digest
from .contracts import (
    Calculation,
    CandidateContent,
    ExecutionPlan,
    ReportSelection,
    ReviewReport,
    Source,
    TaskSpec,
    default_plan,
    new_id,
    utcnow,
)
from .engine import code_digest, schemas_digest
from .providers import Gateway, ProviderFailure, actual_cost, estimate_reservation
from .roles import PROMPT_VERSION, PROMPTS, prompt_hash
from .security import PolicyDenied, ensure_no_secrets, safe_error
from .storage import BudgetExceeded, Conflict, Store
from .validation import (
    GateFailed,
    prepare_evidence,
    validate_candidate,
    validate_plan,
    validate_review,
    validate_review_transition,
    validate_selection,
)


def pilot_task(budget: Decimal) -> TaskSpec:
    return TaskSpec(
        task_id="openrouter_contract_probe",
        goal="Return the exact supplied quotation and all registered metrics. This is a synthetic API contract check.",
        sources=[
            Source(
                source_id="synthetic_source",
                text="Synthetic sample: two items cost 10 and 20 units.",
                available_at="2026-11-15T00:00:00Z",
                rights="Synthetic public test data.",
            )
        ],
        calculations=[
            Calculation(
                metric_id="total",
                operation="sum",
                values=["10", "20"],
                unit="units",
                source_ids=["synthetic_source"],
            )
        ],
        constraints=["Use only the supplied evidence. Return verbatim quotes. Do not invoke external tools."],
        acceptance_criteria=["The quote matches the supplied source and total is included."],
        policy={
            "classification": "public",
            "allow_external": True,
            "allowed_providers": ["openrouter"],
            "allowed_tools": ["evidence", "aggregate"],
            "require_human_release": True,
        },
        limits={
            "max_cost_usd": budget,
            "max_calls": 4,
            "max_seconds": 600,
            "max_repairs": 0,
            "max_escalations": 0,
            "transport_retries": 0,
        },
    )


def _get_json(client: httpx.Client, url: str) -> dict:
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise ProviderFailure(
                "openrouter_catalog_http_" + str(response.status_code), known_zero_cost=True
            )
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 10000000:
                raise ValueError("catalog response too large")
    result = json.loads(data)
    ensure_no_secrets(result)
    return result


def discover_models(
    planner_model: str, executor_model: str, planner_upstream: str, executor_upstream: str, transport=None
) -> tuple[AppConfig, dict]:
    # Validate input before constructing catalog URLs; /auto and model modifiers are forbidden.
    selections = [
        ("planner_reviewer", planner_model, planner_upstream, ["planner", "reviewer"]),
        ("executor_reporter", executor_model, executor_upstream, ["executor", "reporter"]),
    ]
    for name, mid, upstream, _ in selections:
        ModelConfig(
            candidate_id=name,
            provider="openrouter",
            model_id=mid,
            revision="pending",
            family=mid.split("/")[0],
            enabled=False,
            qualified=[],
            allowed_data=["public"],
            openrouter_routing=OpenRouterRouting(only=[upstream]),
        )
    if planner_model.split("/")[0] == executor_model.split("/")[0]:
        raise PolicyDenied("pilot needs planner/reviewer and executor from different model families")
    with httpx.Client(timeout=20, transport=transport, trust_env=False, follow_redirects=False) as client:
        catalog = _get_json(client, "https://openrouter.ai/api/v1/models")
        by_id = {item["id"]: item for item in catalog["data"]}
        models, snapshots = [], []
        for name, mid, upstream, roles in selections:
            item = by_id.get(mid)
            if item is None:
                raise ValueError("requested model is not in the current OpenRouter catalog")
            endpoint_data = _get_json(client, "https://openrouter.ai/api/v1/models/" + mid + "/endpoints")[
                "data"
            ]
            endpoint = next((e for e in endpoint_data["endpoints"] if e.get("tag") == upstream), None)
            if endpoint is None or not {"structured_outputs", "response_format"} <= set(
                endpoint.get("supported_parameters", [])
            ):
                raise PolicyDenied("selected endpoint does not advertise structured output support")
            pricing = endpoint["pricing"]
            # Max across published tiers keeps reservations conservative even if pricing has overrides.
            tiers = [pricing, *pricing.get("overrides", [])]
            in_rate = max(Decimal(t["prompt"]) for t in tiers) * 1000000
            out_rate = max(Decimal(t["completion"]) for t in tiers) * 1000000
            if not in_rate.is_finite() or not out_rate.is_finite() or min(in_rate, out_rate) <= 0:
                raise PolicyDenied("pilot requires finite, positive, published token prices")
            snapshot = {
                "id": mid,
                "canonical_slug": item.get("canonical_slug", mid),
                "endpoint": endpoint,
                "retrieved_at": utcnow(),
            }
            snapshots.append(snapshot)
            models.append(
                ModelConfig(
                    candidate_id="or_" + name,
                    provider="openrouter",
                    model_id=mid,
                    revision=snapshot["canonical_slug"],
                    family=mid.split("/")[0],
                    enabled=False,
                    api_key_env="OPENROUTER_API_KEY",
                    input_per_million_usd=in_rate,
                    output_per_million_usd=out_rate,
                    max_output_tokens=4096,
                    timeout_seconds=120,
                    max_input_bytes=60000,
                    reasoning_effort="low",
                    qualified=[
                        Qualification(
                            role=r,
                            eval_id="pending-live-contract-probe",
                            passed=False,
                            quality=0,
                            scope="contract",
                        )
                        for r in roles
                    ],
                    allowed_data=["public"],
                    governance_reviewed=True,
                    processing_region="Not asserted; public-data pilot only",
                    retention="OpenRouter and selected upstream terms apply; not approved for private data",
                    training_use="data_collection=deny requested; public-data pilot only",
                    terms_reference="https://openrouter.ai/privacy; https://openrouter.ai/docs/guides/routing/provider-selection",
                    openrouter_routing=OpenRouterRouting(only=[upstream]),
                )
            )
    return AppConfig(
        config_version="openrouter-contract-pilot-1", models=models, qualification_scope="contract_pilot"
    ), {"models": snapshots}


def setup_openrouter(
    output: Path,
    *,
    budget: Decimal = Decimal("2"),
    planner_model="openai/gpt-6-astra",
    executor_model="anthropic/claude-sonnet-5.5",
    planner_upstream="openai",
    executor_upstream="anthropic",
    gateway: Gateway | None = None,
    transport=None,
) -> dict:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise PolicyDenied("OPENROUTER_API_KEY is not set; enter it locally before setup")
    if not budget.is_finite() or not 0 < budget <= 100:
        raise ValueError("qualification budget must be between 0 and 100 USD")
    if output.exists():
        raise Conflict(
            "output config already exists; choose a new output path to preserve prior qualification"
        )
    config, snapshot = discover_models(
        planner_model, executor_model, planner_upstream, executor_upstream, transport
    )
    task = pilot_task(budget)
    gateway = gateway or Gateway()
    # Dedicated durable ledger: this is a contract test, never an accepted business result.
    store = Store(output.resolve().parent.parent / "var" / "openrouter-qualification")
    run_id = store.create_run(task, config.model_dump(mode="json"), code_digest(), schemas_digest())
    owner = new_id()
    store.claim(run_id, owner)
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(3):
            try:
                store.heartbeat(run_id, owner)
            except Exception:
                return

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    store.event(
        run_id, "contract_probe_started", {"kind": "synthetic_live_contract_only", "catalog": snapshot}
    )
    proof, qualified_roles = [], {}

    def probe(role, output_type, data):
        model = next(m for m in config.models if any(q.role == role for q in m.qualified))
        request = {
            "role": role,
            "prompt_version": PROMPT_VERSION,
            "system": PROMPTS[role],
            "output_type": output_type.__name__,
            "output_schema": output_type.model_json_schema(),
            "input": data,
        }
        if len(canonical(request).encode()) > model.max_input_bytes:
            raise PolicyDenied("contract probe exceeds configured input bound")
        store.heartbeat(run_id, owner)
        call_id = store.reserve_call(
            run_id, role, model, request, prompt_hash(role), estimate_reservation(model, request), owner
        )
        try:
            result = gateway.generate(model, request, model.timeout_seconds, lambda: False)
            cost = actual_cost(model, result)
            receipt = {
                "text": result.text,
                "termination": result.termination,
                "resolved_model": result.resolved_model,
                "upstream_provider": result.upstream_provider,
                "reported_cost_usd": str(result.reported_cost_usd)
                if result.reported_cost_usd is not None
                else None,
            }
            store.finish_call(
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
            calls = store.calls(run_id)
            accounted = sum(
                c["actual_microusd"] if c["actual_microusd"] is not None else c["reserved_microusd"]
                for c in calls
            )
            if accounted > int(budget * 1000000):
                raise BudgetExceeded("contract probe cost exceeded budget")
            if result.termination != "completed":
                raise GateFailed("contract probe refused, incomplete or model mismatch")
            parsed = output_type.model_validate_json(result.text)
            qualified_roles[role] = (model.candidate_id, cost, result.latency_ms)
            return parsed
        except BaseException:
            store.finish_call(call_id, status="unknown", error_code="contract_probe_interrupted")
            raise

    try:
        evidence = prepare_evidence(task)
        plan = probe(
            "planner",
            ExecutionPlan,
            {"task": task.model_dump(mode="json"), "template": default_plan().model_dump(mode="json")},
        )
        validate_plan(plan, task)
        plan_meta = store.commit_artifact(
            run_id, "plan", "ExecutionPlan", plan.model_dump(mode="json"), [], owner
        )
        evidence_meta = store.commit_artifact(
            run_id,
            "prepare",
            "EvidenceBundle",
            evidence.model_dump(mode="json"),
            [plan_meta.artifact_id],
            owner,
        )
        candidate = probe(
            "executor",
            CandidateContent,
            {
                "task": task.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "feedback": None,
            },
        )
        validation = validate_candidate(candidate, evidence)
        if validation.status != "passed":
            raise GateFailed("contract probe candidate failed evidence gates")
        candidate_meta = store.commit_artifact(
            run_id,
            "execute",
            "CandidateContent",
            candidate.model_dump(mode="json"),
            [evidence_meta.artifact_id],
            owner,
        )
        validation_meta = store.commit_artifact(
            run_id,
            "validate",
            "ValidationReport",
            validation.model_dump(mode="json"),
            [candidate_meta.artifact_id],
            owner,
        )
        refs = {"artifact://" + a.artifact_id for a in (candidate_meta, validation_meta, evidence_meta)}
        review = probe(
            "reviewer",
            ReviewReport,
            {
                "task": task.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "artifact_refs": sorted(refs),
                "validation_artifact_ref": "artifact://" + validation_meta.artifact_id,
                "previous_review": None,
            },
        )
        validate_review(review, refs)
        validate_review_transition(None, review, None, candidate_meta.sha256, validation_meta.artifact_id)
        if review.status != "approved":
            raise GateFailed("contract probe reviewer did not approve the control sample")
        review_meta = store.commit_artifact(
            run_id,
            "review",
            "ReviewReport",
            review.model_dump(mode="json"),
            [candidate_meta.artifact_id, validation_meta.artifact_id, evidence_meta.artifact_id],
            owner,
        )
        selection = probe(
            "reporter",
            ReportSelection,
            {
                "candidate": candidate.model_dump(mode="json"),
                "approved_review": review.model_dump(mode="json"),
            },
        )
        validate_selection(selection, candidate)
        store.commit_artifact(
            run_id,
            "report",
            "ReportSelection",
            selection.model_dump(mode="json"),
            [candidate_meta.artifact_id, review_meta.artifact_id],
            owner,
        )
        for model in config.models:
            model.enabled = True
            for q in model.qualified:
                q.eval_id, q.passed, q.quality = "openrouter-contract-" + run_id, True, 1
            measurements = [v for v in qualified_roles.values() if v[0] == model.candidate_id]
            model.expected_accepted_cost_usd = (
                Decimal(sum(v[1] for v in measurements)) / len(measurements) / 1000000
            )
            model.expected_latency_seconds = max(
                1, sum(v[2] for v in measurements) / len(measurements) / 1000
            )
        store.status(run_id, "completed", "contract probe passed; domain quality not evaluated", owner)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents accidental replacement of a previously approved config.
        with output.open("x", encoding="utf-8") as stream:
            stream.write(config.model_dump_json(indent=2))
        proof = {
            "status": "passed",
            "run_id": run_id,
            "scope": "contract",
            "domain_quality_evaluated": False,
            "config_sha256": digest(config.model_dump(mode="json")),
            "catalog": snapshot,
            "calls": store.calls(run_id),
            "artifacts": [a.model_dump(mode="json") for a in store.artifacts(run_id)],
        }
    except BaseException as exc:
        status = "budget_exceeded" if isinstance(exc, BudgetExceeded) else "validation_failed"
        store.status(run_id, status, "contract_probe:" + safe_error(exc), owner)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".qualification.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "run_id": run_id,
                    "config_written": False,
                    "error_type": safe_error(exc),
                    "calls": store.calls(run_id),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    finally:
        stop.set()
        heartbeat_thread.join(timeout=4)
        store.release_lease(run_id, owner)
    proof_path = output.with_suffix(".qualification.json")
    proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "passed",
        "scope": "contract_pilot",
        "config": str(output.resolve()),
        "proof": str(proof_path.resolve()),
        "calls": len(proof["calls"]),
        "domain_quality_evaluated": False,
    }
