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
from sparkgate.gate import audit_baseline, compute_metrics, decide, evaluate
from sparkgate.judge import PairwiseJudge
from sparkgate.providers import ChatClient
from sparkgate.schemas import (
    CandidateMetrics,
    GateDecision,
    ModelInfo,
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
Key = tuple[str, int]  # (trace id, repeat)
Replays = dict[Key, ReplayResult]


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

    @property
    def _repeats(self) -> range:
        return range(self.cfg.runtime.repeats)

    async def _replay_all(
        self, stage: str, traces: list[Trace], model: str, addendum: str | None, variant: str,
        keys: list[Key] | None = None,
    ) -> Replays:
        """Replay each trace once per repeat (or exactly the given (trace, repeat) keys)."""
        by_id = {t.id: t for t in traces}
        keys = keys if keys is not None else [(t.id, k) for t in traces for k in self._repeats]
        results = await self._tracked(
            stage,
            (
                replay_trace(
                    self.client,
                    by_id[tid],
                    model,
                    max_turns=self.cfg.runtime.max_tool_turns,
                    system_addendum=addendum,
                    variant=variant,
                    repeat=k,
                )
                for tid, k in keys
            ),
        )
        return {(r.trace_id, r.repeat): r for r in results}

    async def _diff_all(
        self, stage: str, traces: list[Trace], baseline: Replays, candidate: Replays,
    ) -> list[TraceDiff]:
        """Diff every candidate replay against the baseline replay with the same repeat."""
        by_id = {t.id: t for t in traces}

        async def one(key: Key) -> TraceDiff:
            t, b, c = by_id[key[0]], baseline[key], candidate[key]
            d = diff_replays(t, b, c, self.cfg.gate.side_effect_tools)
            if self.judge and c.status is ReplayStatus.OK:
                j = await self.judge.compare(t, b, c)
                d.quality, d.quality_reason = j.verdict, j.reason
                d.judge_orders_agreed = j.orders_agreed
                d.regression_codes = regression_codes(d)
            return d

        keys = sorted(k for k in candidate if k in baseline and k[0] in by_id)
        return await self._tracked(stage, (one(k) for k in keys))

    def _metrics(
        self, model: str, variant: str, diffs: list[TraceDiff], baseline: Replays,
        candidate: Replays,
    ) -> CandidateMetrics:
        keys = [(d.trace_id, d.repeat) for d in diffs]
        return compute_metrics(
            model,
            variant,
            diffs,
            [baseline[k] for k in keys],
            [candidate[k] for k in keys],
            priced=self._priced(model),
        )

    @staticmethod
    def _worst_repeat(diffs: list[TraceDiff]) -> dict[str, TraceDiff]:
        """One diff per trace for diagnosis: the regressed repeat if there is one."""
        out: dict[str, TraceDiff] = {}
        for d in diffs:
            cur = out.get(d.trace_id)
            if cur is None or (d.is_regression and not cur.is_regression):
                out[d.trace_id] = d
        return out

    def _priced(self, model: str) -> bool:
        return self.cfg.models[model].priced and self.cfg.models[self.cfg.baseline].priced

    # ------------------------------------------------------------------ stages

    async def _candidate(
        self, model: str, traces: list[Trace], baseline: Replays, replay: Replays,
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
        # The fixer looks at one replay per trace: the repeat where the regression showed.
        diff_by_id = self._worst_repeat(diffs)
        base_by_id = {tid: baseline[(tid, d.repeat)] for tid, d in diff_by_id.items()}
        cand_by_id = {tid: replay[(tid, d.repeat)] for tid, d in diff_by_id.items()}
        targets = fixable[: cfg.fixer.max_patterns]
        diagnosed = await self._tracked(
            f"diagnosing {model}",
            (self.fixer.diagnose(p, by_id, base_by_id, cand_by_id, diff_by_id)
             for p in targets),
        )

        # Re-test each fix on the traces it is meant to repair, with the same repeats.
        for i, p in enumerate(diagnosed):
            if not p.suggested_fix:
                continue
            affected = [by_id[tid] for tid in p.trace_ids]
            keys = [(t.id, k) for t in affected for k in self._repeats if (t.id, k) in baseline]
            retest = await self._replay_all(
                f"re-testing fix {i + 1}", affected, model, p.suggested_fix, f"fix:{p.id}",
                keys=keys,
            )
            retest_diffs = await self._diff_all(
                f"judging fix {i + 1}", affected, baseline, retest
            )
            # A trace counts as repaired only if no repeat regresses after the fix.
            still_bad = {d.trace_id for d in retest_diffs if d.is_regression}
            resolved = sorted({d.trace_id for d in retest_diffs} - still_bad)
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
                replay_trace(self.client, t, m, max_turns=cfg.runtime.max_tool_turns, repeat=k)
                for m in models
                for t in traces
                for k in self._repeats
            ),
        )
        by_model: dict[str, Replays] = {m: {} for m in models}
        for r in replays:
            by_model[r.model][(r.trace_id, r.repeat)] = r

        # A (trace, repeat) pair is usable when its baseline replay finished; a trace is
        # inconclusive when none of its baseline replays did.
        all_base = by_model[cfg.baseline]
        baseline = {k: r for k, r in all_base.items() if r.status is ReplayStatus.OK}
        inconclusive = sorted(
            t.id for t in traces if not any((t.id, k) in baseline for k in self._repeats)
        )
        usable = [t for t in traces if t.id not in inconclusive]
        by_id = {t.id: t for t in traces}

        noise = NoiseFloor(
            model=cfg.baseline,
            traces=len(baseline),
            tool_sequence_drift_rate=(
                sum(sequence_drifted(by_id[tid].tool_calls, r.calls)
                    for (tid, _), r in baseline.items())
                / len(baseline)
                if baseline
                else 0.0
            ),
        )
        first_base: dict[str, ReplayResult] = {}
        for (tid, _), r in sorted(baseline.items()):
            first_base.setdefault(tid, r)

        outcomes = await asyncio.gather(
            *(
                self._candidate(m, usable, baseline,
                                {k: by_model[m][k] for k in baseline if k in by_model[m]})
                for m in cfg.candidates
            )
        )

        report = RunReport(
            run_id=uuid.uuid4().hex[:12],
            baseline_model=cfg.baseline,
            candidate_models=list(cfg.candidates),
            n_traces=len(traces),
            repeats=cfg.runtime.repeats,
            inconclusive_trace_ids=inconclusive,
            noise_floor=noise,
            baseline_policy=audit_baseline(cfg.baseline, usable, first_base),
            model_info={
                m: ModelInfo(hosting=cfg.models[m].hosting, served_by=cfg.models[m].served_by,
                             wire_model=cfg.models[m].wire_model or m)
                for m in models
            },
            replays=list(all_base.values()),
        )
        for o in outcomes:
            report.metrics += o.metrics
            report.diffs += o.diffs
            report.replays += o.replays
            report.patterns += o.patterns
            report.verdicts.append(o.verdict)
        report.total_cost_usd = round(self.client.spent_usd, 6)
        return report
