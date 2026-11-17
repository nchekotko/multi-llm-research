from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, DecimalException

import httpx

from .config import ModelConfig, canonical
from .security import PolicyDenied, ensure_no_secrets


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int = 0
    request_id: str | None = None
    resolved_model: str | None = None
    termination: str = "completed"
    # Cache writes may be more expensive than ordinary input tokens.
    # A conservative full reservation is kept when pricing is not representable.
    pricing_uncertain: bool = False
    reported_cost_usd: Decimal | None = None
    upstream_provider: str | None = None


class ProviderFailure(RuntimeError):
    def __init__(self, code: str, retryable: bool = False, known_zero_cost: bool = False):
        self.code, self.retryable, self.known_zero_cost = code, retryable, known_zero_cost
        super().__init__(code)


def wire_schema(value: dict) -> dict:
    """Provider-common subset only; full Pydantic validation still runs locally."""
    unsupported = {
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "title",
    }

    def convert(item):
        if isinstance(item, list):
            return [convert(v) for v in item]
        if not isinstance(item, dict):
            return item
        result = {}
        for key, value in item.items():
            if key in unsupported:
                continue
            if key in {"properties", "$defs", "definitions"}:
                result[key] = {name: convert(child) for name, child in value.items()}
            elif key == "const":
                result["enum"] = [value]
            else:
                result[key] = convert(value)
        if result.get("type") == "object":
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result

    return convert(value)


def microusd(value: Decimal) -> int:
    return int((value * 1000000).to_integral_value(rounding=ROUND_CEILING))


def estimate_reservation(model: ModelConfig, request: dict) -> int:
    # UTF-8 bytes + schema/prompt overhead is intentionally conservative, not a tokenizer.
    input_bound = len(canonical(request).encode()) + 4096
    return microusd(
        (model.input_per_million_usd * input_bound + model.output_per_million_usd * model.max_output_tokens)
        / 1000000
    )


def actual_cost(model: ModelConfig, result: ProviderResult) -> int:
    if result.reported_cost_usd is not None:
        if not result.reported_cost_usd.is_finite() or result.reported_cost_usd < 0:
            raise ValueError("invalid reported cost")
        return microusd(result.reported_cost_usd)
    return microusd(
        (
            model.input_per_million_usd * result.input_tokens
            + model.output_per_million_usd * result.output_tokens
        )
        / 1000000
    )


