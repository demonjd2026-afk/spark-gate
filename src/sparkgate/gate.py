"""Aggregate per-trace diffs into metrics, and metrics into a ship decision."""

from __future__ import annotations

import math
from collections.abc import Sequence

from sparkgate.config import GateThresholds
from sparkgate.schemas import (
    CandidateMetrics,
    GateDecision,
    QualityVerdict,
    ReplayResult,
    ReplayStatus,
    ThresholdCheck,
    ToolFidelity,
    TraceDiff,
    Verdict,
)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (same definition as numpy's default)."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def compute_metrics(
    model: str,
    variant: str,
    diffs: list[TraceDiff],
    baseline_replays: list[ReplayResult],
    candidate_replays: list[ReplayResult],
    *,
    priced: bool,
) -> CandidateMetrics:
    n = len(diffs)
    if n == 0:
        return CandidateMetrics(model=model, variant=variant)
    ok = [d for d in diffs if d.candidate_status is ReplayStatus.OK]
    schema_diffs = [d for d in diffs if d.schema_required]

    b_lat = [r.latency_ms for r in baseline_replays if r.status is ReplayStatus.OK]
    c_lat = [r.latency_ms for r in candidate_replays if r.status is ReplayStatus.OK]
    b_cost = sum(r.usage.cost_usd for r in baseline_replays)
    c_cost = sum(r.usage.cost_usd for r in candidate_replays)

    m = CandidateMetrics(
        model=model,
        variant=variant,
        traces=n,
        errors=n - len(ok),
        tool_fidelity_rate=sum(d.tool_fidelity is not ToolFidelity.SEQUENCE_CHANGED for d in ok) / n,
        tool_exact_rate=sum(d.tool_fidelity is ToolFidelity.EXACT for d in ok) / n,
        schema_valid_rate=(
            sum(bool(d.candidate_schema_valid) for d in schema_diffs) / len(schema_diffs)
            if schema_diffs
            else None
        ),
        quality_win_rate=sum(d.quality is QualityVerdict.CANDIDATE_BETTER for d in diffs) / n,
        quality_loss_rate=sum(d.quality is QualityVerdict.BASELINE_BETTER for d in diffs) / n,
        regressions=sum(d.is_regression for d in diffs),
    )
    m.latency_p50_ratio = _ratio(percentile(c_lat, 0.5), percentile(b_lat, 0.5)) or 1.0
    m.latency_p95_ratio = _ratio(percentile(c_lat, 0.95), percentile(b_lat, 0.95)) or 1.0
    m.cost_ratio = (_ratio(c_cost, b_cost) if priced else None) or 1.0
    return m


def evaluate(
    m: CandidateMetrics, t: GateThresholds, *, judged: bool, priced: bool
) -> list[ThresholdCheck]:
    def check(name: str, direction: str, threshold: float | None, actual: float | None):
        if threshold is None:
            return None
        if actual is None:
            return ThresholdCheck(
                name=name, direction=direction, threshold=threshold, actual=None, passed=True
            )
        ok = actual >= threshold if direction == "min" else actual <= threshold
        return ThresholdCheck(
            name=name, direction=direction, threshold=threshold, actual=round(actual, 4), passed=ok
        )

    checks = [
        check("run errors", "max", float(t.max_errors), float(m.errors)),
        check("tool-call fidelity", "min", t.min_tool_fidelity, m.tool_fidelity_rate),
        check("schema validity", "min", t.min_schema_valid, m.schema_valid_rate),
        check("quality loss rate", "max", t.max_quality_loss,
              m.quality_loss_rate if judged else None),
        check("p95 latency ratio", "max", t.max_latency_p95_ratio, m.latency_p95_ratio),
        check("cost ratio", "max", t.max_cost_ratio, m.cost_ratio if priced else None),
    ]
    return [c for c in checks if c is not None]


def decide(
    model: str,
    checks: list[ThresholdCheck],
    patched_checks: list[ThresholdCheck] | None = None,
    addendum: str | None = None,
) -> Verdict:
    failed = [c.name for c in checks if not c.passed]
    if not failed:
        return Verdict(
            candidate_model=model,
            decision=GateDecision.SHIP,
            checks=checks,
            summary="All gate checks passed.",
        )
    if patched_checks and addendum and all(c.passed for c in patched_checks):
        return Verdict(
            candidate_model=model,
            decision=GateDecision.SHIP_WITH_FIX,
            checks=checks,
            patched_checks=patched_checks,
            prompt_addendum=addendum,
            summary=(
                f"Failed {', '.join(failed)} as-is; passes every check with the suggested "
                "system-prompt addendum applied."
            ),
        )
    still = [c.name for c in (patched_checks or []) if not c.passed]
    tail = f" Still failing with the suggested fix: {', '.join(still)}." if patched_checks else ""
    return Verdict(
        candidate_model=model,
        decision=GateDecision.DONT_SHIP,
        checks=checks,
        patched_checks=patched_checks or [],
        prompt_addendum=addendum,
        summary=f"Failed: {', '.join(failed)}.{tail}",
    )
