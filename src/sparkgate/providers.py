"""Model access: one async router over every OpenAI-compatible endpoint in the config.

Responsibilities kept here and nowhere else:
  * per-endpoint clients (Meta Model API, plus any host serving open weights);
  * bounded concurrency (one semaphore shared by every call in a run);
  * retries with jittered exponential backoff on transient failures only;
  * token accounting and cost, priced from the config;
  * a hard spend ceiling that stops new calls once the run budget is used.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai
from openai import AsyncOpenAI
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from sparkgate.config import GateConfig, ModelConfig
from sparkgate.schemas import Usage

_TRANSIENT = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)


class BudgetExceeded(RuntimeError):
    """The run's spend ceiling was reached; no further model calls are made."""


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(slots=True)
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCallRequest]
    usage: Usage
    latency_ms: float


class ChatClient(Protocol):
    """Anything that can run one chat-completion turn for a configured model key."""

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse: ...

    @property
    def spent_usd(self) -> float: ...


def parse_arguments(raw: str | None) -> dict[str, Any]:
    """Tool arguments as a dict. Malformed JSON is kept verbatim so the diff can see it."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__unparseable__": raw[:2_000]}
    return value if isinstance(value, dict) else {"__value__": value}


def price(model: ModelConfig, usage: Usage) -> float:
    if not model.priced:
        return 0.0
    assert model.input_per_mtok is not None and model.output_per_mtok is not None
    cached_rate = (
        model.cached_input_per_mtok
        if model.cached_input_per_mtok is not None
        else model.input_per_mtok
    )
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (
        uncached * model.input_per_mtok
        + usage.cached_input_tokens * cached_rate
        + usage.output_tokens * model.output_per_mtok
    ) / 1_000_000


@dataclass
class _Budget:
    limit_usd: float
    spent_usd: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self) -> None:
        async with self.lock:
            if self.spent_usd >= self.limit_usd:
                raise BudgetExceeded(
                    f"run budget of ${self.limit_usd:.2f} reached (spent ${self.spent_usd:.4f})"
                )

    async def add(self, amount: float) -> None:
        async with self.lock:
            self.spent_usd += amount


class ModelRouter:
    """Routes a configured model key to its endpoint and accounts for every call.

    The budget is checked before each call, so a run can overshoot by at most the
    calls already in flight (bounded by `runtime.concurrency`).
    """

    def __init__(self, config: GateConfig) -> None:
        self._cfg = config
        self._rt = config.runtime
        self._sem = asyncio.Semaphore(self._rt.concurrency)
        self._budget = _Budget(self._rt.budget_usd)
        self._clients: dict[str, AsyncOpenAI] = {}
        used_endpoints = {config.models[m].endpoint for m in self._models_in_use()}
        for name in sorted(used_endpoints):
            ep = config.endpoints[name]
            self._clients[name] = AsyncOpenAI(
                base_url=ep.resolve_url(),
                api_key=ep.resolve_key().get_secret_value(),
                timeout=self._rt.request_timeout_s,
                max_retries=0,  # retries are owned by tenacity below, not doubled up
            )

    def _models_in_use(self) -> set[str]:
        c = self._cfg
        used = {c.baseline, *c.candidates}
        if c.judge.enabled:
            used.add(c.judge.model)
        if c.fixer.enabled:
            used.add(c.fixer.model)
        return used

    @property
    def spent_usd(self) -> float:
        return self._budget.spent_usd

    async def aclose(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients.values()))

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        mcfg = self._cfg.models[model]
        client = self._clients[mcfg.endpoint]
        kwargs: dict[str, Any] = {
            "model": mcfg.wire_model or model,
            "messages": messages,
        }
        kwargs[self._rt.max_tokens_param] = max_tokens or self._rt.max_output_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._rt.temperature is not None:
            kwargs["temperature"] = self._rt.temperature
        if self._rt.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._rt.reasoning_effort

        await self._budget.check()
        async with self._sem:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_TRANSIENT),
                wait=wait_random_exponential(multiplier=1, max=30),
                stop=stop_after_attempt(self._rt.max_retries + 1),
                reraise=True,
            ):
                with attempt:
                    started = time.perf_counter()
                    resp = await client.chat.completions.create(**kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000

        usage = _usage_from(resp)
        usage.cost_usd = price(mcfg, usage)
        await self._budget.add(usage.cost_usd)

        if not resp.choices:
            return ChatResponse(content=None, tool_calls=[], usage=usage, latency_ms=latency_ms)
        msg = resp.choices[0].message
        calls = [
            ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                arguments=parse_arguments(tc.function.arguments),
                raw_arguments=tc.function.arguments or "",
            )
            for tc in (msg.tool_calls or [])
            if getattr(tc, "function", None) is not None
        ]
        return ChatResponse(
            content=msg.content, tool_calls=calls, usage=usage, latency_ms=latency_ms
        )


def _usage_from(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    details = getattr(u, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    return Usage(
        input_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        cached_input_tokens=int(cached or 0),
    )
