"""Spark Gate dashboard: one signal per candidate model, and the evidence behind it.

Run:  streamlit run app/dashboard.py
Reads report JSON written by `sparkgate run` from $SPARKGATE_REPORTS (default
.sparkgate/reports, then results/). Every value that came from a model is HTML-escaped before it is
placed in markup; only the static stylesheet is injected unescaped.
"""

from __future__ import annotations

import html
import os
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from pydantic import ValidationError

from sparkgate.gate import PERCENT_CHECKS
from sparkgate.policy import describe
from sparkgate.schemas import (
    CalibrationReport,
    CandidateMetrics,
    GateDecision,
    ReplayResult,
    RunReport,
    ThresholdCheck,
    TraceDiff,
)


def _reports_dir() -> Path:
    if env := os.environ.get("SPARKGATE_REPORTS"):
        return Path(env)
    local = Path(".sparkgate/reports")
    # `results/` holds reports committed to the repo, for the hosted dashboard.
    return local if local.is_dir() else Path("results")


REPORTS = _reports_dir()

INK, STEEL, PAPER = "#18202B", "#5B6B7F", "#EEF1F5"
GO, CAUTION, STOP, COBALT = "#1F8A5B", "#C98A12", "#C2362F", "#2C5BD8"
SIGNAL = {
    GateDecision.SHIP: (GO, "Ship"),
    GateDecision.SHIP_WITH_FIX: (CAUTION, "Ship with the prompt fix"),
    GateDecision.DONT_SHIP: (STOP, "Don't ship"),
}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
[data-testid="stAppViewContainer"] {{ background:{PAPER}; }}
html, body, [class*="css"], .stMarkdown, .stText {{ font-family: 'IBM Plex Sans', sans-serif; }}
h1, h2, h3 {{ font-family: 'Archivo', sans-serif; font-stretch: 112%; letter-spacing: -0.01em; color: {INK}; }}
.block-container {{ padding-top: 4rem; max-width: 1180px; }}
.sg-head {{ display:flex; align-items:baseline; gap:1rem; flex-wrap:wrap; margin-bottom:.4rem; }}
.sg-title {{ font-family:'Archivo'; font-stretch:125%; font-weight:900; font-size:clamp(1.6rem, 3.4vw, 2.6rem) !important; line-height:1.1; color:{INK}; margin:0 0 .3rem; }}
[data-testid="stHeader"] {{ background:transparent; }}
.sg-scroll {{ overflow-x:auto; }}
.sg-sub {{ color:{STEEL}; font-size:.98rem; }}
.sg-board {{ background:{INK}; border-radius:14px; padding:10px; display:grid; gap:8px; margin:1rem 0 1.6rem; }}
.sg-plate {{ display:grid; grid-template-columns: 54px minmax(0,1.3fr) minmax(0,2fr); align-items:center;
  gap:18px; background:#232D3A; border-radius:8px; padding:14px 18px; }}
.sg-lamp {{ width:30px; height:30px; border-radius:50%; margin:auto; box-shadow:0 0 0 5px #11171F, 0 0 22px 2px var(--glow); }}
.sg-model {{ font-family:'Archivo'; font-stretch:118%; font-weight:800; font-size:1.35rem; color:#F3F6FA; }}
.sg-call {{ font-family:'Archivo'; font-weight:700; font-size:.95rem; }}
.sg-why {{ color:#B8C3D1; font-size:.9rem; line-height:1.45; }}
.sg-why b {{ color:#F3F6FA; font-weight:600; }}
.sg-note {{ color:{STEEL}; font-size:.88rem; }}
.sg-matrix {{ width:100%; border-collapse:separate; border-spacing:4px; font-size:.9rem; }}
.sg-matrix th {{ text-align:left; color:{STEEL}; font-weight:500; padding:4px 8px; }}
.sg-matrix td {{ padding:10px 12px; border-radius:6px; font-family:'IBM Plex Mono'; }}
.sg-pass {{ background:#DCEFE4; color:#145C3D; }}
.sg-fail {{ background:#F6DAD7; color:#8C211B; }}
.sg-skip {{ background:#E2E7EE; color:{STEEL}; }}
.sg-model-cell {{ font-family:'IBM Plex Sans' !important; font-weight:600; color:{INK}; background:transparent !important; }}
.sg-chip {{ display:inline-block; padding:3px 9px; margin:2px 4px 2px 0; border-radius:999px; font-family:'IBM Plex Mono'; font-size:.8rem;
  background:#E2E7EE; color:{INK}; }}
.sg-chip.missing {{ background:#F6DAD7; color:#8C211B; text-decoration:line-through; }}
.sg-chip.extra {{ background:#FBEBC9; color:#7A5208; }}
.sg-audit {{ background:#FFF6E3; border-left:4px solid {CAUTION}; border-radius:6px; padding:.8rem 1rem;
  margin:-.6rem 0 1.4rem; color:{INK}; }}
.sg-audit ul {{ margin:.4rem 0 0 1.1rem; padding:0; }}
.sg-audit li {{ font-size:.9rem; color:#4A3A12; }}
.sg-big {{ font-family:'Archivo'; font-stretch:118%; font-weight:800; font-size:2.4rem; line-height:1; color:{INK}; }}
.sg-stat {{ display:inline-block; margin:0 2.4rem 1rem 0; vertical-align:top; }}
.sg-bar {{ height:8px; border-radius:4px; background:#E2E7EE; overflow:hidden; min-width:90px; }}
.sg-bar > span {{ display:block; height:100%; }}
.sg-pattern {{ border-left:4px solid {COBALT}; padding:.2rem 0 .2rem 1rem; margin:1.2rem 0 .4rem; }}
.sg-pattern h4 {{ margin:0 0 .2rem; font-family:'Archivo'; font-stretch:105%; }}
@media (max-width: 720px) {{
  .sg-plate {{ grid-template-columns: 40px 1fr; }}
  .sg-why {{ grid-column: 1 / -1; }}
}}
</style>
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


@st.cache_data(show_spinner=False)
def load_reports(folder: str) -> list[RunReport]:
    path = Path(folder)
    if not path.is_dir():
        return []
    out = []
    for f in sorted(path.glob("*.json")):
        if (f.name == "latest.json" or f.name.startswith("calibration-")
                or f.stat().st_size > 50 * 2**20):
            continue
        try:
            out.append(RunReport.model_validate_json(f.read_text(encoding="utf-8")))
        except (ValidationError, OSError):
            continue
    return sorted(out, key=lambda r: r.created_at, reverse=True)


@st.cache_data(show_spinner=False)
def load_calibration(folder: str) -> CalibrationReport | None:
    path = Path(folder) / "calibration-latest.json"
    if not path.is_file() or path.stat().st_size > 10 * 2**20:
        return None
    try:
        return CalibrationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, OSError):
        return None


def fmt_value(name: str, x: float | None) -> str:
    if x is None:
        return "n/a"
    if name in PERCENT_CHECKS:
        return f"{x:.0%}"
    if name == "run errors":
        return f"{x:.0f}"
    return f"{x:.2f}×"


def fmt_check(c: ThresholdCheck) -> str:
    return fmt_value(c.name, c.actual)


def fmt_limit(c: ThresholdCheck) -> str:
    return ("at least " if c.direction == "min" else "at most ") + fmt_value(c.name, c.threshold)


def signal_board(report: RunReport) -> str:
    rows = []
    for v in report.verdicts:
        color, call = SIGNAL[v.decision]
        failed = [c for c in v.checks if not c.passed]
        if failed:
            why = "; ".join(
                f"<b>{esc(c.name)}</b> {esc(fmt_check(c))} ({esc(fmt_limit(c))})"
                for c in failed
            )
            if v.decision is GateDecision.SHIP_WITH_FIX:
                why += ". Passes every check once the suggested prompt fix is applied."
        else:
            why = "Every check passed against the baseline."
        rows.append(
            f'<div class="sg-plate"><div class="sg-lamp" style="background:{color};--glow:{color}66"></div>'
            f'<div><div class="sg-model">{esc(v.candidate_model)}</div>'
            f'<div class="sg-call" style="color:{color}">{esc(call)}</div></div>'
            f'<div class="sg-why">{why}</div></div>'
        )
    return f'<div class="sg-board" role="list">{"".join(rows)}</div>'


def policy_audit(report: RunReport) -> str:
    audit = report.baseline_policy
    if not audit or audit.pass_rate is None or not audit.failures:
        return ""
    items = "".join(
        f"<li><b>{esc(tid)}</b>: {esc('; '.join(describe(r) for r in rules))}</li>"
        for tid, rules in list(audit.failures.items())[:8]
    )
    if len(audit.failures) > 8:
        items += f"<li>and {len(audit.failures) - 8} more (see the Traces tab)</li>"
    return (
        f'<div class="sg-audit"><b>{esc(audit.model)} already breaks a policy rule on '
        f"{len(audit.failures)} of {audit.traces_checked} conversations.</b> A gate that only "
        f"compares against the current model would never flag these.<ul>{items}</ul></div>"
    )


def reliability(cal: CalibrationReport | None) -> None:
    if cal is None:
        st.info("No calibration yet. Run it on your traces (free, no model calls):")
        st.code("sparkgate calibrate            # seeded faults\n"
                "sparkgate calibrate --judge    # also audit the judge model", language="bash")
        return
    stats = []
    if cal.detection_rate is not None:
        stats.append((f"{cal.detection_rate:.0%}", "of seeded faults caught"))
    if cal.false_positive_rate is not None:
        stats.append((f"{cal.false_positive_rate:.0%}", "false alarms on unchanged runs"))
    j = cal.judge
    if j and j.asked_pairs:
        stats.append((f"{j.orders_agreed / j.asked_pairs:.0%}",
                      "judge agreed with itself when the order was swapped"))
    st.markdown("".join(f'<div class="sg-stat"><div class="sg-big">{esc(v)}</div>'
                        f'<div class="sg-note">{esc(k)}</div></div>' for v, k in stats),
                unsafe_allow_html=True)
    st.markdown(f'<p class="sg-note">Each fault is injected into a copy of a recorded run and '
                f"put through the same checks a real candidate gets. {cal.n_traces} traces.</p>",
                unsafe_allow_html=True)
    rows = []
    for o in cal.operators:
        rate = o.rate
        color = GO if (rate or 0) >= 0.95 else (CAUTION if (rate or 0) >= 0.5 else STOP)
        if o.operator == "unchanged":
            color = GO if (rate or 0) == 0 else STOP
        bar = (f'<div class="sg-bar"><span style="width:{(rate or 0) * 100:.0f}%;'
               f'background:{color}"></span></div>') if rate is not None else ""
        layers = ", ".join(f"{k} ({v})" for k, v in sorted(o.caught_by.items())) or "—"
        claim = "yes" if o.should_catch else ("control" if o.operator == "unchanged" else "no")
        rows.append(f"<tr><td class='sg-model-cell'>{esc(o.description)}</td>"
                    f"<td class='sg-skip'>{esc(claim)}</td><td class='sg-skip'>{o.mutants}</td>"
                    f"<td class='sg-skip'>{'' if rate is None else f'{rate:.0%}'}{bar}</td>"
                    f"<td class='sg-skip' style=\"font-family:'IBM Plex Sans'\">{esc(layers)}</td></tr>")
    st.markdown('<div class="sg-scroll"><table class="sg-matrix"><thead><tr><th>Seeded fault</th>'
                "<th>Designed to catch</th><th>Cases</th><th>Flagged</th><th>Caught by</th>"
                f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>',
                unsafe_allow_html=True)
    if j:
        lines = []
        if j.degraded_pairs:
            lines.append(f"Preferred the real answer over another customer's answer in "
                         f"{j.degraded_caught} of {j.degraded_pairs} pairs "
                         f"(picked the wrong one {j.degraded_inverted} times).")
        if j.control_pairs:
            lines.append(f"Called two equivalent answers a tie in {j.control_ties} of "
                         f"{j.control_pairs} pairs.")
        lines.append(f"Audit spend ${j.cost_usd:.4f}.")
        st.markdown(f"#### Judge: {esc(j.model)}")
        st.markdown("".join(f'<p class="sg-note">{esc(x)}</p>' for x in lines),
                    unsafe_allow_html=True)


def check_matrix(report: RunReport) -> str:
    names = [c.name for c in report.verdicts[0].checks] if report.verdicts else []
    head = "".join(f"<th>{esc(n)}</th>" for n in names)
    body = []
    for v in report.verdicts:
        cells = []
        for c in v.checks:
            cls = "sg-skip" if c.actual is None else ("sg-pass" if c.passed else "sg-fail")
            cells.append(f'<td class="{cls}" title="{esc(fmt_limit(c))}">'
                         f"{esc(fmt_check(c))}</td>")
        body.append(f'<tr><td class="sg-model-cell">{esc(v.candidate_model)}</td>{"".join(cells)}</tr>')
    return (f'<div class="sg-scroll"><table class="sg-matrix"><thead><tr><th>Candidate</th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def frontier(metrics: list[CandidateMetrics], report: RunReport) -> go.Figure:
    decision = {v.candidate_model: v.decision for v in report.verdicts}
    fig = go.Figure()
    fig.add_vline(x=1, line={"color": "#C5CED9", "dash": "dot"})
    fig.add_hline(y=0, line={"color": "#C5CED9", "dash": "dot"})
    fig.add_trace(go.Scatter(
        x=[1.0], y=[0.0], mode="markers+text", text=[report.baseline_model],
        textposition="bottom center", marker={"size": 14, "color": STEEL, "symbol": "diamond"},
        hovertemplate="baseline<extra></extra>", showlegend=False,
    ))
    for m in metrics:
        color = SIGNAL[decision[m.model]][0]
        fig.add_trace(go.Scatter(
            x=[m.cost_ratio], y=[m.quality_win_rate - m.quality_loss_rate],
            mode="markers+text", text=[m.model], textposition="top center",
            marker={"size": 14 + 26 * m.tool_fidelity_rate, "color": color,
                        "line": {"color": "white", "width": 2}},
            customdata=[[m.tool_fidelity_rate, m.latency_p95_ratio]],
            hovertemplate=("%{text}<br>cost %{x:.2f}× baseline<br>net quality %{y:+.0%}"
                           "<br>tool fidelity %{customdata[0]:.0%}"
                           "<br>p95 latency %{customdata[1]:.2f}×<extra></extra>"),
            showlegend=False,
        ))
    fig.update_layout(
        height=380, margin={"l": 10, "r": 10, "t": 10, "b": 10}, plot_bgcolor="white",
        paper_bgcolor="rgba(0,0,0,0)", font={"family": "IBM Plex Sans", "color": INK},
        xaxis={"title": "Cost relative to baseline", "ticksuffix": "×", "gridcolor": "#E6EBF1"},
        yaxis={"title": "Judged wins minus losses", "tickformat": "+.0%", "gridcolor": "#E6EBF1"},
    )
    return fig


def chips(names: list[str], missing: set[str], extra: set[str]) -> str:
    if not names and not missing:
        return '<span class="sg-note">No tool calls</span>'
    out = []
    for n in names:
        cls = "extra" if n in extra else ""
        out.append(f'<span class="sg-chip {cls}">{esc(n)}</span>')
    out += [f'<span class="sg-chip missing">{esc(n)}</span>' for n in sorted(missing)]
    return "".join(out)


def trace_explorer(report: RunReport) -> None:
    models = [v.candidate_model for v in report.verdicts]
    c1, c2, c3 = st.columns([1.2, 1, 1.6])
    model = c1.selectbox("Candidate", models)
    variants = sorted({d.variant for d in report.diffs if d.candidate_model == model
                       and not d.variant.startswith("fix:")})
    variant = c2.selectbox("Prompt", variants,
                           format_func=lambda v: "as-is" if v == "original" else "with fix")
    diffs = [d for d in report.diffs if d.candidate_model == model and d.variant == variant]
    only_reg = c3.toggle("Only regressions", value=True)
    if only_reg:
        diffs = [d for d in diffs if d.is_regression]
    if not diffs:
        st.info("No regressed traces for this candidate. Turn off the filter to browse all traces.")
        return
    d: TraceDiff = st.selectbox(
        "Trace", diffs,
        format_func=lambda x: f"{x.trace_id}  —  {', '.join(x.regression_codes) or 'no regression'}",
    )
    replays: dict[tuple[str, str, str], ReplayResult] = {
        (r.model, r.variant, r.trace_id): r for r in report.replays
    }
    base = replays.get((report.baseline_model, "original", d.trace_id))
    cand = replays.get((model, variant, d.trace_id))

    left, right = st.columns(2)
    with left:
        st.markdown(f"#### {esc(report.baseline_model)}")
        st.markdown(chips(d.baseline_tools, set(), set()), unsafe_allow_html=True)
        st.code(base.final_text if base else "", language="json", wrap_lines=True)
    with right:
        st.markdown(f"#### {esc(model)}")
        st.markdown(chips(d.candidate_tools, set(d.missing_tools), set(d.extra_tools)),
                    unsafe_allow_html=True)
        st.code(cand.final_text if cand else "", language="json", wrap_lines=True)
    facts: list[tuple[str, str]] = []
    if d.quality_reason:
        facts.append(("Judge", f"{d.quality.value.replace('_', ' ')}. {d.quality_reason}"))
    if d.schema_errors and d.candidate_schema_valid is False:
        facts.append(("Schema", "; ".join(d.schema_errors)))
    if d.candidate_policy_violations:
        facts.append(("Policy", "; ".join(describe(v) for v in d.candidate_policy_violations)))
    if d.baseline_policy_violations:
        facts.append((f"Policy ({report.baseline_model})",
                      "; ".join(describe(v) for v in d.baseline_policy_violations)))
    if d.arg_changes:
        facts.append(("Arguments changed", "; ".join(d.arg_changes)))
    if d.unrecorded_calls:
        facts.append(("Unrecorded calls", (f"{d.unrecorded_calls} call(s) had no recorded result "
                      "and were answered with a tool-unavailable error.")))
    st.markdown("".join(f"<p><b>{esc(k)}:</b> {esc(v)}</p>" for k, v in facts),
                unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="Spark Gate", page_icon="🚦", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    reports = load_reports(str(REPORTS))
    if not reports:
        st.markdown('<div class="sg-title">Spark Gate</div>', unsafe_allow_html=True)
        st.warning(f"No gate runs found in `{REPORTS}`.")
        st.markdown("Record traces, then run the gate:\n\n"
                    "```bash\npython -m demo.support_agent.record --model muse-spark-1.1\n"
                    "sparkgate run --config sparkgate.yaml\n```")
        return

    with st.sidebar:
        report = st.selectbox(
            "Run", reports,
            format_func=lambda r: f"{r.created_at:%b %d, %H:%M} UTC · {r.run_id}",
        )
        st.caption(f"{report.n_traces} traces · spend ${report.total_cost_usd:.2f}")
        if report.noise_floor:
            st.caption(
                f"Noise floor: {report.baseline_model} itself changed its tool sequence on "
                f"{report.noise_floor.tool_sequence_drift_rate:.0%} of re-runs."
            )

    st.markdown(
        f'<div class="sg-head"><div class="sg-title">Can we upgrade from '
        f"{esc(report.baseline_model)}?</div></div>"
        f'<div class="sg-sub">{report.n_traces} real conversations replayed on each candidate, '
        f"with every tool answer served from the original recording.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(signal_board(report), unsafe_allow_html=True)
    if audit_html := policy_audit(report):
        st.markdown(audit_html, unsafe_allow_html=True)

    tab_checks, tab_patterns, tab_traces, tab_reliability = st.tabs(
        ["Checks", "What broke and the fix", "Traces", "Can you trust this gate?"]
    )
    originals = [m for m in report.metrics if m.variant == "original"]

    with tab_checks:
        st.markdown(check_matrix(report), unsafe_allow_html=True)
        st.markdown("### Cost against quality")
        st.markdown('<p class="sg-note">Bubble size is tool-call fidelity. Up and to the left '
                    "is better than the baseline on both.</p>", unsafe_allow_html=True)
        st.plotly_chart(frontier(originals, report), width="stretch",
                        config={"displayModeBar": False})

    with tab_patterns:
        if not report.patterns:
            st.success("No regressions on any candidate.")
        for v in report.verdicts:
            pats = [p for p in report.patterns if p.candidate_model == v.candidate_model]
            if not pats:
                continue
            st.markdown(f"### {esc(v.candidate_model)}")
            for p in pats:
                fix_line = ""
                if p.suggested_fix:
                    fix_line = (f"Fix repaired {len(p.fix_resolved_ids)} of {len(p.trace_ids)} "
                                "on re-test." if p.fix_accepted else
                                f"Fix repaired 0 of {len(p.trace_ids)} on re-test; not used.")
                st.markdown(
                    f'<div class="sg-pattern"><h4>{esc(p.title)}</h4>'
                    f'<div>{esc(p.description)}</div>'
                    f'<div class="sg-note">{len(p.trace_ids)} traces · {esc(p.code)}'
                    f'{" · " + esc(fix_line) if fix_line else ""}</div></div>',
                    unsafe_allow_html=True,
                )
                if p.suggested_fix:
                    st.code(p.suggested_fix, language="text", wrap_lines=True)
            if v.prompt_addendum and v.patched_checks:
                st.markdown("**Checks with the combined fix applied**")
                fixed = v.model_copy(update={"checks": v.patched_checks})
                st.markdown(check_matrix(report.model_copy(update={"verdicts": [fixed]})),
                            unsafe_allow_html=True)

    with tab_traces:
        trace_explorer(report)

    with tab_reliability:
        reliability(load_calibration(str(REPORTS)))


main()
