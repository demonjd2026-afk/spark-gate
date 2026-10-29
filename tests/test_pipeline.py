"""End-to-end gate run on scripted models: no network, real pipeline, store and report."""

import json
from pathlib import Path

import pytest
from fakes import ScriptedClient, tool_messages

from sparkgate.config import GateConfig, load_config
from sparkgate.pipeline import GatePipeline
from sparkgate.report import to_markdown
from sparkgate.schemas import GateDecision, ToolCall, ToolDef, Trace
from sparkgate.store import RunStore

SCHEMA = {"type": "object", "required": ["action"],
          "properties": {"action": {"enum": ["refund_issued", "declined"]}}}
FIX = "Always call check_refund_eligibility before issue_refund."


def _traces(n: int = 6) -> list[Trace]:
    out = []
    for i in range(n):
        oid = f"LK-{1000 + i}"
        out.append(Trace(
            id=f"refund-{i}", system="You are support.", user=f"refund {oid}",
            tools=[ToolDef(name=t) for t in ("lookup_order", "check_refund_eligibility",
                                             "issue_refund")],
            tool_calls=[
                ToolCall(name="lookup_order", arguments={"order_id": oid}, result='{"ok":1}'),
                ToolCall(name="check_refund_eligibility", arguments={"order_id": oid},
                         result='{"eligible":true,"max_refund":50}'),
                ToolCall(name="issue_refund", arguments={"order_id": oid, "amount": 50},
                         result='{"refund_id":"RF-1"}'),
            ],
            final_text='{"action":"refund_issued"}', output_schema=SCHEMA,
        ))
    return out


def agent(model: str, messages: list[dict]):
    oid = messages[1]["content"].split()[-1]
    careful = model in {"base", "good"} or FIX in messages[0]["content"]
    plan = (["lookup_order", "check_refund_eligibility", "issue_refund"] if careful
            else ["lookup_order", "issue_refund"])
    step = tool_messages(messages)
    if step < len(plan):
        args = {"order_id": oid} if plan[step] != "issue_refund" else {"order_id": oid, "amount": 50}
        return None, [(plan[step], args)]
    if model == "sloppy" and FIX not in messages[0]["content"]:
        return "Refund done!", []  # also breaks the output contract
    return '{"action":"refund_issued"}', []


def judge(model: str, prompt: str) -> str:
    a = prompt.split("<run_a>")[1].split("</run_a>")[0]
    b = prompt.split("<run_b>")[1].split("</run_b>")[0]
    a_ok, b_ok = "check_refund_eligibility" in a, "check_refund_eligibility" in b
    winner = "tie" if a_ok == b_ok else ("A" if a_ok else "B")
    return json.dumps({"winner": winner, "reason": "skipped the eligibility check"})


def fixer(prompt: str) -> str:
    assert "dropped_tool:check_refund_eligibility" in prompt or "schema_invalid" in prompt
    return json.dumps({"title": "Skips eligibility check", "description": "Refunds without "
                       "checking policy.", "addendum": FIX, "rationale": "Restores the check."})


@pytest.fixture
def cfg(tmp_path: Path) -> GateConfig:
    (tmp_path / "t.jsonl").write_text("")
    raw = {
        "baseline": "base", "candidates": ["good", "sloppy"], "traces": "t.jsonl",
        "endpoints": {"e": {"base_url": "https://example.invalid/v1", "api_key_env": "K"}},
        "models": {m: {"endpoint": "e", "input_per_mtok": 1, "output_per_mtok": 1}
                   for m in ("base", "good", "sloppy", "judge")},
        "judge": {"model": "judge"}, "fixer": {"model": "judge"},
        "store_dir": "store",
    }
    path = tmp_path / "sparkgate.yaml"
    path.write_text(json.dumps(raw))  # JSON is valid YAML
    return load_config(path)


async def test_full_run_ship_and_ship_with_fix(cfg, tmp_path):
    client = ScriptedClient(agent, judge=judge, fixer=fixer)
    report = await GatePipeline(cfg, client).run(_traces())

    verdicts = {v.candidate_model: v for v in report.verdicts}
    assert verdicts["good"].decision is GateDecision.SHIP
    sloppy = verdicts["sloppy"]
    assert sloppy.decision is GateDecision.SHIP_WITH_FIX
    assert sloppy.prompt_addendum == FIX
    assert not {c.name for c in sloppy.checks if not c.passed} - {
        "tool-call fidelity", "schema validity", "quality loss rate"}

    pat = next(p for p in report.patterns if p.candidate_model == "sloppy")
    assert pat.code == "schema_invalid"  # schema outranks the dropped tool
    assert pat.fix_accepted and len(pat.fix_resolved_ids) == 6

    assert report.noise_floor.tool_sequence_drift_rate == 0.0
    assert report.total_cost_usd > 0

    store = RunStore(cfg.store_dir)
    store.save(report)
    assert store.load("latest").run_id == report.run_id
    assert store.history()[0][0] == report.run_id
    md = to_markdown(report)
    assert "SHIP WITH FIX" in md and FIX in md


async def test_budget_exhaustion_is_reported_not_raised(cfg):
    from sparkgate.providers import BudgetExceeded

    class Broke(ScriptedClient):
        async def chat(self, *a, **k):
            raise BudgetExceeded("out of budget")

    report = await GatePipeline(cfg, Broke(agent)).run(_traces(2))
    assert report.inconclusive_trace_ids == ["refund-0", "refund-1"]
    # No usable evidence must never produce a SHIP.
    assert {v.decision for v in report.verdicts} == {GateDecision.DONT_SHIP}
