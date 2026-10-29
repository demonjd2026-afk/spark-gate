"""Absolute, per-trace policy checks (Trace.expectations).

Pure and deterministic. Each violation is a short, stable code so violations can be
grouped, compared between baseline and candidate, and counted.
"""

from __future__ import annotations

from typing import Any

from sparkgate.jsontext import extract_json
from sparkgate.schemas import ArgRule, Expectations, ReplayResult, ReplayStatus, ToolCall

_MISSING = object()


def _lookup(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _same(a: Any, b: Any) -> bool:
    # 1 == True in Python; a policy value of true must not accept the number 1.
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def _arg_ok(rule: ArgRule, value: Any) -> bool:
    if rule.one_of is not None and not any(_same(value, v) for v in rule.one_of):
        return False
    if rule.min is not None or rule.max is not None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        if rule.min is not None and value < rule.min:
            return False
        if rule.max is not None and value > rule.max + 1e-9:
            return False
    return True


def check_calls(exp: Expectations, calls: list[ToolCall]) -> list[str]:
    names = [c.name for c in calls]
    out: list[str] = []
    out += [f"must_call:{t}" for t in exp.must_call if t not in names]
    out += [f"must_not_call:{t}" for t in exp.must_not_call if t in names]
    for first, then in exp.call_before:
        for i, n in enumerate(names):
            if n == then and first not in names[:i]:
                out.append(f"call_before:{first}>{then}")
                break
    for key, rule in exp.args.items():
        tool, arg = key.split(".")
        for c in calls:
            if c.name == tool and arg in c.arguments and not _arg_ok(rule, c.arguments[arg]):
                out.append(f"arg:{key}")
                break
            if c.name == tool and arg not in c.arguments and rule.min is not None:
                out.append(f"arg:{key}")
                break
    if exp.max_tool_calls is not None and len(calls) > exp.max_tool_calls:
        out.append("too_many_tool_calls")
    return out


def check_answer(exp: Expectations, final_text: str) -> list[str]:
    out: list[str] = []
    if exp.answer:
        parsed = extract_json(final_text)
        for path, allowed in exp.answer.items():
            value = _lookup(parsed, path) if isinstance(parsed, dict) else _MISSING
            if value is _MISSING or not any(_same(value, a) for a in allowed):
                out.append(f"answer:{path}")
    lowered = final_text.lower()
    for i, term in enumerate(exp.answer_must_not_contain):
        if term and term.lower() in lowered:
            out.append(f"answer_leak:{i}")
    return out


def violations(exp: Expectations | None, run: ReplayResult) -> list[str] | None:
    """Violated rules for one run, or None when the trace carries no expectations."""
    if exp is None:
        return None
    if run.status is not ReplayStatus.OK:
        return [f"run_failed:{run.status.value}"]
    return check_calls(exp, run.calls) + check_answer(exp, run.final_text)


def describe(violation: str) -> str:
    """Human-readable sentence for a violation code."""
    kind, _, arg = violation.partition(":")
    if kind == "call_before":
        first, _, then = arg.partition(">")
        return f"Calls {then} without calling {first} first"
    return {
        "must_call": f"Never calls {arg}",
        "must_not_call": f"Calls {arg}, which this case forbids",
        "arg": f"{arg} outside the allowed range",
        "answer": f"Wrong {arg} in the final answer",
        "answer_leak": "Reveals details it must not share",
        "too_many_tool_calls": "Makes more tool calls than allowed",
        "run_failed": f"Run failed ({arg})",
    }.get(kind, violation)
