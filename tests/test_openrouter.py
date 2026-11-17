import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from application.config import AppConfig, ModelConfig, OpenRouterRouting
from application.demo import generate_demo
from application.engine import Engine
from application.openrouter_setup import discover_models, pilot_task, setup_openrouter
from application.providers import Gateway, ProviderFailure, actual_cost
from application.replay import replay
from application.security import PolicyDenied
from application.storage import BudgetExceeded, Conflict, Store
from application.validation import GateFailed

IDS = ["openai/gpt-6-astra", "anthropic/claude-sonnet-5.5"]


def candidate():
    return ModelConfig(
        candidate_id="or_test",
        provider="openrouter",
        model_id=IDS[0],
        revision="snapshot-1",
        family="openai",
        enabled=False,
        api_key_env="OPENROUTER_API_KEY",
        qualified=[],
        allowed_data=["public"],
        openrouter_routing=OpenRouterRouting(only=["openai"]),
    )


REQUEST = {
    "role": "executor",
    "system": "Return JSON.",
    "input": {"sample": "public"},
    "output_type": "Test",
    "output_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
}


def envelope(text='{"ok":true}', mid=IDS[0], **updates):
    result = {
        "id": "generation-123",
        "model": mid,
        "provider": "OpenAI",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": text, "reasoning": "private thinking not to retain"},
            }
        ],
        "usage": {"prompt_tokens": 23, "completion_tokens": 17, "cost": 0.0000123},
    }
    result.update(updates)
    return result


@pytest.mark.parametrize("returned_model", [IDS[0], "snapshot-1"])
def test_openrouter_wire_contract_and_exact_reported_cost(monkeypatch, returned_model):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-credential-12345678")

    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"].startswith("Bearer ")
        body = json.loads(request.content)
        assert body["model"] == IDS[0] and "models" not in body
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["provider"]["only"] == ["openai"]
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["require_parameters"] is True
        assert body["provider"]["data_collection"] == "deny"
        assert body["reasoning"]["exclude"] is True
        return httpx.Response(200, json=envelope(mid=returned_model))

    result = Gateway(httpx.MockTransport(handler)).generate(candidate(), REQUEST, 1, lambda: False)
    assert result.text == '{"ok":true}' and "thinking" not in result.text
    assert result.request_id == "generation-123" and result.upstream_provider == "OpenAI"
    assert result.reported_cost_usd == Decimal("0.0000123")
    assert result.termination == "completed"
    assert actual_cost(candidate(), result) == 13


@pytest.mark.parametrize(
    "raw",
    [
        {"error": {"code": 402, "message": "private remote message"}},
        {"choices": []},
        {"choices": [{"error": {"code": 503, "message": "secret"}}]},
    ],
)
def test_openrouter_http200_errors_are_failures(raw):
    with pytest.raises(ProviderFailure, match="openrouter_error_envelope"):
        Gateway._parse("openrouter", raw)


@pytest.mark.parametrize("bad_cost", [-1, "NaN", "Infinity"])
def test_invalid_cost_rejected(bad_cost):
    raw = envelope()
    raw["usage"]["cost"] = bad_cost
    with pytest.raises(ValueError):
        Gateway._parse("openrouter", raw)


def test_refusal_incomplete_and_missing_cost_are_distinguished():
    raw = envelope()
    raw["choices"][0]["finish_reason"] = "length"
    assert Gateway._parse("openrouter", raw).termination == "incomplete"
    raw["choices"][0]["message"]["refusal"] = "declined"
    assert Gateway._parse("openrouter", raw).termination == "refused"
    raw["usage"].pop("cost")
    assert Gateway._parse("openrouter", raw).pricing_uncertain is True


@pytest.mark.parametrize(
    "model_id", ["openrouter/auto", "openai/model:free", "openai/../model", "https://evil.example/model"]
)
def test_router_aliases_and_unsafe_model_ids_not_permitted(model_id):
    data = candidate().model_dump()
    data["model_id"] = model_id
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(data)


def test_namespaced_models_and_explicit_upstream_only_for_openrouter():
    data = candidate().model_dump()
    data["openrouter_routing"] = None
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(data)
    data["provider"] = "openai"
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(data)


def catalog_handler(request):
    if request.url.path == "/api/v1/models":
        return httpx.Response(
            200, json={"data": [{"id": mid, "canonical_slug": mid + "-snapshot"} for mid in IDS]}
        )
    if request.url.path.endswith("/endpoints"):
        mid = request.url.path.removeprefix("/api/v1/models/").removesuffix("/endpoints")
        return httpx.Response(
            200,
            json={
                "data": {
                    "endpoints": [
                        {
                            "tag": mid.split("/")[0],
                            "supported_parameters": ["response_format", "structured_outputs"],
                            "pricing": {
                                "prompt": "0.000001",
                                "completion": "0.000002",
                                "overrides": [{"prompt": "0.000003", "completion": "0.000004"}],
                            },
                        }
                    ]
                }
            },
        )
    raise AssertionError("unexpected catalog URL")


def fixture_gateway(config=None):
    def handler(request):
        body = json.loads(request.content)
        mid = body["model"]
        data = json.loads(body["messages"][1]["content"])
        role_by_type = {
            "ExecutionPlan": "planner",
            "CandidateContent": "executor",
            "ReviewReport": "reviewer",
            "ReportSelection": "reporter",
        }
        role = role_by_type[body["response_format"]["json_schema"]["name"]]
        model = candidate()
        text = generate_demo(model, {"role": role, "input": data}).text
        raw = envelope(text, mid)
        if config:
            config(raw, role)
        return httpx.Response(200, json=raw)

    return Gateway(httpx.MockTransport(handler))