class Gateway:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def generate(self, model: ModelConfig, request: dict, timeout: float, cancelled) -> ProviderResult:
        ensure_no_secrets(request)
        if cancelled():
            raise InterruptedError("cancelled")
        if model.provider == "demo":
            from .demo import generate_demo

            return generate_demo(model, request)
        key = os.environ.get(model.api_key_env, "")
        if not key:
            raise ProviderFailure("missing_api_key", known_zero_cost=True)
        schema = wire_schema(request["output_schema"])
        system, user = request["system"], canonical(request["input"])
        headers = {"Content-Type": "application/json"}
        if model.provider == "openai":
            url = "https://api.openai.com/v1/responses"
            headers["Authorization"] = "Bearer " + key
            payload = {
                "model": model.model_id,
                "store": False,
                "instructions": system,
                "input": [{"role": "user", "content": user}],
                "max_output_tokens": model.max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": request["output_type"],
                        "strict": True,
                        "schema": schema,
                    }
                },
            }
            if model.reasoning_effort:
                payload["reasoning"] = {"effort": model.reasoning_effort}
        elif model.provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            payload = {
                "model": model.model_id,
                "system": system,
                "max_tokens": model.max_output_tokens,
                "messages": [{"role": "user", "content": user}],
                "output_config": {"format": {"type": "json_schema", "schema": schema}},
            }
        elif model.provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers["Authorization"] = "Bearer " + key
            routing = model.openrouter_routing
            payload = {
                "model": model.model_id,
                "stream": False,
                "max_tokens": model.max_output_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": request["output_type"], "strict": True, "schema": schema},
                },
                "provider": {
                    "only": routing.only,
                    "allow_fallbacks": False,
                    "require_parameters": True,
                    "data_collection": routing.data_collection,
                    "zdr": routing.zdr,
                    "max_price": {
                        "prompt": str(model.input_per_million_usd),
                        "completion": str(model.output_per_million_usd),
                    },
                },
                "reasoning": {"exclude": True},
            }
            if model.reasoning_effort:
                payload["reasoning"]["effort"] = model.reasoning_effort
        elif model.provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model.model_id}:generateContent"
            headers["x-goog-api-key"] = key
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "maxOutputTokens": model.max_output_tokens,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            }
        if model.temperature is not None:
            if model.provider == "gemini":
                payload["generationConfig"]["temperature"] = model.temperature
            else:
                payload["temperature"] = model.temperature
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=max(0.01, timeout), transport=self.transport, trust_env=False, follow_redirects=False
            ) as client:
                with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 300:
                        status = response.status_code
                        raise ProviderFailure(
                            f"http_{status}",
                            retryable=status in {408, 429, 500, 502, 503, 504},
                            known_zero_cost=400 <= status < 500 and status != 408,
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if cancelled():
                            raise InterruptedError("cancelled during provider response")
                        data.extend(chunk)
                        if len(data) > 2000000:
                            raise ProviderFailure("response_too_large")
                    request_id = response.headers.get("x-request-id")
            raw = json.loads(data)
            result = self._parse(model.provider, raw)
            if any(
                type(n) is not int or not 0 <= n <= 10000000
                for n in (result.input_tokens, result.output_tokens)
            ):
                raise ValueError("invalid usage")
            if model.provider == "openrouter" and result.resolved_model not in {
                model.model_id,
                model.revision,
            }:
                result.termination = "model_mismatch"
            result.request_id = request_id or result.request_id
            result.latency_ms = int((time.monotonic() - started) * 1000)
            ensure_no_secrets(
                {
                    "text": result.text,
                    "request_id": result.request_id,
                    "model": result.resolved_model,
                    "upstream_provider": result.upstream_provider,
                }
            )
            return result
        except httpx.TimeoutException as exc:
            raise ProviderFailure("transport_timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderFailure("transport_error", retryable=True) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, DecimalException) as exc:
            if isinstance(exc, PolicyDenied):
                raise
            raise ProviderFailure("invalid_provider_envelope") from exc

    @staticmethod
    def _parse(provider: str, raw: dict) -> ProviderResult:
        if provider == "openrouter":
            if raw.get("error") or not raw.get("choices") or raw["choices"][0].get("error"):
                raise ProviderFailure("openrouter_error_envelope")
            choice, usage = raw["choices"][0], raw["usage"]
            message = choice["message"]
            content = message.get("content") or ""
            if not isinstance(content, str):
                content = "".join(part["text"] for part in content if part.get("type") == "text")
            refused = bool(message.get("refusal")) or choice.get("finish_reason") == "content_filter"
            cost = usage.get("cost")
            reported = Decimal(str(cost)) if cost is not None else None
            if reported is not None and (not reported.is_finite() or reported < 0):
                raise ValueError("invalid OpenRouter cost")
            return ProviderResult(
                text=content,
                input_tokens=usage["prompt_tokens"],
                output_tokens=usage["completion_tokens"],
                request_id=raw.get("id"),
                resolved_model=raw["model"],
                upstream_provider=raw.get("provider"),
                reported_cost_usd=reported,
                pricing_uncertain=reported is None,
                termination="refused"
                if refused
                else ("completed" if choice.get("finish_reason") == "stop" else "incomplete"),
            )
        if provider == "openai":
            content = [
                c
                for item in raw.get("output", [])
                if item.get("type") == "message"
                for c in item.get("content", [])
            ]
            termination = (
                "refused"
                if any(c.get("type") == "refusal" for c in content)
                else raw.get("status", "incomplete")
            )
            return ProviderResult(
                text="".join(c["text"] for c in content if c.get("type") == "output_text"),
                input_tokens=raw["usage"]["input_tokens"],
                output_tokens=raw["usage"]["output_tokens"],
                resolved_model=raw.get("model"),
                termination=termination,
            )
        if provider == "anthropic":
            usage = raw["usage"]
            extra = usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            stop = raw.get("stop_reason")
            return ProviderResult(
                text="".join(c["text"] for c in raw["content"] if c.get("type") == "text"),
                input_tokens=usage["input_tokens"] + extra,
                output_tokens=usage["output_tokens"],
                resolved_model=raw.get("model"),
                termination="completed"
                if stop == "end_turn"
                else ("refused" if stop == "refusal" else "incomplete"),
                pricing_uncertain=bool(extra),
            )
        candidates = raw.get("candidates", [])
        if not candidates:
            return ProviderResult(
                text="",
                input_tokens=raw.get("usageMetadata", {}).get("promptTokenCount", 0),
                output_tokens=0,
                termination="refused",
            )
        c, usage = candidates[0], raw["usageMetadata"]
        return ProviderResult(
            text="".join(
                p["text"]
                for p in c.get("content", {}).get("parts", [])
                if "text" in p and not p.get("thought")
            ),
            input_tokens=usage["promptTokenCount"],
            output_tokens=usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
            resolved_model=raw.get("modelVersion"),
            termination="completed" if c.get("finishReason") == "STOP" else "incomplete",
        )
