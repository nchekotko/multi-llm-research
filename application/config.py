from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Money, ProviderName, TaskSpec
from .security import PolicyDenied, ensure_no_secrets


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Qualification(Contract):
    role: Literal["planner", "executor", "reviewer", "reporter"]
    eval_id: str = Field(min_length=1)
    passed: bool
    quality: float = Field(ge=0, le=1)
    scope: Literal["domain", "contract"] = "domain"


class OpenRouterRouting(Contract):
    only: list[str] = Field(min_length=1, max_length=10)
    data_collection: Literal["deny", "allow"] = "deny"
    zdr: bool = False

    @model_validator(mode="after")
    def provider_slugs(self) -> OpenRouterRouting:
        import re

        if any(
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,100}", slug) or ".." in slug for slug in self.only
        ):
            raise ValueError("invalid OpenRouter upstream provider slug")
        return self


class ModelConfig(Contract):
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    provider: ProviderName
    model_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*(/[a-zA-Z0-9][a-zA-Z0-9._-]*)?$")
    revision: str = Field(min_length=1)
    family: str = Field(min_length=1)
    enabled: bool = True
    api_key_env: str = ""
    input_per_million_usd: Money = Decimal("0")
    output_per_million_usd: Money = Decimal("0")
    max_output_tokens: int = Field(default=4096, ge=256, le=32768)
    max_input_bytes: int = Field(default=100000, ge=1000, le=1000000)
    timeout_seconds: int = Field(default=30, ge=1, le=180)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    qualified: list[Qualification]
    allowed_data: list[Literal["public", "internal", "confidential"]]
    governance_reviewed: bool = False
    processing_region: str = "not reviewed"
    retention: str = "not reviewed"
    training_use: str = "not reviewed"
    terms_reference: str = "not reviewed"
    expected_accepted_cost_usd: Money = Decimal("0")
    expected_latency_seconds: float = Field(default=1, gt=0)
    tier: int = Field(default=0, ge=0, le=2)
    openrouter_routing: OpenRouterRouting | None = None

    @model_validator(mode="after")
    def live_contract(self) -> ModelConfig:
        if self.reasoning_effort is not None and self.provider not in {"openai", "openrouter"}:
            raise ValueError("reasoning_effort is supported only by OpenAI and OpenRouter adapters")
        if self.provider == "openrouter":
            if (
                "/" not in self.model_id
                or self.model_id.startswith("openrouter/")
                or self.openrouter_routing is None
            ):
                raise ValueError(
                    "OpenRouter requires a specific vendor/model ID and explicit upstream allowlist"
                )
        elif "/" in self.model_id or self.openrouter_routing is not None:
            raise ValueError("namespaced model IDs and upstream routing apply only to OpenRouter")
        if self.enabled and self.provider != "demo":
            if not self.governance_reviewed or not self.api_key_env:
                raise ValueError(
                    "live candidate requires governance review and secret environment variable name"
                )
            if self.input_per_million_usd == 0 or self.output_per_million_usd == 0:
                raise ValueError("live candidate must have explicitly configured nonzero pricing")
            if (
                self.model_id.endswith("latest")
                or self.revision in {"latest", "auto", ""}
                or "REPLACE" in self.revision
            ):
                raise ValueError("moving latest aliases are forbidden")
            if any(
                v in {"", "not reviewed"}
                for v in (self.processing_region, self.retention, self.training_use, self.terms_reference)
            ):
                raise ValueError("live candidate requires explicit data governance terms")
        return self


RoleName = Literal["planner", "executor", "reviewer", "reporter"]


class RoleSettings(Contract):
    enabled: bool = True
    candidate_id: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=256, le=32768)
    timeout_seconds: int | None = Field(default=None, ge=1, le=180)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    instructions: str = Field(default="", max_length=6000)


class AppConfig(Contract):
    config_version: str = "demo-1"
    role_settings: dict[RoleName, RoleSettings] = Field(default_factory=dict)
    minimum_quality: float = Field(default=0.8, ge=0, le=1)
    models: list[ModelConfig] = Field(min_length=1)
    allow_same_family_review: bool = False
    sandbox_image: str = "multi-llm-sandbox:local"
    sandbox_enabled: bool = False
    qualification_scope: Literal["domain", "contract_pilot"] = "domain"

    @model_validator(mode="after")
    def unique(self) -> AppConfig:
        ids = [m.candidate_id for m in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate IDs")
        for role, settings in self.role_settings.items():
            ensure_no_secrets(settings.model_dump(mode="json"))
            if settings.candidate_id:
                model = next((m for m in self.models if m.candidate_id == settings.candidate_id), None)
                if model is None or not model.enabled or not any(q.role == role and q.passed and q.quality >= self.minimum_quality for q in model.qualified):
                    raise ValueError("selected model is not qualified for this role")
                changes = settings.model_dump(exclude_none=True, exclude={"enabled", "candidate_id", "instructions"})
                ModelConfig.model_validate({**model.model_dump(), **changes})
            else:
                changes = settings.model_dump(exclude_none=True, exclude={"enabled", "candidate_id", "instructions"})
                for model in self.models:
                    if model.enabled and any(q.role == role and q.passed for q in model.qualified):
                        ModelConfig.model_validate({**model.model_dump(), **changes})
        return self

    def route(
        self,
        role: str,
        task: TaskSpec,
        input_bytes: int,
        excluded: set[str] | None = None,
        author_family: str | None = None,
        min_tier: int = 0,
        max_latency_seconds: float = float("inf"),
    ) -> list[ModelConfig]:
        if self.qualification_scope == "contract_pilot" and (
            task.risk != "low"
            or task.policy.classification != "public"
            or not task.policy.require_human_release
        ):
            raise PolicyDenied("contract pilot allows only low-risk public tasks with human release")
        settings = self.role_settings.get(role, RoleSettings())
        if not settings.enabled:
            raise PolicyDenied(f"module {role} is disabled for new tasks")
        eligible = []
        for candidate in self.models:
            if (
                not candidate.enabled
                or (settings.candidate_id is not None and candidate.candidate_id != settings.candidate_id)
                or candidate.candidate_id in (excluded or set())
                or candidate.tier < min_tier
            ):
                continue
            if candidate.expected_latency_seconds > max_latency_seconds:
                continue
            if candidate.provider not in task.policy.allowed_providers:
                continue
            if (
                task.policy.classification not in candidate.allowed_data
                or input_bytes > candidate.max_input_bytes
            ):
                continue
            if candidate.provider != "demo" and (
                not task.policy.allow_external or not candidate.governance_reviewed
            ):
                continue
            if role == "reviewer" and candidate.family == author_family:
                if not self.allow_same_family_review or task.risk == "high":
                    continue
            q = next((q for q in candidate.qualified if q.role == role and q.passed), None)
            if q is None or q.quality < self.minimum_quality:
                continue
            if q.scope == "contract" and self.qualification_scope != "contract_pilot":
                continue
            changes = settings.model_dump(exclude_none=True, exclude={"enabled", "candidate_id", "instructions"})
            candidate = ModelConfig.model_validate({**candidate.model_dump(), **changes})
            eligible.append(candidate)
        eligible.sort(
            key=lambda c: (c.expected_accepted_cost_usd, c.expected_latency_seconds, c.candidate_id)
        )
        if not eligible:
            raise PolicyDenied(f"no qualified policy-compatible candidate for {role}")
        return eligible


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        from .demo import demo_config

        return demo_config()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    ensure_no_secrets(raw)
    return AppConfig.model_validate(raw)
