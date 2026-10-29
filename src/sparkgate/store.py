"""Run history in DuckDB, plus the full report as JSON for the dashboard and CI artifacts.

All statements are parameterised; no value read from a trace or a model response
is ever interpolated into SQL.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import duckdb

from sparkgate.schemas import CalibrationReport, RunReport

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id        VARCHAR PRIMARY KEY,
        created_at    TIMESTAMP NOT NULL,  -- UTC
        baseline      VARCHAR NOT NULL,
        candidates    VARCHAR[] NOT NULL,
        n_traces      INTEGER NOT NULL,
        total_cost    DOUBLE NOT NULL,
        report_json   VARCHAR NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS metrics (
        run_id VARCHAR, model VARCHAR, variant VARCHAR, traces INTEGER, errors INTEGER,
        tool_fidelity_rate DOUBLE, tool_exact_rate DOUBLE, schema_valid_rate DOUBLE,
        quality_win_rate DOUBLE, quality_loss_rate DOUBLE, latency_p50_ratio DOUBLE,
        latency_p95_ratio DOUBLE, cost_ratio DOUBLE, regressions INTEGER
    )""",
    """
    CREATE TABLE IF NOT EXISTS diffs (
        run_id VARCHAR, trace_id VARCHAR, candidate VARCHAR, variant VARCHAR,
        tool_fidelity VARCHAR, quality VARCHAR, candidate_schema_valid BOOLEAN,
        regression_codes VARCHAR[], candidate_latency_ms DOUBLE, candidate_cost_usd DOUBLE
    )""",
    """
    CREATE TABLE IF NOT EXISTS verdicts (
        run_id VARCHAR, model VARCHAR, decision VARCHAR, summary VARCHAR
    )""",
)


class RunStore:
    def __init__(self, store_dir: Path) -> None:
        self.dir = store_dir
        self.reports_dir = store_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = store_dir / "sparkgate.duckdb"
        with duckdb.connect(str(self.db_path)) as con:
            for stmt in _DDL:
                con.execute(stmt)

    def save(self, report: RunReport) -> Path:
        payload = report.model_dump_json()
        json_path = self.reports_dir / f"{report.run_id}.json"
        json_path.write_text(payload, encoding="utf-8")
        (self.reports_dir / "latest.json").write_text(payload, encoding="utf-8")

        with duckdb.connect(str(self.db_path)) as con:
            con.execute("BEGIN")
            con.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                [report.run_id, report.created_at.astimezone(UTC).replace(tzinfo=None),
                 report.baseline_model,
                 report.candidate_models, report.n_traces, report.total_cost_usd, payload],
            )
            if report.metrics:
                con.executemany(
                    "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        [report.run_id, m.model, m.variant, m.traces, m.errors,
                         m.tool_fidelity_rate, m.tool_exact_rate, m.schema_valid_rate,
                         m.quality_win_rate, m.quality_loss_rate, m.latency_p50_ratio,
                         m.latency_p95_ratio, m.cost_ratio, m.regressions]
                        for m in report.metrics
                    ],
                )
            if report.diffs:
                con.executemany(
                    "INSERT INTO diffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        [report.run_id, d.trace_id, d.candidate_model, d.variant,
                         d.tool_fidelity.value, d.quality.value, d.candidate_schema_valid,
                         d.regression_codes, d.candidate_latency_ms, d.candidate_cost_usd]
                        for d in report.diffs
                    ],
                )
            if report.verdicts:
                con.executemany(
                    "INSERT INTO verdicts VALUES (?, ?, ?, ?)",
                    [[report.run_id, v.candidate_model, v.decision.value, v.summary]
                     for v in report.verdicts],
                )
            con.execute("COMMIT")
        return json_path

    def history(self, limit: int = 20) -> list[tuple]:
        with duckdb.connect(str(self.db_path), read_only=True) as con:
            return con.execute(
                """
                SELECT r.run_id, r.created_at, r.baseline, r.n_traces, r.total_cost,
                       string_agg(v.model || '=' || v.decision, ', ' ORDER BY v.model)
                FROM runs r LEFT JOIN verdicts v USING (run_id)
                GROUP BY ALL ORDER BY r.created_at DESC LIMIT ?
                """,
                [limit],
            ).fetchall()

    def load(self, run_id: str) -> RunReport:
        if run_id == "latest":
            path = self.reports_dir / "latest.json"
        else:
            if not run_id.isalnum():
                raise ValueError("run id must be alphanumeric")
            path = self.reports_dir / f"{run_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no report for run {run_id!r} in {self.reports_dir}")
        return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))

    # Calibration reports live next to run reports, prefixed so they never mix.
    def save_calibration(self, report: CalibrationReport) -> Path:
        payload = report.model_dump_json()
        path = self.reports_dir / f"calibration-{report.calibration_id}.json"
        path.write_text(payload, encoding="utf-8")
        (self.reports_dir / "calibration-latest.json").write_text(payload, encoding="utf-8")
        return path

    def load_calibration(self) -> CalibrationReport:
        path = self.reports_dir / "calibration-latest.json"
        if not path.is_file():
            raise FileNotFoundError(f"no calibration report in {self.reports_dir}")
        return CalibrationReport.model_validate_json(path.read_text(encoding="utf-8"))
