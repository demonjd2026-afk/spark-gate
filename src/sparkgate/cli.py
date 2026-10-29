"""`sparkgate` command line.

Exit codes: 0 = gate passed, 1 = gate failed, 2 = configuration or input error.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from sparkgate.calibrate import calibrate_judge, run_mutations
from sparkgate.config import ConfigError, GateConfig, load_config
from sparkgate.judge import PairwiseJudge
from sparkgate.pipeline import GatePipeline
from sparkgate.providers import ModelRouter
from sparkgate.report import calibration_markdown, to_markdown
from sparkgate.schemas import GateDecision, RunReport
from sparkgate.store import RunStore
from sparkgate.traces import TraceFileError, load_traces

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console(stderr=True)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to sparkgate.yaml")]


def _load(config: Path, candidates: list[str] | None) -> GateConfig:
    try:
        cfg = load_config(config)
        if candidates:
            cfg = cfg.with_candidates(candidates)
        return cfg
    except (ConfigError, ValidationError) as exc:
        console.print(f"[red]config error:[/] {exc}")
        raise typer.Exit(2) from exc


async def _execute(cfg: GateConfig, traces) -> RunReport:
    router = ModelRouter(cfg)
    try:
        with Progress(
            TextColumn("[bold]{task.description:<28}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as bar:
            tasks: dict[str, int] = {}

            def progress(stage: str, done: int, total: int) -> None:
                if stage not in tasks:
                    tasks[stage] = bar.add_task(stage, total=total)
                bar.update(tasks[stage], completed=done)

            return await GatePipeline(cfg, router, progress).run(traces)
    finally:
        await router.aclose()


def _verdict_table(report: RunReport) -> Table:
    table = Table(title=f"Spark Gate · baseline {report.baseline_model}")
    for col in ("candidate", "verdict", "policy", "fidelity", "schema", "quality loss", "p95 lat",
                "cost"):
        table.add_column(col)
    style = {"ship": "green", "ship_with_fix": "yellow", "dont_ship": "red"}
    originals = {m.model: m for m in report.metrics if m.variant == "original"}
    for v in report.verdicts:
        m = originals[v.candidate_model]
        table.add_row(
            v.candidate_model,
            f"[{style[v.decision.value]}]{v.decision.value}[/]",
            "-" if m.policy_pass_rate is None else f"{m.policy_pass_rate:.0%}",
            f"{m.tool_fidelity_rate:.0%}",
            "-" if m.schema_valid_rate is None else f"{m.schema_valid_rate:.0%}",
            f"{m.quality_loss_rate:.0%}",
            f"{m.latency_p95_ratio:.2f}x",
            f"{m.cost_ratio:.2f}x",
        )
    return table


@app.command()
def run(
    config: ConfigOpt = Path("sparkgate.yaml"),
    candidate: Annotated[
        list[str] | None, typer.Option("--candidate", help="Override candidates (repeatable)")
    ] = None,
    markdown: Annotated[
        Path | None, typer.Option("--markdown", help="Write the PR-comment report here")
    ] = None,
    fail_on: Annotated[
        str, typer.Option(help="Exit 1 when any candidate is: dont_ship | not_ship | never")
    ] = "dont_ship",
    strict: Annotated[
        bool, typer.Option("--strict", help="Fail instead of skipping candidates without keys")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Replay traces across models, diff behavior, and print a ship / don't-ship verdict."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    if fail_on not in {"dont_ship", "not_ship", "never"}:
        console.print("[red]--fail-on must be dont_ship, not_ship or never[/]")
        raise typer.Exit(2)
    cfg = _load(config, candidate)
    try:
        traces = load_traces(cfg.traces)
    except TraceFileError as exc:
        console.print(f"[red]trace error:[/] {exc}")
        raise typer.Exit(2) from exc
    skipped: dict[str, str] = {}
    if not strict:
        try:
            cfg, skipped = cfg.split_available()
        except ConfigError as exc:
            console.print(f"[red]config error:[/] {exc}")
            raise typer.Exit(2) from exc
        for model, reason in skipped.items():
            console.print(f"[yellow]skipping {model}[/] ({reason})")
    try:
        report = asyncio.run(_execute(cfg, traces))
        report.skipped_candidates = skipped
    except ConfigError as exc:
        console.print(f"[red]config error:[/] {exc}")
        raise typer.Exit(2) from exc

    path = RunStore(cfg.store_dir).save(report)
    console.print(_verdict_table(report))
    console.print(f"report: {path}  ·  spend: ${report.total_cost_usd:.4f}")
    md_text = to_markdown(report)
    if markdown:
        markdown.write_text(md_text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md_text)

    decisions = {v.decision for v in report.verdicts}
    failed = (fail_on == "dont_ship" and GateDecision.DONT_SHIP in decisions) or (
        fail_on == "not_ship" and decisions != {GateDecision.SHIP}
    )
    raise typer.Exit(1 if failed else 0)


@app.command()
def calibrate(
    config: ConfigOpt = Path("sparkgate.yaml"),
    judge: Annotated[
        bool, typer.Option("--judge", help="Also audit the judge model (makes API calls)")
    ] = False,
    max_judge_pairs: Annotated[int, typer.Option(min=2, max=500)] = 24,
    markdown: Annotated[Path | None, typer.Option("--markdown")] = None,
    fail_under: Annotated[
        float, typer.Option(min=0, max=1, help="Exit 1 if the seeded-fault detection rate is lower")
    ] = 0.0,
) -> None:
    """Measure the gate itself: seed known faults into recorded runs and count what it catches."""
    cfg = _load(config, None)
    try:
        traces = load_traces(cfg.traces)
    except TraceFileError as exc:
        console.print(f"[red]trace error:[/] {exc}")
        raise typer.Exit(2) from exc

    cal = run_mutations(traces, cfg.gate.side_effect_tools)
    if judge:
        if not cfg.judge.enabled:
            console.print("[red]judge is disabled in the config[/]")
            raise typer.Exit(2)

        async def audit():
            router = ModelRouter(cfg, models={cfg.judge.model})
            try:
                return await calibrate_judge(
                    PairwiseJudge(router, cfg.judge.model), cfg.judge.model, traces,
                    max_judge_pairs,
                )
            finally:
                await router.aclose()

        try:
            cal.judge = asyncio.run(audit())
        except ConfigError as exc:
            console.print(f"[red]config error:[/] {exc}")
            raise typer.Exit(2) from exc

    path = RunStore(cfg.store_dir).save_calibration(cal)
    table = Table(title="Spark Gate calibration")
    for col in ("fault", "claimed", "mutants", "caught", "caught by"):
        table.add_column(col)
    for op in cal.operators:
        table.add_row(op.description, "yes" if op.should_catch else "no", str(op.mutants),
                      "-" if op.rate is None else f"{op.rate:.0%}",
                      ", ".join(f"{k} {v}" for k, v in sorted(op.caught_by.items())))
    console.print(table)
    if cal.detection_rate is not None:
        console.print(f"detection {cal.detection_rate:.0%} · false positives "
                      f"{(cal.false_positive_rate or 0):.0%} · report: {path}")
    text = calibration_markdown(cal)
    if markdown:
        markdown.write_text(text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    rate = cal.detection_rate if cal.detection_rate is not None else 0.0
    raise typer.Exit(1 if rate < fail_under else 0)


@app.command()
def validate(config: ConfigOpt = Path("sparkgate.yaml")) -> None:
    """Check the config and trace file without calling any model."""
    cfg = _load(config, None)
    table = Table(title="Models")
    for col in ("model", "role", "hosting", "served by", "status"):
        table.add_column(col)
    roles: dict[str, list[str]] = {}
    roles.setdefault(cfg.baseline, []).append("baseline")
    for m in cfg.candidates:
        roles.setdefault(m, []).append("candidate")
    if cfg.judge.enabled:
        roles.setdefault(cfg.judge.model, []).append("judge")
    if cfg.fixer.enabled:
        roles.setdefault(cfg.fixer.model, []).append("fixer")
    for m, r in roles.items():
        mc = cfg.models[m]
        reason = cfg.unavailable(m)
        table.add_row(m, ", ".join(r), mc.hosting.replace("_", " "), mc.served_by or "-",
                      f"[yellow]{reason}[/]" if reason else "[green]ready[/]")
    console.print(table)
    try:
        traces = load_traces(cfg.traces)
    except TraceFileError as exc:
        console.print(f"[red]trace error:[/] {exc}")
        raise typer.Exit(2) from exc
    with_schema = sum(t.output_schema is not None for t in traces)
    with_rules = sum(t.expectations is not None for t in traces)
    calls = sum(len(t.tool_calls) for t in traces)
    console.print(
        f"[green]ok[/] · {len(traces)} traces · {calls} recorded tool calls · "
        f"{with_schema} with an output schema · {with_rules} with policy expectations · "
        f"baseline {cfg.baseline} · "
        f"candidates {', '.join(cfg.candidates)}"
    )


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run id, or 'latest'")] = "latest",
    config: ConfigOpt = Path("sparkgate.yaml"),
) -> None:
    """Print a stored run as Markdown."""
    cfg = _load(config, None)
    try:
        rep = RunStore(cfg.store_dir).load(run_id)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    typer.echo(to_markdown(rep))


@app.command()
def history(config: ConfigOpt = Path("sparkgate.yaml"), limit: int = 20) -> None:
    """List past runs."""
    cfg = _load(config, None)
    table = Table(title="Spark Gate runs")
    for col in ("run", "when (UTC)", "baseline", "traces", "spend", "verdicts"):
        table.add_column(col)
    for run_id, created, base, n, cost, verdicts in RunStore(cfg.store_dir).history(limit):
        table.add_row(run_id, f"{created:%Y-%m-%d %H:%M}", base, str(n), f"${cost:.4f}",
                      verdicts or "")
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
