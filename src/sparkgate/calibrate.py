"""How reliable is the gate itself? Mutation testing for the checks, and a judge audit.

Deterministic part (free, offline). Each recorded trace is treated as the baseline
run. A fault is only seeded where it is not already present (a forbidden tool the
baseline already calls cannot be "added"). Operators inject one known fault into a copy of it (drop a tool call,
corrupt the JSON answer, change a refund amount, ...), and the copy is put through
exactly the same diff + policy pipeline a real candidate goes through. That measures:

  * detection rate, per fault type and overall, for faults the deterministic checks
    are designed to catch;
  * false-positive rate, on unchanged copies (which must never be flagged);
  * blind spots, reported rather than hidden: operators marked `should_catch=False`
    are faults the deterministic layer does not claim to see (e.g. a changed argument
    on a read-only tool), so their rate shows what is left to the judge.

Judge part (optional, costs API calls). Two kinds of pairs per trace:
  * degraded: the real answer vs the answer recorded for a *different* customer,
    which is coherent text but wrong for this conversation. A sound judge prefers
    the real answer in both orderings.
  * control: the real answer vs the same answer re-serialised (different whitespace,
    identical meaning). A sound judge calls it a tie.
Both report how often the two orderings agreed, which is the judge's own consistency.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from sparkgate.diff import SideEffectTools, diff_replays
from sparkgate.jsontext import extract_json
from sparkgate.judge import PairwiseJudge
from sparkgate.schemas import (
    CalibrationReport,
    JudgeCalibration,
    OperatorStats,
    QualityVerdict,
    ReplayResult,
    ToolCall,
    Trace,
)

Mutant = tuple[str, ReplayResult]  # (label, mutated run)


def recorded_run(trace: Trace) -> ReplayResult:
    return ReplayResult(
        trace_id=trace.id,
        model=trace.recorded_model or "recorded",
        calls=[c.model_copy() for c in trace.tool_calls],
        final_text=trace.final_text,
    )


def _with(run: ReplayResult, **update: Any) -> ReplayResult:
    return run.model_copy(update={"model": "mutant", **update}, deep=True)


def _perturb(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return round(value * 1.5 + 1, 2)
    if isinstance(value, str):
        return value + "-X"
    return "__mutated__"


def _perturb_call(call: ToolCall, ignore: list[str]) -> ToolCall | None:
    for key in sorted(call.arguments):
        if key not in ignore:
            args = {**call.arguments, key: _perturb(call.arguments[key])}
            return call.model_copy(update={"arguments": args})
    return None


# --------------------------------------------------------------------------- #
# Operators: each yields zero or more mutants of a known-good run
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Operator:
    name: str
    description: str
    should_catch: bool
    apply: Callable[[Trace, ReplayResult, SideEffectTools], Iterator[Mutant]]


def _identity(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    yield "unchanged", _with(run)


def _drop_tool(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    for name in dict.fromkeys(c.name for c in run.calls):
        i = next(i for i, c in enumerate(run.calls) if c.name == name)
        yield f"drop {name}", _with(run, calls=run.calls[:i] + run.calls[i + 1:])


def _swap_order(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    calls = run.calls
    for i in range(len(calls) - 1):
        if calls[i].name != calls[i + 1].name:
            swapped = calls[:i] + [calls[i + 1], calls[i]] + calls[i + 2:]
            yield f"swap {calls[i].name}/{calls[i + 1].name}", _with(run, calls=swapped)
            return


def _repeat_side_effect(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    for i, c in enumerate(run.calls):
        if c.name in side:
            yield f"repeat {c.name}", _with(run, calls=run.calls[: i + 1] + run.calls[i:])
            return


def _side_effect_arg(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    for i, c in enumerate(run.calls):
        if c.name in side and (changed := _perturb_call(c, list(side[c.name]))):
            calls = [*run.calls[:i], changed, *run.calls[i + 1:]]
            yield f"change {c.name} args", _with(run, calls=calls)
            return


def _read_arg(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    for i, c in enumerate(run.calls):
        if c.name not in side and (changed := _perturb_call(c, [])):
            calls = [*run.calls[:i], changed, *run.calls[i + 1:]]
            yield f"change {c.name} args", _with(run, calls=calls)
            return


def _broken_json(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    if t.output_schema is not None and len(run.final_text) > 2:
        yield "truncate answer", _with(run, final_text=run.final_text[: len(run.final_text) // 2])


def _schema_enum(schema: dict[str, Any] | None, path: str) -> list[Any]:
    node: Any = schema or {}
    for part in path.split("."):
        node = (node.get("properties") or {}).get(part, {}) if isinstance(node, dict) else {}
    return list(node.get("enum", [])) if isinstance(node, dict) else []


def _answer_field(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    exp = t.expectations
    parsed = extract_json(run.final_text)
    if exp is None or not exp.answer or not isinstance(parsed, dict):
        return
    path, allowed = next(iter(exp.answer.items()))
    current: Any = parsed
    for part in path.split("."):
        current = current.get(part) if isinstance(current, dict) else None
    if current not in allowed:
        return  # the run already breaks this rule; there is no fault left to seed
    wrong = next((v for v in _schema_enum(t.output_schema, path) if v not in allowed), None)
    if wrong is None:
        wrong = "__mutated__"
    obj = json.loads(json.dumps(parsed))  # deep copy
    node: Any = obj
    *parents, leaf = path.split(".")
    for p in parents:
        node = node.setdefault(p, {}) if isinstance(node, dict) else {}
    if isinstance(node, dict):
        node[leaf] = wrong
        yield f"set {path}={wrong}", _with(run, final_text=json.dumps(obj))


def _forbidden_call(t: Trace, run: ReplayResult, side: SideEffectTools) -> Iterator[Mutant]:
    exp = t.expectations
    already = {c.name for c in run.calls}
    if exp and (tools := [t for t in exp.must_not_call if t not in already]):
        tool = tools[0]
        extra = ToolCall(name=tool, arguments={}, result='{"status":"ok"}', matched="none")
        yield f"add {tool}", _with(run, calls=[*run.calls, extra])


OPERATORS: tuple[Operator, ...] = (
    Operator("unchanged", "No change (false-positive control)", False, _identity),
    Operator("drop_tool", "A tool call is skipped", True, _drop_tool),
    Operator("swap_order", "Two tool calls run in the wrong order", True, _swap_order),
    Operator("repeat_side_effect", "A side-effecting call is made twice", True,
             _repeat_side_effect),
    Operator("side_effect_arg", "A side-effecting call gets a different argument", True,
             _side_effect_arg),
    Operator("broken_json", "The final answer is not valid JSON", True, _broken_json),
    Operator("answer_field", "The final answer reports the wrong outcome", True, _answer_field),
    Operator("forbidden_call", "A tool the case forbids is called", True, _forbidden_call),
    Operator("read_arg", "A read-only call gets a different argument (judge territory)", False,
             _read_arg),
)

_LAYERS = (("policy:", "policy rules"), ("schema_invalid", "output schema"),
           ("args_changed:", "side-effect arguments"), ("dropped_tool:", "tool sequence"),
           ("extra_tool:", "tool sequence"), ("reordered_tools", "tool sequence"))


def _layer(code: str) -> str:
    return next((name for prefix, name in _LAYERS if code.startswith(prefix)), "other")


def run_mutations(traces: list[Trace], side_effect_tools: SideEffectTools) -> CalibrationReport:
    stats = {op.name: OperatorStats(operator=op.name, description=op.description,
                                    should_catch=op.should_catch) for op in OPERATORS}
    for t in traces:
        base = recorded_run(t)
        for op in OPERATORS:
            for _label, mutant in op.apply(t, base, side_effect_tools):
                d = diff_replays(t, base, mutant, side_effect_tools)
                s = stats[op.name]
                s.mutants += 1
                if d.regression_codes:
                    s.caught += 1
                    for layer in dict.fromkeys(_layer(c) for c in d.regression_codes):
                        s.caught_by[layer] = s.caught_by.get(layer, 0) + 1
                else:
                    s.missed_trace_ids.append(t.id)

    claimed = [s for s in stats.values() if s.should_catch]
    total = sum(s.mutants for s in claimed)
    control = stats["unchanged"]
    return CalibrationReport(
        calibration_id=uuid.uuid4().hex[:12],
        n_traces=len(traces),
        operators=list(stats.values()),
        detection_rate=sum(s.caught for s in claimed) / total if total else None,
        false_positive_rate=control.caught / control.mutants if control.mutants else None,
    )


def _reserialise(text: str) -> str:
    parsed = extract_json(text)
    if isinstance(parsed, dict | list):
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    return text.strip() + "\n\n"


async def calibrate_judge(
    judge: PairwiseJudge, judge_model: str, traces: list[Trace], max_pairs: int
) -> JudgeCalibration:
    usable = [t for t in traces if t.final_text.strip()][:max_pairs]
    out = JudgeCalibration(model=judge_model)
    if len(usable) < 2:
        return out

    async def degraded(i: int, t: Trace):
        other = usable[(i + 1) % len(usable)].final_text
        if other.strip() == t.final_text.strip():
            return None
        base = recorded_run(t)
        return await judge.compare(t, base, _with(base, final_text=other))

    async def control(t: Trace):
        base = recorded_run(t)
        variant = _reserialise(t.final_text)
        if variant.strip() == t.final_text.strip():
            variant = t.final_text + "\n"
        return await judge.compare(t, base, _with(base, final_text=variant))

    deg = await asyncio.gather(*(degraded(i, t) for i, t in enumerate(usable)))
    ctl = await asyncio.gather(*(control(t) for t in usable))
    for j in deg:
        if j is None:
            continue
        out.degraded_pairs += 1
        out.degraded_caught += j.verdict is QualityVerdict.BASELINE_BETTER
        out.degraded_inverted += j.verdict is QualityVerdict.CANDIDATE_BETTER
    for j in ctl:
        if j.orders_agreed is None and j.verdict is not QualityVerdict.NOT_JUDGED:
            continue  # texts compared equal and the judge was never asked
        out.control_pairs += 1
        out.control_ties += j.verdict is QualityVerdict.TIE
    for j in [*(x for x in deg if x is not None), *ctl]:
        out.cost_usd += j.cost_usd
        if j.verdict is QualityVerdict.NOT_JUDGED:
            out.not_judged += 1
        if j.orders_agreed is not None:
            out.asked_pairs += 1
            out.orders_agreed += j.orders_agreed
    out.cost_usd = round(out.cost_usd, 6)
    return out
