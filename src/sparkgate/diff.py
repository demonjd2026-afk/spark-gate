"""Deterministic behavioral diff between two replays of the same trace.

Everything here is pure: no model calls, no I/O. Quality judging (which does need
a model) lives in `judge.py` and is merged in by the pipeline.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from sparkgate.jsontext import extract_json
from sparkgate.policy import violations
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

# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #


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


SideEffectTools = Mapping[str, Sequence[str]]  # tool -> argument names to ignore


@dataclass(frozen=True, slots=True)
class ToolDiff:
    fidelity: ToolFidelity
    missing: list[str]
    extra: list[str]
    arg_changes: list[str]
    side_effect_changes: list[str]  # side-effecting tools whose material args changed


def _material(args: dict[str, Any], ignore: Sequence[str]) -> str:
    return canonical_args({k: v for k, v in args.items() if k not in ignore})


def tool_diff(
    baseline: list[ToolCall],
    candidate: list[ToolCall],
    side_effect_tools: SideEffectTools | None = None,
) -> ToolDiff:
    """Compare two tool-call sequences.

    Argument changes on read-only tools are reported but not treated as regressions:
    a model may reasonably phrase a search differently. On side-effecting tools
    (refunds, tickets, writes) a changed argument changes what happens in the world,
    so it is surfaced separately, ignoring free-text arguments listed in the config.
    """
    side = side_effect_tools or {}
    b_names = [c.name for c in baseline]
    c_names = [c.name for c in candidate]
    if b_names == c_names:
        changes, side_changes = [], []
        for b, c in zip(baseline, candidate, strict=True):
            if canonical_args(b.arguments) == canonical_args(c.arguments):
                continue
            changes.append(_describe_arg_change(b.name, b.arguments, c.arguments))
            if b.name in side and _material(b.arguments, side[b.name]) != _material(
                c.arguments, side[b.name]
            ):
                side_changes.append(b.name)
        fidelity = ToolFidelity.ARGS_CHANGED if changes else ToolFidelity.EXACT
        return ToolDiff(fidelity, [], [], changes, list(dict.fromkeys(side_changes)))
    b_count, c_count = Counter(b_names), Counter(c_names)
    missing = sorted((b_count - c_count).elements())
    extra = sorted((c_count - b_count).elements())
    return ToolDiff(ToolFidelity.SEQUENCE_CHANGED, missing, extra, [], [])


def sequence_drifted(recorded: list[ToolCall], replayed: list[ToolCall]) -> bool:
    return [c.name for c in recorded] != [c.name for c in replayed]


# --------------------------------------------------------------------------- #
# Full diff + regression codes
# --------------------------------------------------------------------------- #

# Highest priority first; the first code a diff carries is its primary code.
_CODE_PRIORITY = ("error:", "policy:", "schema_invalid", "args_changed:", "dropped_tool:",
                  "extra_tool:", "reordered_tools", "quality_worse")


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
    if d.candidate_policy_violations:
        # Only rules the baseline satisfied are regressions; rules the baseline also
        # breaks are counted by the absolute policy check and the baseline audit.
        already = set(d.baseline_policy_violations or [])
        codes += [f"policy:{v}" for v in d.candidate_policy_violations if v not in already]
    if d.schema_required and d.candidate_schema_valid is False and d.baseline_schema_valid:
        codes.append("schema_invalid")
    codes += [f"args_changed:{t}" for t in d.changed_side_effect_tools]
    if d.tool_fidelity is ToolFidelity.SEQUENCE_CHANGED:
        codes += [f"dropped_tool:{n}" for n in dict.fromkeys(d.missing_tools)]
        codes += [f"extra_tool:{n}" for n in dict.fromkeys(d.extra_tools)]
        if not d.missing_tools and not d.extra_tools:
            codes.append("reordered_tools")
    if d.quality is QualityVerdict.BASELINE_BETTER:
        codes.append("quality_worse")
    return codes


def diff_replays(
    trace: Trace,
    baseline: ReplayResult,
    candidate: ReplayResult,
    side_effect_tools: SideEffectTools | None = None,
) -> TraceDiff:
    td = tool_diff(baseline.calls, candidate.calls, side_effect_tools)
    d = TraceDiff(
        trace_id=trace.id,
        baseline_model=baseline.model,
        candidate_model=candidate.model,
        variant=candidate.variant,
        candidate_status=candidate.status,
        tool_fidelity=td.fidelity,
        baseline_tools=[c.name for c in baseline.calls],
        candidate_tools=[c.name for c in candidate.calls],
        missing_tools=td.missing,
        extra_tools=td.extra,
        arg_changes=td.arg_changes,
        changed_side_effect_tools=td.side_effect_changes,
        baseline_policy_violations=violations(trace.expectations, baseline),
        candidate_policy_violations=violations(trace.expectations, candidate),
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
    "SideEffectTools",
    "ToolDiff",
    "check_schema",
    "diff_replays",
    "extract_json",
    "primary_code",
    "regression_codes",
    "sequence_drifted",
    "tool_diff",
    "validate_output",
]
