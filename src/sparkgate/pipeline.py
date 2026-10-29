"""End-to-end gate run: replay -> diff -> judge -> gate -> diagnose -> fix -> re-gate."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

from sparkgate.config import GateConfig
from sparkgate.diff import diff_replays, regression_codes, sequence_drifted
from sparkgate.engine import replay_trace
from sparkgate.fixer import PromptFixer, combined_addendum, group_patterns
from sparkgate.gate import compute_metrics, decide, evaluate
from sparkgate.judge import PairwiseJudge
from sparkgate.providers import ChatClient
from sparkgate.schemas import (
    CandidateMetrics,
    GateDecision,
    NoiseFloor,
    RegressionPattern,
    ReplayResult,
    ReplayStatus,
    RunReport,
    Trace,
    TraceDiff,
    Verdict,
)

ProgressFn = Callable[[str, int, int], None]
T = TypeVar("T")


def _noop(stage: str, done: int, total: int) -> None:  # pragma: no cover - trivial
    return None


@dataclass
class _CandidateOutcome:
    metrics: list[CandidateMetrics]
    diffs: list[TraceDiff]
    replays: list[ReplayResult]
    patterns: list[RegressionPattern]
    verdict: Verdict


class GatePipeline:
    def __init__(
        self, cfg: GateConfig, client: ChatClient, progress: ProgressFn | None = None
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.progress = progress or _noop
        self.judge = PairwiseJudge(client, cfg.judge.model) if cfg.judge.enabled else None
        self.fixer = (
            PromptFixer(client, cfg.fixer.model, cfg.fixer.examples_per_pattern)
            if cfg.fixer.enabled and cfg.fixer.max_patterns > 0
            else None
        )

    # ------------------------------------------------------------------ helpers

    async def _tracked(self, stage: str, coros: Iterable[Awaitable[T]]) -> list[T]:
        coros = list(coros)
        total, done = len(coros), 0
        self.progress(stage, 0, total)

        async def wrap(c: Awaitable[T]) -> T:
            nonlocal done
            out = await c
            done += 1
            self.progress(stage, done, total)
            return out

        return await asyncio.gather(*(wrap(c) for c in coros))

    async def _replay_all(
        self, stage: str, traces: list[Trace], model: str, addendum: str | None, variant: str
    ) -> dict[str, ReplayResult]:
        results = await self._tracked(
            stage,
            (
                replay_trace(
                    self.client,
                    t,
                    model,
                    max_turns=self.cfg.runtime.max_tool_turns,
                    system_addendum=addendum,
                    variant=variant,
                )
                for t in traces
            ),
        )
        return {r.trace_id: r for r in results}

    async def _diff_all(
        self,
        stage: str,
        traces: list[Trace],
        baseline: dict[str, ReplayResult],
        candidate: dict[str, ReplayResult],
    ) -> list[TraceDiff]:
        async def one(t: Trace) -> TraceDiff:
            b, c = baseline[t.id], candidate[t.id]
            d = diff_replays(t, b, c)
            if self.judge and c.status is ReplayStatus.OK:
                j = await self.judge.compare(t, b, c)
                d.quality, d.quality_reason = j.verdict, j.reason
                d.regression_codes = regression_codes(d)
            return d

        return await self._tracked(stage, (one(t) for t in traces))

    def _metrics(
        self,
        model: str,
        variant: str,
        diffs: list[TraceDiff],
        baseline: dict[str, ReplayResult],
        candidate: dict[str, ReplayResult],
    ) -> CandidateMetrics:
        ids = [d.trace_id for d in diffs]
        return compute_metrics(
            model,
            variant,
            diffs,
            [baseline[i] for i in ids],
            [candidate[i] for i in ids],
            priced=self._priced(model),
        )

    def _priced(self, model: str) -> bool:
        return self.cfg.models[model].priced and self.cfg.models[self.cfg.baseline].priced

    # ------------------------------------------------------------------ stages

    async def _candidate(
        self, model: str, traces: list[Trace], baseline: dict[str, ReplayResult],
        replay: dict[str, ReplayResult],
    ) -> _CandidateOutcome:
        cfg = self.cfg
        diffs = await self._diff_all(f"judging {model}", traces, baseline, replay)
        metrics = self._metrics(model, "original", diffs, baseline, replay)
        checks = evaluate(metrics, cfg.gate, judged=self.judge is not None,
                          priced=self._priced(model))
        verdict = decide(model, checks)
        patterns = group_patterns(diffs)
        out = _CandidateOutcome([metrics], diffs, list(replay.values()), patterns, verdict)

        fixable = [p for p in patterns if not p.code.startswith("error:")]
        if verdict.decision is GateDecision.SHIP or not self.fixer or not fixable:
            return out

        by_id = {t.id: t for t in traces}
        diff_by_id = {d.trace_id: d for d in diffs}
        targets = fixable[: cfg.fixer.max_patterns]
        diagnosed = await self._tracked(
            f"diagnosing {model}",
            (self.fixer.diagnose(p, by_id, baseline, replay, diff_by_id) for p in targets),
        )

        # Re-test each fix on the traces it is meant to repair.
        for i, p in enumerate(diagnosed):
            if not p.suggested_fix:
                continue
            affected = [by_id[tid] for tid in p.trace_ids]
            retest = await self._replay_all(
                f"re-testing fix {i + 1}", affected, model, p.suggested_fix, f"fix:{p.id}"
            )
            retest_diffs = await self._diff_all(
                f"judging fix {i + 1}", affected, baseline, retest
            )
            resolved = sorted(d.trace_id for d in retest_diffs if not d.is_regression)
            diagnosed[i] = p.model_copy(
                update={"fix_resolved_ids": resolved, "fix_accepted": bool(resolved)}
            )
            out.replays += list(retest.values())

        diagnosed_ids = {p.id for p in diagnosed}
        out.patterns = diagnosed + [p for p in patterns if p.id not in diagnosed_ids]

        # Verify the accepted fixes *together* on every trace: a fix that repairs one
        # pattern but breaks other traces must not let the candidate through.
        addendum = combined_addendum(diagnosed)
        if addendum:
            patched = await self._replay_all(
                f"verifying patched {model}", traces, model, addendum, "patched"
            )
            p_diffs = await self._diff_all(f"judging patched {model}", traces, baseline, patched)
            p_metrics = self._metrics(model, "patched", p_diffs, baseline, patched)
            p_checks = evaluate(p_metrics, cfg.gate, judged=self.judge is not None,
                                priced=self._priced(model))
            out.verdict = decide(model, checks, p_checks, addendum)
            out.metrics.append(p_metrics)
            out.diffs += p_diffs
            out.replays += list(patched.values())
        return out

    async def run(self, traces: list[Trace]) -> RunReport:
        cfg = self.cfg
        models = [cfg.baseline, *cfg.candidates]
        replays = await self._tracked(
            "replaying",
            (
                replay_trace(self.client, t, m, max_turns=cfg.runtime.max_tool_turns)
                for m in models
                for t in traces
            ),
        )
        by_model: dict[str, dict[str, ReplayResult]] = {m: {} for m in models}
        for r in replays:
            by_model[r.model][r.trace_id] = r

        baseline = by_model[cfg.baseline]
        inconclusive = sorted(tid for tid, r in baseline.items() if r.status is not ReplayStatus.OK)
        usable = [t for t in traces if t.id not in inconclusive]

        noise = NoiseFloor(
            model=cfg.baseline,
            traces=len(usable),
            tool_sequence_drift_rate=(
                sum(sequence_drifted(t.tool_calls, baseline[t.id].calls) for t in usable)
                / len(usable)
                if usable
                else 0.0
            ),
        )

        outcomes = await asyncio.gather(
            *(
                self._candidate(m, usable, baseline, {t.id: by_model[m][t.id] for t in usable})
                for m in cfg.candidates
            )
        )

        report = RunReport(
            run_id=uuid.uuid4().hex[:12],
            baseline_model=cfg.baseline,
            candidate_models=list(cfg.candidates),
            n_traces=len(traces),
            inconclusive_trace_ids=inconclusive,
            noise_floor=noise,
            replays=list(baseline.values()),
        )
        for o in outcomes:
            report.metrics += o.metrics
            report.diffs += o.diffs
            report.replays += o.replays
            report.patterns += o.patterns
            report.verdicts.append(o.verdict)
        report.total_cost_usd = round(self.client.spent_usd, 6)
        return report
