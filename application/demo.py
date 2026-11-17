import json

from .config import AppConfig, ModelConfig, Qualification
from .contracts import default_plan
from .providers import ProviderResult


def demo_config(mode: str = "specialized") -> AppConfig:
    if mode not in {"single", "roles", "specialized"}:
        raise ValueError("unknown demo configuration")
    roles = ["planner", "executor", "reviewer", "reporter"]
    if mode == "specialized":
        models = [
            ModelConfig(
                candidate_id="demo_" + role,
                provider="demo",
                model_id="deterministic-fixture-v1",
                revision="fixture-v1",
                family="fixture_review" if role == "reviewer" else "fixture_author",
                qualified=[Qualification(role=role, eval_id="synthetic-contract-v1", passed=True, quality=1)],
                allowed_data=["public", "internal", "confidential"],
            )
            for role in roles
        ]
    else:
        models = [
            ModelConfig(
                candidate_id="demo_single",
                provider="demo",
                model_id="deterministic-fixture-v1",
                revision="fixture-v1",
                family="fixture_same",
                qualified=[
                    Qualification(role=r, eval_id="synthetic-contract-v1", passed=True, quality=1)
                    for r in roles
                ],
                allowed_data=["public", "internal", "confidential"],
            )
        ]
    return AppConfig(
        models=models, allow_same_family_review=mode != "specialized", config_version="demo-1-" + mode
    )


def generate_demo(model: ModelConfig, request: dict) -> ProviderResult:
    role, data = request["role"], request["input"]
    if request.get("output_type") == "ResearchAssessment":
        output = {
            "summary": "Офлайн-синтетический ответ для проверки контракта. Финансовая аналитика модели не выполнялась.",
            "concerns": ["Исполнимость часового сценария не подтверждена."],
            "next_experiments": [],
        }
    elif role == "reviewer" and data.get("validation", {}).get("execution_certified") is False:
        output = {
            "status": "inconclusive",
            "findings": [],
            "unresolved_questions": ["Офлайн-фикстура не оценивает качество финансовых исследований."],
        }
    elif role == "planner":
        output = default_plan().model_dump(mode="json")
    elif role == "executor":
        output = {
            "schema_version": "1.0",
            "claims": [
                {
                    "schema_version": "1.0",
                    "claim_id": "claim_" + str(i),
                    "source_id": source["source_id"],
                    "quote": source["text"][:400],
                }
                for i, source in enumerate(data["evidence"]["sources"])
            ],
            "metric_ids": [m["metric_id"] for m in data["evidence"]["metrics"]],
            "limitation_codes": [
                "source_truth_unverified",
                "coverage_limited",
                "offline_demo",
                "human_review_required",
            ],
        }
    elif role == "reviewer":
        output = {
            "schema_version": "1.0",
            "status": "approved",
            "findings": [],
            "unresolved_questions": [],
            "resolutions": [],
        }
    else:
        output = {
            "schema_version": "1.0",
            "claim_ids": [c["claim_id"] for c in data["candidate"]["claims"]],
            "metric_ids": data["candidate"]["metric_ids"],
        }
    return ProviderResult(
        text=json.dumps(output, ensure_ascii=False),
        input_tokens=0,
        output_tokens=0,
        resolved_model=model.model_id,
    )
