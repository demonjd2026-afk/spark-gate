"""Domain models for Spark Gate.

A *trace* is one recorded run of an agent: system prompt, user turn, tool
definitions, every tool call the agent made with the result it got back, and the
final answer. Replaying a trace against another model re-runs the same
conversation while serving the *recorded* tool results instead of hitting live
systems, so comparisons are deterministic on the tool side and free of side
effects (no refund is issued twice).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_TEXT = 200_000  # hard cap on any single free-text field read from a trace file


def canonical_args(arguments: dict[str, Any]) -> str:
    """Key-order independent fingerprint of tool arguments."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- #
# Traces (input)
# --------------------------------------------------------------------------- #


class ToolDef(BaseModel):
    """OpenAI-compatible function tool definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    description: str = Field(default="", max_length=4_000)
    parameters: dict[str, Any] = Field(default_factory=dict)

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class ToolCall(BaseModel):
    """One tool invocation and (if served) the result returned to the model."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    matched: Literal["exact", "fuzzy", "none", "live"] = "live"


class Trace(BaseModel):
    """A single recorded agent run."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,128}$")
    system: str = Field(max_length=_MAX_TEXT)
    user: str = Field(max_length=_MAX_TEXT)
    tools: list[ToolDef] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    final_text: str = Field(default="", max_length=_MAX_TEXT)
    output_schema: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    recorded_model: str | None = None

    @field_validator("tool_calls")
    @classmethod
    def _cap_calls(cls, v: list[ToolCall]) -> list[ToolCall]:
        if len(v) > 64:
            raise ValueError("trace has more than 64 tool calls")
        return v


# --------------------------------------------------------------------------- #
# Replay (one trace x one model)
# --------------------------------------------------------------------------- #


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class ReplayStatus(StrEnum):
    OK = "ok"
    TOOL_LOOP_EXCEEDED = "tool_loop_exceeded"
    API_ERROR = "api_error"
    BUDGET_EXCEEDED = "budget_exceeded"


class ReplayResult(BaseModel):
    trace_id: str
    model: str
    variant: str = "original"  # "original" or "patched"
    status: ReplayStatus = ReplayStatus.OK
    final_text: str = ""
    calls: list[ToolCall] = Field(default_factory=list)
    latency_ms: float = 0.0
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None


# --------------------------------------------------------------------------- #
# Diffs (baseline replay vs candidate replay, one trace)
# --------------------------------------------------------------------------- #


class ToolFidelity(StrEnum):
    EXACT = "exact"                        # same tools, same order, same canonical args
    ARGS_CHANGED = "args_changed"          # same tool sequence, different arguments
    SEQUENCE_CHANGED = "sequence_changed"  # tools added, dropped or reordered


class QualityVerdict(StrEnum):
    CANDIDATE_BETTER = "candidate_better"
    TIE = "tie"
    BASELINE_BETTER = "baseline_better"
    NOT_JUDGED = "not_judged"


class TraceDiff(BaseModel):
    trace_id: str
    baseline_model: str
    candidate_model: str
    variant: str = "original"

    candidate_status: ReplayStatus = ReplayStatus.OK
    tool_fidelity: ToolFidelity
    baseline_tools: list[str] = Field(default_factory=list)
    candidate_tools: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    extra_tools: list[str] = Field(default_factory=list)
    arg_changes: list[str] = Field(default_factory=list)
    unrecorded_calls: int = 0  # candidate calls the recording could not serve

    schema_required: bool = False
    baseline_schema_valid: bool | None = None
    candidate_schema_valid: bool | None = None
    schema_errors: list[str] = Field(default_factory=list)

    quality: QualityVerdict = QualityVerdict.NOT_JUDGED
    quality_reason: str = ""

    baseline_latency_ms: float = 0.0
    candidate_latency_ms: float = 0.0
    baseline_cost_usd: float = 0.0
    candidate_cost_usd: float = 0.0

    regression_codes: list[str] = Field(default_factory=list)

    @property
    def is_regression(self) -> bool:
        return bool(self.regression_codes)


# --------------------------------------------------------------------------- #
# Patterns, fixes, metrics, verdicts
# --------------------------------------------------------------------------- #


class RegressionPattern(BaseModel):
    """Regressions on one candidate that share the same primary failure code."""

    id: str
    candidate_model: str
    code: str
    title: str
    description: str
    trace_ids: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    fix_rationale: str | None = None
    fix_resolved_ids: list[str] = Field(default_factory=list)
    fix_accepted: bool = False


class CandidateMetrics(BaseModel):
    model: str
    variant: str = "original"
    traces: int = 0
    errors: int = 0
    tool_fidelity_rate: float = 0.0  # share of traces without a sequence change
    tool_exact_rate: float = 0.0
    schema_valid_rate: float | None = None
    quality_win_rate: float = 0.0
    quality_loss_rate: float = 0.0
    latency_p50_ratio: float = 1.0
    latency_p95_ratio: float = 1.0
    cost_ratio: float = 1.0
    regressions: int = 0


class GateDecision(StrEnum):
    SHIP = "ship"
    SHIP_WITH_FIX = "ship_with_fix"
    DONT_SHIP = "dont_ship"


class ThresholdCheck(BaseModel):
    name: str
    direction: Literal["min", "max"]
    threshold: float
    actual: float | None
    passed: bool


class Verdict(BaseModel):
    candidate_model: str
    decision: GateDecision
    checks: list[ThresholdCheck] = Field(default_factory=list)
    patched_checks: list[ThresholdCheck] = Field(default_factory=list)
    prompt_addendum: str | None = None
    summary: str = ""


class NoiseFloor(BaseModel):
    """How much the baseline disagrees with its own recording on a fresh replay."""

    model: str
    traces: int
    tool_sequence_drift_rate: float


class RunReport(BaseModel):
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    baseline_model: str
    candidate_models: list[str]
    n_traces: int
    inconclusive_trace_ids: list[str] = Field(default_factory=list)  # baseline replay failed
    noise_floor: NoiseFloor | None = None
    metrics: list[CandidateMetrics] = Field(default_factory=list)
    diffs: list[TraceDiff] = Field(default_factory=list)
    replays: list[ReplayResult] = Field(default_factory=list)
    patterns: list[RegressionPattern] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    total_cost_usd: float = 0.0
