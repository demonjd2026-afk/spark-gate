"""Reading and writing trace files (JSON Lines, one `Trace` per line)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from sparkgate.diff import SchemaError, check_schema
from sparkgate.schemas import Trace

MAX_TRACE_FILE_BYTES = 50 * 1024 * 1024
MAX_TRACES = 5_000


class TraceFileError(ValueError):
    pass


def load_traces(path: Path) -> list[Trace]:
    if not path.is_file():
        raise TraceFileError(f"trace file not found: {path}")
    if path.stat().st_size > MAX_TRACE_FILE_BYTES:
        raise TraceFileError(f"trace file exceeds {MAX_TRACE_FILE_BYTES // 2**20} MiB: {path}")

    traces: list[Trace] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                trace = Trace.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise TraceFileError(f"{path}:{lineno}: invalid trace: {exc}") from exc
            if trace.id in seen:
                raise TraceFileError(f"{path}:{lineno}: duplicate trace id {trace.id!r}")
            if trace.output_schema is not None:
                try:
                    check_schema(trace.output_schema)
                except SchemaError as exc:
                    raise TraceFileError(
                        f"{path}:{lineno}: output_schema is not a valid JSON Schema: {exc.message}"
                    ) from exc
            seen.add(trace.id)
            traces.append(trace)
            if len(traces) > MAX_TRACES:
                raise TraceFileError(f"more than {MAX_TRACES} traces in {path}")
    if not traces:
        raise TraceFileError(f"no traces in {path}")
    return traces


def write_traces(path: Path, traces: Iterable[Trace]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for t in traces:
            fh.write(t.model_dump_json(exclude_none=True) + "\n")
            n += 1
    tmp.replace(path)  # atomic on POSIX; a crash never leaves a half-written file
    return n
