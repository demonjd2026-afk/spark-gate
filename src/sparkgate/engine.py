"""The agent loop, shared by recording (live tools) and replay (recorded tools).

Keeping one loop for both paths matters: if recording and replay drove the model
differently, every diff would carry that difference as noise.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import openai

from sparkgate.providers import BudgetExceeded, ChatClient
from sparkgate.schemas import (
    ReplayResult,
    ReplayStatus,
    ToolCall,
    ToolDef,
    Trace,
    Usage,
    canonical_args,
)

log = logging.getLogger(__name__)


class ToolExecutor(Protocol):
    def execute(self, name: str, arguments: dict[str, Any]) -> ToolCall: ...


# --------------------------------------------------------------------------- #
# Recorded-tool oracle
# --------------------------------------------------------------------------- #

_UNRECORDED = json.dumps(
    {"error": "tool_unavailable", "detail": "No recorded result for this call in replay."}
)


def _flatten(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        out: set[str] = set()
        for k, v in value.items():
            out |= _flatten(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    if isinstance(value, list):
        out = set()
        for i, v in enumerate(value):
            out |= _flatten(v, f"{prefix}[{i}]")
        return out
    return {f"{prefix}={json.dumps(value, sort_keys=True, default=str)}"}


def arg_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Jaccard similarity over flattened `path=value` pairs. 1.0 for two empty dicts."""
    fa, fb = _flatten(a), _flatten(b)
    if not fa and not fb:
        return 1.0
    return len(fa & fb) / len(fa | fb)


class RecordedToolOracle:
    """Serves tool results from a trace's recording.

    Matching order for a call (name, args):
      1. an unused recorded call with the same name and identical canonical args;
      2. an unused recorded call with the same name and arg similarity >= threshold
         (the closest one wins; ties go to the earliest);
      3. an already-used recorded call with identical args (idempotent re-reads);
      4. otherwise a deterministic `tool_unavailable` error, so the model must cope
         exactly as it would with a failing tool, and the diff counts it.
    """

    def __init__(self, recorded: list[ToolCall], fuzzy_threshold: float = 0.5) -> None:
        self._recorded = recorded
        self._used = [False] * len(recorded)
        self._threshold = fuzzy_threshold

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        key = canonical_args(arguments)
        same_name = [i for i, r in enumerate(self._recorded) if r.name == name]

        for i in same_name:
            if not self._used[i] and canonical_args(self._recorded[i].arguments) == key:
                return self._serve(i, name, arguments, "exact")

        best, best_score = None, self._threshold
        for i in same_name:
            if self._used[i]:
                continue
            score = arg_similarity(arguments, self._recorded[i].arguments)
            if score >= best_score and (best is None or score > best_score):
                best, best_score = i, score
        if best is not None:
            return self._serve(best, name, arguments, "fuzzy")

        for i in same_name:
            if canonical_args(self._recorded[i].arguments) == key:
                return self._serve(i, name, arguments, "exact")

        return ToolCall(name=name, arguments=arguments, result=_UNRECORDED, matched="none")

    def _serve(self, i: int, name: str, arguments: dict[str, Any], how: str) -> ToolCall:
        self._used[i] = True
        return ToolCall(
            name=name,
            arguments=arguments,
            result=self._recorded[i].result or "",
            matched=how,  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #


async def run_agent(
    client: ChatClient,
    *,
    model: str,
    trace_id: str,
    system: str,
    user: str,
    tools: list[ToolDef],
    executor: ToolExecutor,
    max_turns: int,
    variant: str = "original",
) -> ReplayResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tool_specs = [t.as_openai() for t in tools] or None
    result = ReplayResult(trace_id=trace_id, model=model, variant=variant)
    usage = Usage()

    try:
        for _ in range(max_turns + 1):
            resp = await client.chat(model, messages, tool_specs)
            usage = usage + resp.usage
            result.latency_ms += resp.latency_ms

            if not resp.tool_calls:
                result.final_text = resp.content or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.raw_arguments or "{}"},
                        }
                        for tc in resp.tool_calls
                    ],
                }
            )
            for tc in resp.tool_calls:
                served = executor.execute(tc.name, tc.arguments)
                result.calls.append(served)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": served.result or ""}
                )
        else:
            result.status = ReplayStatus.TOOL_LOOP_EXCEEDED
            result.error = f"model kept calling tools after {max_turns} turns"
    except BudgetExceeded as exc:
        result.status = ReplayStatus.BUDGET_EXCEEDED
        result.error = str(exc)
    except openai.APIError as exc:
        result.status = ReplayStatus.API_ERROR
        result.error = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.warning("replay %s on %s failed: %s", trace_id, model, result.error)

    result.usage = usage
    return result


async def replay_trace(
    client: ChatClient,
    trace: Trace,
    model: str,
    *,
    max_turns: int,
    system_addendum: str | None = None,
    variant: str = "original",
) -> ReplayResult:
    system = trace.system if not system_addendum else f"{trace.system}\n\n{system_addendum}"
    return await run_agent(
        client,
        model=model,
        trace_id=trace.id,
        system=system,
        user=trace.user,
        tools=trace.tools,
        executor=RecordedToolOracle(trace.tool_calls),
        max_turns=max_turns,
        variant=variant,
    )
