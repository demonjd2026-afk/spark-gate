"""A scripted, offline stand-in for the model router (tests only)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sparkgate.providers import ChatResponse, ToolCallRequest
from sparkgate.schemas import Usage

AgentFn = Callable[[str, list[dict[str, Any]]], tuple[str | None, list[tuple[str, dict]]]]


class ScriptedClient:
    """Routes judge / fixer / agent calls to plain Python functions.

    `agent(model, messages)` returns (final_text, tool_calls); a non-empty tool_calls
    list means "call these tools now".
    """

    def __init__(
        self,
        agent: AgentFn,
        judge: Callable[[str, str], str] | None = None,
        fixer: Callable[[str], str] | None = None,
        cost_per_call: float = 0.001,
        latency_ms: Callable[[str], float] = lambda m: 100.0,
    ) -> None:
        self.agent, self.judge, self.fixer = agent, judge, fixer
        self.cost, self.latency = cost_per_call, latency_ms
        self.calls: list[tuple[str, str]] = []
        self._spent = 0.0
        self._ids = 0

    @property
    def spent_usd(self) -> float:
        return self._spent

    async def chat(self, model, messages, tools=None, *, max_tokens=None) -> ChatResponse:
        self._spent += self.cost
        usage = Usage(input_tokens=100, output_tokens=20, cost_usd=self.cost)
        system = messages[0]["content"]
        if system.startswith("You are an impartial reviewer"):
            self.calls.append((model, "judge"))
            return ChatResponse(self.judge(model, messages[-1]["content"]), [], usage, 50.0)
        if system.startswith("You diagnose behavior regressions"):
            self.calls.append((model, "fixer"))
            return ChatResponse(self.fixer(messages[-1]["content"]), [], usage, 50.0)
        self.calls.append((model, "agent"))
        text, calls = self.agent(model, messages)
        reqs = []
        for name, args in calls:
            self._ids += 1
            reqs.append(ToolCallRequest(f"call_{self._ids}", name, args, json.dumps(args)))
        return ChatResponse(text, reqs, usage, self.latency(model))


def tool_messages(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m["role"] == "tool")
