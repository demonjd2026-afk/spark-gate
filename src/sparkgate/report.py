"""Markdown report for pull-request comments and job summaries.

Model output is untrusted. Every value rendered into Markdown goes through `md()`,
which strips control characters, escapes table and HTML syntax and length-limits
it, so a model cannot inject links, images, HTML or mentions into the PR comment.
"""

from __future__ import annotations

import re

from sparkgate.gate import PERCENT_CHECKS
from sparkgate.policy import describe
from sparkgate.schemas import CalibrationReport, GateDecision, RunReport, ThresholdCheck

_BADGE = {
    GateDecision.SHIP: "✅ SHIP",
    GateDecision.SHIP_WITH_FIX: "🟡 SHIP WITH FIX",
    GateDecision.DONT_SHIP: "⛔ DON'T SHIP",
}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#!|~>])")


def md(value: object, limit: int = 300) -> str:
    text = _CTRL.sub("", str(value)).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("@", "@\u200b")  # break @mentions
    return _MD_SPECIAL.sub(r"\\\1", text)


def _fmt(check: ThresholdCheck) -> str:
    pct = check.name in PERCENT_CHECKS

    def f(x: float) -> str:
        return f"{x:.0%}" if pct else (f"{x:.0f}" if check.name == "run errors" else f"{x:.2f}×")

    actual = "skipped" if check.actual is None else f(check.actual)
    op = "≥" if check.direction == "min" else "≤"
    mark = "✅" if check.passed else "❌"
    return f"| {check.name} | {actual} | {op} {f(check.threshold)} | {mark} |"


def hosting_label(report: RunReport, model: str) -> str:
    info = report.model_info.get(model)
    if info is None:
        return ""
    kind = "open weights" if info.hosting == "open_weights" else "Meta Model API"
    return f"{kind} ({info.served_by})" if info.served_by and info.hosting == "open_weights" else kind


def to_markdown(report: RunReport, *, max_patterns: int = 6) -> str:
    out: list[str] = ["## Spark Gate", ""]
    out.append(
        f"Baseline **{md(report.baseline_model)}** · {report.n_traces} traces · "
        f"run `{md(report.run_id)}` · spend ${report.total_cost_usd:.4f}"
    )
    if report.noise_floor:
        out.append(
            f"\nNoise floor: the baseline re-ran with a different tool sequence on "
            f"{report.noise_floor.tool_sequence_drift_rate:.0%} of traces."
        )
    audit = report.baseline_policy
    if audit and audit.pass_rate is not None:
        failing = len(audit.failures)
        out.append(
            f"\nPolicy audit: the current model **{md(audit.model)}** breaks a policy rule on "
            f"{failing} of {audit.traces_checked} traces."
        )
        for tid, rules in list(audit.failures.items())[:5]:
            out.append(f"- `{md(tid, 80)}`: {md('; '.join(describe(r) for r in rules), 300)}")
    if report.inconclusive_trace_ids:
        out.append(
            f"\n{len(report.inconclusive_trace_ids)} trace(s) excluded because the baseline "
            "replay itself failed."
        )
    out.append("")
    out.append("| Candidate | Served by | Verdict | Summary |")
    out.append("|---|---|---|---|")
    for v in report.verdicts:
        out.append(f"| {md(v.candidate_model)} | {md(hosting_label(report, v.candidate_model))} | "
                   f"{_BADGE[v.decision]} | {md(v.summary, 400)} |")
    for model, reason in report.skipped_candidates.items():
        out.append(f"| {md(model)} | - | skipped | {md(reason)} |")

    for v in report.verdicts:
        out += ["", f"### {md(v.candidate_model)} — {_BADGE[v.decision]}", ""]
        out += ["| Check | Actual | Threshold | |", "|---|---|---|---|"]
        out += [_fmt(c) for c in v.checks]
        if v.patched_checks:
            out += ["", "With the suggested prompt addendum:", ""]
            out += ["| Check | Actual | Threshold | |", "|---|---|---|---|"]
            out += [_fmt(c) for c in v.patched_checks]
        pats = [p for p in report.patterns if p.candidate_model == v.candidate_model]
        if pats:
            out += ["", "<details><summary>Regression patterns</summary>", ""]
            for p in pats[:max_patterns]:
                status = ""
                if p.suggested_fix:
                    status = (
                        f" — fix repaired {len(p.fix_resolved_ids)}/{len(p.trace_ids)} on re-test"
                    )
                out.append(f"- **{md(p.title, 120)}** ({len(p.trace_ids)} traces){status}")
                out.append(f"  - {md(p.description, 400)}")
            out += ["", "</details>"]
        if v.prompt_addendum:
            body = _CTRL.sub("", v.prompt_addendum)[:1_500].replace("```", "ʼʼʼ")
            out += ["", "Suggested system-prompt addendum:", "", "```text", body, "```"]
    return "\n".join(out) + "\n"


def calibration_markdown(cal: CalibrationReport) -> str:
    out = ["## Spark Gate calibration", ""]
    if cal.detection_rate is not None:
        out.append(
            f"Seeded faults caught: **{cal.detection_rate:.0%}** · false positives on unchanged "
            f"runs: **{(cal.false_positive_rate or 0):.0%}** · {cal.n_traces} traces"
        )
    out += ["", "| Fault | Claimed | Mutants | Caught | Caught by |", "|---|---|---|---|---|"]
    for s in cal.operators:
        rate = "-" if s.rate is None else f"{s.rate:.0%}"
        layers = ", ".join(f"{k} {v}" for k, v in sorted(s.caught_by.items())) or "-"
        claimed = "yes" if s.should_catch else "no"
        out.append(f"| {md(s.description)} | {claimed} | {s.mutants} | {rate} | {md(layers)} |")
    if cal.judge:
        j = cal.judge
        out += ["", f"Judge ({md(j.model)}):"]
        if j.degraded_pairs:
            out.append(f"- prefers the real answer over another customer's answer: "
                       f"{j.degraded_caught}/{j.degraded_pairs}, inverted {j.degraded_inverted}")
        if j.control_pairs:
            out.append(f"- calls equivalent answers a tie: {j.control_ties}/{j.control_pairs}")
        if j.asked_pairs:
            out.append(f"- both orderings agreed: {j.orders_agreed}/{j.asked_pairs}")
        out.append(f"- spend: ${j.cost_usd:.4f}")
    return "\n".join(out) + "\n"
