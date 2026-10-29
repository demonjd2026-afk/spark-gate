"""Configuration: `sparkgate.yaml` (what to compare and how strictly) + environment (secrets).

Secrets never live in the YAML file. Each endpoint names the environment variable
holding its key, and keys are read once into `SecretStr` so they never end up in
logs, reports or reprs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    """Raised for invalid or incomplete configuration."""


class EndpointConfig(BaseModel):
    """An OpenAI-compatible endpoint (Meta Model API, or a host serving open weights)."""

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str

    @model_validator(mode="after")
    def _one_url_source(self) -> EndpointConfig:
        if not (self.base_url or self.base_url_env):
            raise ValueError("endpoint needs base_url or base_url_env")
        return self

    def resolve_url(self) -> str:
        url = self.base_url or os.environ.get(self.base_url_env or "", "")
        if not url:
            raise ConfigError(f"environment variable {self.base_url_env} is not set")
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.netloc:
            return url.rstrip("/")
        if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
            return url.rstrip("/")
        raise ConfigError(f"endpoint URL must be https (http allowed for localhost only): {url}")

    def unavailable_reason(self) -> str | None:
        """Why this endpoint can't be used right now (missing env vars), or None."""
        missing = [v for v in (self.base_url_env if not self.base_url else None, self.api_key_env)
                   if v and not os.environ.get(v, "").strip()]
        return f"not set: {', '.join(missing)}" if missing else None

    def resolve_key(self) -> SecretStr:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ConfigError(f"environment variable {self.api_key_env} is not set")
        return SecretStr(key)


class ModelConfig(BaseModel):
    """A model as Spark Gate sees it: where it is served and what it costs."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    wire_model: str | None = None  # id sent on the wire, if different from the config key
    hosting: Literal["meta_api", "open_weights"] = "meta_api"
    served_by: str | None = None  # display name of the host, e.g. "Baseten"
    input_per_mtok: float | None = Field(default=None, ge=0)
    output_per_mtok: float | None = Field(default=None, ge=0)
    cached_input_per_mtok: float | None = Field(default=None, ge=0)

    @property
    def priced(self) -> bool:
        return self.input_per_mtok is not None and self.output_per_mtok is not None


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    model: str


class FixerConfig(RoleConfig):
    max_patterns: int = Field(default=5, ge=0, le=20)
    examples_per_pattern: int = Field(default=3, ge=1, le=8)


class GateThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_tool_fidelity: float = Field(default=0.90, ge=0, le=1)
    min_schema_valid: float = Field(default=0.95, ge=0, le=1)
    # Share of traces whose expectations (absolute policy rules) all hold.
    min_policy_pass: float = Field(default=1.0, ge=0, le=1)
    max_quality_loss: float = Field(default=0.10, ge=0, le=1)
    max_latency_p95_ratio: float | None = Field(default=1.5, gt=0)
    max_cost_ratio: float | None = Field(default=1.5, gt=0)
    max_errors: int = Field(default=0, ge=0)
    # Tools that change the world. A changed argument on these is a regression;
    # the listed argument names (free text such as a reason) are ignored.
    side_effect_tools: dict[str, list[str]] = Field(default_factory=dict)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=6, ge=1, le=64)
    max_tool_turns: int = Field(default=8, ge=1, le=32)
    temperature: float | None = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=2048, ge=64, le=32_768)
    request_timeout_s: float = Field(default=120, gt=0, le=900)
    max_retries: int = Field(default=5, ge=0, le=10)
    budget_usd: float = Field(default=20.0, gt=0)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    # Name of the output-length parameter the endpoint accepts.
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline: str
    candidates: list[str] = Field(min_length=1)
    traces: Path
    endpoints: dict[str, EndpointConfig]
    models: dict[str, ModelConfig]
    judge: RoleConfig
    fixer: FixerConfig
    gate: GateThresholds = Field(default_factory=GateThresholds)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    store_dir: Path = Path(".sparkgate")

    @field_validator("candidates")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("candidates must be unique")
        return v

    @model_validator(mode="after")
    def _references_resolve(self) -> GateConfig:
        needed = {self.baseline, *self.candidates}
        if self.judge.enabled:
            needed.add(self.judge.model)
        if self.fixer.enabled:
            needed.add(self.fixer.model)
        missing = sorted(m for m in needed if m not in self.models)
        if missing:
            raise ValueError(f"models referenced but not defined under `models:`: {missing}")
        bad = sorted(
            f"{name} -> {m.endpoint}"
            for name, m in self.models.items()
            if m.endpoint not in self.endpoints
        )
        if bad:
            raise ValueError(f"models point at undefined endpoints: {bad}")
        if self.baseline in self.candidates:
            raise ValueError("baseline must not also be listed as a candidate")
        return self

    def with_candidates(self, candidates: list[str]) -> GateConfig:
        return self.model_validate({**self.model_dump(), "candidates": candidates})

    def unavailable(self, model: str) -> str | None:
        return self.endpoints[self.models[model].endpoint].unavailable_reason()

    def split_available(self) -> tuple[GateConfig, dict[str, str]]:
        """Drop candidates whose endpoint has no credentials yet.

        Returns the trimmed config and {model: reason} for every candidate skipped.
        The baseline, judge and fixer are never dropped silently; `ConfigError` instead.
        """
        required = {self.baseline}
        if self.judge.enabled:
            required.add(self.judge.model)
        if self.fixer.enabled:
            required.add(self.fixer.model)
        for m in sorted(required):
            if reason := self.unavailable(m):
                raise ConfigError(f"{m} is required but its endpoint is unusable ({reason})")
        skipped = {m: r for m in self.candidates if (r := self.unavailable(m))}
        keep = [m for m in self.candidates if m not in skipped]
        if not keep:
            raise ConfigError(f"no candidate has credentials configured: {skipped}")
        return (self.with_candidates(keep) if skipped else self), skipped


def load_config(path: str | Path) -> GateConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    cfg = GateConfig.model_validate(raw)
    # Resolve relative paths against the config file's directory, not the CWD.
    base = path.parent.resolve()
    updates: dict[str, Path] = {}
    if not cfg.traces.is_absolute():
        updates["traces"] = base / cfg.traces
    if not cfg.store_dir.is_absolute():
        updates["store_dir"] = base / cfg.store_dir
    return cfg.model_copy(update=updates)
