import json
import os

import httpx
import pytest

from application.config import ModelConfig, Qualification
from application.providers import Gateway, ProviderFailure
from application.sandbox import DockerSandbox, SandboxUnavailable
from application.security import PolicyDenied


def model(provider):
    return ModelConfig(
        candidate_id="test_" + provider,
        provider=provider,
        model_id="test-pinned-v1",
        revision="v1",
        family=provider,
        api_key_env="TEST_PROVIDER_API_KEY",
        input_per_million_usd="1",
        output_per_million_usd="2",
        governance_reviewed=True,
        allowed_data=["public"],
        processing_region="test",
        retention="test",
        training_use="test",
        terms_reference="test-fixture",
        qualified=[Qualification(role="executor", eval_id="test", passed=True, quality=1)],
    )


REQUEST = {
    "role": "executor",
    "system": "Return JSON.",
    "input": {"text": "hello"},
    "output_type": "Test",
    "output_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_real_adapter_wire_contract_with_mock_http(provider, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "dummy-test-credential-12345678")

    def handler(request):
        assert "dummy-test-credential" not in str(request.url)
        payload = json.loads(request.content)
        if provider == "openai":
            assert payload["store"] is False
            assert payload["text"]["format"]["strict"] is True
            raw = {
                "model": "test-pinned-v1",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }
        elif provider == "anthropic":
            assert payload["output_config"]["format"]["type"] == "json_schema"
            raw = {
                "model": "test-pinned-v1",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }
        else:
            assert payload["generationConfig"]["responseMimeType"] == "application/json"
            raw = {
                "candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
            }
        return httpx.Response(200, json=raw, headers={"x-request-id": "test-request"})

    result = Gateway(httpx.MockTransport(handler)).generate(model(provider), REQUEST, 1, lambda: False)
    assert json.loads(result.text) == {"ok": True}
    assert result.input_tokens == 10 and result.output_tokens == 4
    assert result.request_id == "test-request"


def test_refusal_and_incomplete_responses_not_treated_as_success():
    raw = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    assert Gateway._parse("openai", raw).termination == "refused"
    raw["status"] = "incomplete"
    raw["output"] = []
    assert Gateway._parse("openai", raw).termination == "incomplete"


def test_remote_error_body_never_leaks_into_error(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "dummy-test-credential-12345678")
    gateway = Gateway(httpx.MockTransport(lambda request: httpx.Response(429, text="secret remote body")))
    with pytest.raises(ProviderFailure) as error:
        gateway.generate(model("openai"), REQUEST, 1, lambda: False)
    assert error.value.code == "http_429"
    assert "secret" not in str(error.value)


def test_sandbox_fails_closed_without_permission_or_runtime(monkeypatch):
    with pytest.raises(PolicyDenied):
        DockerSandbox("unused").execute("print(1)", None, allowed=True)
    with pytest.raises(PolicyDenied):
        DockerSandbox("unused", True).execute("print(1)", None, allowed=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SandboxUnavailable):
        DockerSandbox("unused", True).execute("print(1)", None, allowed=True)


@pytest.mark.sandbox
@pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_TESTS") != "1",
    reason="requires prebuilt Linux Docker sandbox; explicitly enable RUN_SANDBOX_TESTS=1",
)
def test_live_sandbox_isolates_host_secrets_network_and_resources():
    sandbox = DockerSandbox("multi-llm-sandbox:local", True)
    result = sandbox.execute(
        """import os, socket
assert not os.path.exists('/host')
assert not os.path.exists('/var/run/docker.sock')
assert 'TEST_PROVIDER_API_KEY' not in os.environ
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
    raise AssertionError('network unexpectedly accessible')
except OSError:
    pass
print(task_input['value'] + 1)
""",
        {"value": 41},
        allowed=True,
    )
    assert result.status == "completed" and result.stdout.strip() == "42"
    timeout = sandbox.execute("while True: pass", None, allowed=True, timeout=1)
    assert timeout.status == "timed_out"
