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

from sparkgate.config import ConfigError, GateConfig, load_config
from sparkgate.pipeline import GatePipeline
from sparkgate.providers import ModelRouter
from sparkgate.report import to_markdown
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
    for col in ("candidate", "verdict", "fidelity", "schema", "quality loss", "p95 lat", "cost"):
        table.add_column(col)
    style = {"ship": "green", "ship_with_fix": "yellow", "dont_ship": "red"}
    originals = {m.model: m for m in report.metrics if m.variant == "original"}
    for v in report.verdicts:
        m = originals[v.candidate_model]
        table.add_row(
            v.candidate_model,
            f"[{style[v.decision.value]}]{v.decision.value}[/]",
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
    try:
        report = asyncio.run(_execute(cfg, traces))
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
def validate(config: ConfigOpt = Path("sparkgate.yaml")) -> None:
    """Check the config and trace file without calling any model."""
    cfg = _load(config, None)
    try:
        traces = load_traces(cfg.traces)
    except TraceFileError as exc:
        console.print(f"[red]trace error:[/] {exc}")
        raise typer.Exit(2) from exc
    with_schema = sum(t.output_schema is not None for t in traces)
    calls = sum(len(t.tool_calls) for t in traces)
    console.print(
        f"[green]ok[/] · {len(traces)} traces · {calls} recorded tool calls · "
        f"{with_schema} with an output schema · baseline {cfg.baseline} · "
        f"candidates {', '.join(cfg.candidates)}"
    )
    missing = []
    for name, ep in cfg.endpoints.items():
        if not os.environ.get(ep.api_key_env):
            missing.append(f"{name} ({ep.api_key_env})")
    if missing:
        console.print(f"[yellow]keys not set for:[/] {', '.join(missing)}")


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
