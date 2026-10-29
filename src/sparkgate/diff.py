"""Deterministic behavioral diff between two replays of the same trace.

Everything here is pure: no model calls, no I/O. Quality judging (which does need
a model) lives in `judge.py` and is merged in by the pipeline.
"""

from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from sparkgate.schemas import (
    QualityVerdict,
    ReplayResult,
    ReplayStatus,
    ToolCall,
    ToolFidelity,
    Trace,
    TraceDiff,
    canonical_args,
)

_DECODER = json.JSONDecoder()


# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #


def extract_json(text: str) -> Any | None:
    """The JSON value a model meant to return, tolerating fences and surrounding prose.

    Tries, in order: the whole string; the first ```json fenced block; the first
    position where a JSON object or array decodes cleanly.
    """
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if "```" in s:
        for block in s.split("```")[1::2]:
            body = block.split("\n", 1)[1] if block.lower().startswith("json") else block
            try:
                return json.loads(body.strip())
            except (json.JSONDecodeError, IndexError):
                continue
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                value, _ = _DECODER.raw_decode(s, i)
                return value
            except json.JSONDecodeError:
                continue
    return None


@lru_cache(maxsize=64)
def _validator(schema_json: str) -> Draft202012Validator:
    schema = json.loads(schema_json)
    Draft202012Validator.check_schema(schema)
    # An explicit empty registry disables jsonschema's legacy fallback that fetches
    # remote `$ref` URLs, so an untrusted schema can never trigger network access.
    return Draft202012Validator(schema, registry=Registry())


def check_schema(schema: dict[str, Any]) -> None:
    """Raise `SchemaError` if the output schema itself is invalid."""
    _validator(json.dumps(schema, sort_keys=True))


def validate_output(schema: dict[str, Any], text: str) -> tuple[bool, list[str]]:
    value = extract_json(text)
    if value is None:
        return False, ["no JSON value found in the final answer"]
    validator = _validator(json.dumps(schema, sort_keys=True))
    try:
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    except Unresolvable as exc:
        return False, [f"schema reference could not be resolved: {exc}"[:200]]
    messages = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}" for e in errors[:5]
    ]
    return not errors, messages


# --------------------------------------------------------------------------- #
# Tool behavior
# --------------------------------------------------------------------------- #


def _describe_arg_change(name: str, a: dict[str, Any], b: dict[str, Any]) -> str:
    keys = sorted(set(a) | set(b))
    changed = [
        f"{k}: {json.dumps(a.get(k), default=str)[:60]} -> {json.dumps(b.get(k), default=str)[:60]}"
        for k in keys
        if canonical_args({"v": a.get(k)}) != canonical_args({"v": b.get(k)})
    ]
    return f"{name}({'; '.join(changed[:4])})"


def tool_diff(
    baseline: list[ToolCall], candidate: list[ToolCall]
) -> tuple[ToolFidelity, list[str], list[str], list[str]]:
    """Returns (fidelity, missing_tools, extra_tools, arg_change_descriptions)."""
    b_names = [c.name for c in baseline]
    c_names = [c.name for c in candidate]
    if b_names == c_names:
        changes = [
            _describe_arg_change(b.name, b.arguments, c.arguments)
            for b, c in zip(baseline, candidate, strict=True)
            if canonical_args(b.arguments) != canonical_args(c.arguments)
        ]
        return (ToolFidelity.ARGS_CHANGED if changes else ToolFidelity.EXACT), [], [], changes
    b_count, c_count = Counter(b_names), Counter(c_names)
    missing = sorted((b_count - c_count).elements())
    extra = sorted((c_count - b_count).elements())
    return ToolFidelity.SEQUENCE_CHANGED, missing, extra, []


def sequence_drifted(recorded: list[ToolCall], replayed: list[ToolCall]) -> bool:
    return [c.name for c in recorded] != [c.name for c in replayed]


# --------------------------------------------------------------------------- #
# Full diff + regression codes
# --------------------------------------------------------------------------- #

# Highest priority first; the first code a diff carries is its primary code.
_CODE_PRIORITY = ("error:", "schema_invalid", "dropped_tool:", "extra_tool:", "reordered_tools",
                  "quality_worse")


def primary_code(codes: list[str]) -> str | None:
    for prefix in _CODE_PRIORITY:
        for code in codes:
            if code.startswith(prefix):
                return code
    return codes[0] if codes else None


def regression_codes(d: TraceDiff) -> list[str]:
    """Why a candidate is worse than the baseline on this trace (empty = not worse)."""
    if d.candidate_status is not ReplayStatus.OK:
        return [f"error:{d.candidate_status.value}"]
    codes: list[str] = []
    if d.schema_required and d.candidate_schema_valid is False and d.baseline_schema_valid:
        codes.append("schema_invalid")
    if d.tool_fidelity is ToolFidelity.SEQUENCE_CHANGED:
        codes += [f"dropped_tool:{n}" for n in dict.fromkeys(d.missing_tools)]
        codes += [f"extra_tool:{n}" for n in dict.fromkeys(d.extra_tools)]
        if not d.missing_tools and not d.extra_tools:
            codes.append("reordered_tools")
    if d.quality is QualityVerdict.BASELINE_BETTER:
        codes.append("quality_worse")
    return codes


def diff_replays(trace: Trace, baseline: ReplayResult, candidate: ReplayResult) -> TraceDiff:
    fidelity, missing, extra, arg_changes = tool_diff(baseline.calls, candidate.calls)
    d = TraceDiff(
        trace_id=trace.id,
        baseline_model=baseline.model,
        candidate_model=candidate.model,
        variant=candidate.variant,
        candidate_status=candidate.status,
        tool_fidelity=fidelity,
        baseline_tools=[c.name for c in baseline.calls],
        candidate_tools=[c.name for c in candidate.calls],
        missing_tools=missing,
        extra_tools=extra,
        arg_changes=arg_changes,
        unrecorded_calls=sum(1 for c in candidate.calls if c.matched == "none"),
        baseline_latency_ms=baseline.latency_ms,
        candidate_latency_ms=candidate.latency_ms,
        baseline_cost_usd=baseline.usage.cost_usd,
        candidate_cost_usd=candidate.usage.cost_usd,
    )
    if trace.output_schema is not None:
        d.schema_required = True
        b_ok, _ = validate_output(trace.output_schema, baseline.final_text)
        if candidate.status is ReplayStatus.OK:
            c_ok, errs = validate_output(trace.output_schema, candidate.final_text)
        else:
            c_ok, errs = False, [candidate.error or candidate.status.value]
        d.baseline_schema_valid, d.candidate_schema_valid, d.schema_errors = b_ok, c_ok, errs
    d.regression_codes = regression_codes(d)
    return d


__all__ = [
    "SchemaError",
    "check_schema",
    "diff_replays",
    "extract_json",
    "primary_code",
    "regression_codes",
    "sequence_drifted",
    "tool_diff",
    "validate_output",
]