def test_catalog_uses_current_endpoint_capabilities_and_tier_ceiling():
    config, snapshot = discover_models(*IDS, "openai", "anthropic", httpx.MockTransport(catalog_handler))
    assert config.qualification_scope == "contract_pilot"
    assert all(m.input_per_million_usd == Decimal("3") for m in config.models)
    assert all(m.output_per_million_usd == Decimal("4") for m in config.models)
    assert all(not m.enabled and all(not q.passed for q in m.qualified) for m in config.models)
    assert len(snapshot["models"]) == 2


def test_setup_does_not_claim_qualification_until_all_live_contracts_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-credential-12345678")
    output = tmp_path / "config/openrouter.json"
    result = setup_openrouter(
        output, gateway=fixture_gateway(), transport=httpx.MockTransport(catalog_handler)
    )
    assert result["calls"] == 4 and result["domain_quality_evaluated"] is False
    config = AppConfig.model_validate_json(output.read_text(encoding="utf-8"))
    assert all(m.enabled for m in config.models)
    assert all(q.passed and q.scope == "contract" for m in config.models for q in m.qualified)
    assert config.route("executor", pilot_task(Decimal("1")), 100)
    proof = json.loads(output.with_suffix(".qualification.json").read_text(encoding="utf-8"))
    assert proof["domain_quality_evaluated"] is False and proof["scope"] == "contract"
    assert all(c["actual_microusd"] == 13 for c in proof["calls"])
    with pytest.raises(Conflict):
        setup_openrouter(output, gateway=fixture_gateway(), transport=httpx.MockTransport(catalog_handler))


def test_failed_contract_probe_never_enables_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-credential-12345678")
    output = tmp_path / "config/openrouter.json"

    def mutate(raw, role):
        if role == "reporter":
            raw["choices"][0]["message"]["content"] = '{"claim_ids":["invented"], "metric_ids":["total"]}'

    with pytest.raises(GateFailed):
        setup_openrouter(
            output, gateway=fixture_gateway(mutate), transport=httpx.MockTransport(catalog_handler)
        )
    assert not output.exists()
    proof = json.loads(output.with_suffix(".qualification.json").read_text(encoding="utf-8"))
    assert proof["config_written"] is False and len(proof["calls"]) == 4


def test_qualification_reserves_budget_before_paid_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-credential-12345678")
    output = tmp_path / "config/openrouter.json"
    with pytest.raises(BudgetExceeded):
        setup_openrouter(
            output,
            budget=Decimal("0.000001"),
            gateway=fixture_gateway(),
            transport=httpx.MockTransport(catalog_handler),
        )
    assert not output.exists()
    proof = json.loads(output.with_suffix(".qualification.json").read_text(encoding="utf-8"))
    assert proof["calls"] == []


@pytest.mark.parametrize("mutation", ["internal", "confidential", "medium", "high", "auto_release"])
def test_contract_pilot_cannot_process_private_or_risky_tasks(mutation):
    config, _ = discover_models(*IDS, "openai", "anthropic", httpx.MockTransport(catalog_handler))
    task = pilot_task(Decimal("1"))
    if mutation in {"internal", "confidential"}:
        task.policy.classification = mutation
    elif mutation == "auto_release":
        task.policy.require_human_release = False
    else:
        task.risk = mutation
    with pytest.raises(PolicyDenied, match="contract pilot"):
        config.route("executor", task, 1)


def test_contract_score_cannot_be_promoted_to_domain_qualification():
    config, _ = discover_models(*IDS, "openai", "anthropic", httpx.MockTransport(catalog_handler))
    for model in config.models:
        model.enabled = True
        for qualification in model.qualified:
            qualification.passed, qualification.quality = True, 1
    assert config.route("executor", pilot_task(Decimal("1")), 1)
    config.qualification_scope = "domain"
    with pytest.raises(PolicyDenied):
        config.route("executor", pilot_task(Decimal("1")), 1)


def test_real_adapter_pipeline_tracks_cost_and_replays(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-credential-12345678")
    output = tmp_path / "config/openrouter.json"
    setup_openrouter(output, gateway=fixture_gateway(), transport=httpx.MockTransport(catalog_handler))
    config = AppConfig.model_validate_json(output.read_text(encoding="utf-8"))
    engine = Engine(Store(tmp_path / "runtime"), config, fixture_gateway())
    task = pilot_task(Decimal("1"))
    run_id = engine.submit(task)
    assert engine.run(run_id)["status"] == "waiting_approval"
    assert engine.manifest(run_id).accounting["known_cost_microusd"] == 52
    assert replay(engine.store, run_id)["status"] == "passed"
    final = engine.store.artifact(run_id, engine.store.run(run_id)["final_id"])[1]
    assert any("Пилот реальных моделей" in limit for limit in final["limitations"])
    assert all(c["provider"] == "openrouter" for c in engine.store.calls(run_id))


def test_mismatched_model_stops_without_repair_and_retains_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-credential-12345678")
    output = tmp_path / "config/openrouter.json"
    setup_openrouter(output, gateway=fixture_gateway(), transport=httpx.MockTransport(catalog_handler))
    config = AppConfig.model_validate_json(output.read_text(encoding="utf-8"))
    engine = Engine(
        Store(tmp_path / "runtime"),
        config,
        fixture_gateway(lambda raw, role: raw.update(model="other/unapproved-model")),
    )
    run_id = engine.submit(pilot_task(Decimal("1")))
    assert engine.run(run_id)["status"] == "blocked"
    assert len(engine.store.calls(run_id)) == 1
    assert engine.manifest(run_id).accounting["known_cost_microusd"] == 13


def test_missing_key_stops_setup_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(PolicyDenied, match="not set"):
        setup_openrouter(tmp_path / "config/openrouter.json")
