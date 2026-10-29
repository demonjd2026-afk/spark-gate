"""Record production-style traces of the demo support agent on a live model.

    python -m demo.support_agent.record --config sparkgate.yaml \
        --model muse-spark-1.1 --out traces/support_agent.jsonl

Each scenario runs against a fresh in-memory backend; every tool call and its real
result is captured, which is what Spark Gate later replays on other models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from demo.support_agent.agent import OUTPUT_SCHEMA, SYSTEM_PROMPT
from demo.support_agent.backend import TOOLS, LiveBackend
from sparkgate.config import load_config
from sparkgate.engine import run_agent
from sparkgate.providers import ModelRouter
from sparkgate.schemas import ReplayStatus, Trace
from sparkgate.traces import write_traces

SCENARIOS = Path(__file__).with_name("scenarios.jsonl")


async def record(config_path: Path, model: str, out: Path) -> int:
    cfg = load_config(config_path)
    if model not in cfg.models:
        raise SystemExit(f"model {model!r} is not defined in {config_path}")
    # The router only opens clients for models in use, so point the baseline at `model`.
    cfg = cfg.model_copy(update={"baseline": model, "candidates": [model]})
    cfg = cfg.model_copy(update={"judge": cfg.judge.model_copy(update={"enabled": False}),
                                 "fixer": cfg.fixer.model_copy(update={"enabled": False})})
    scenarios = [json.loads(line) for line in SCENARIOS.read_text().splitlines() if line.strip()]
    router = ModelRouter(cfg)

    async def one(s: dict) -> Trace | None:
        result = await run_agent(
            router, model=model, trace_id=s["id"], system=SYSTEM_PROMPT, user=s["user"],
            tools=TOOLS, executor=LiveBackend(), max_turns=cfg.runtime.max_tool_turns,
        )
        if result.status is not ReplayStatus.OK:
            print(f"  skip {s['id']}: {result.status.value} {result.error or ''}", file=sys.stderr)
            return None
        print(f"  ok   {s['id']}: {[c.name for c in result.calls]}", file=sys.stderr)
        return Trace(
            id=s["id"], system=SYSTEM_PROMPT, user=s["user"], tools=TOOLS,
            tool_calls=result.calls, final_text=result.final_text,
            output_schema=OUTPUT_SCHEMA, expectations=s.get("expectations"),
            tags=s.get("tags", []), recorded_model=model,
        )

    try:
        traces = [t for t in await asyncio.gather(*(one(s) for s in scenarios)) if t]
    finally:
        await router.aclose()
    n = write_traces(out, traces)
    print(f"wrote {n}/{len(scenarios)} traces to {out} (spend ${router.spent_usd:.4f})",
          file=sys.stderr)
    return 0 if n else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=Path("sparkgate.yaml"))
    p.add_argument("--model", required=True)
    p.add_argument("--out", type=Path, default=Path("traces/support_agent.jsonl"))
    args = p.parse_args()
    raise SystemExit(asyncio.run(record(args.config, args.model, args.out)))


if __name__ == "__main__":
    main()
