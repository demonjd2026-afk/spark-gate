import json

import pytest
from fakes import ScriptedClient

from sparkgate.diff import (
    diff_replays,
    extract_json,
    primary_code,
    tool_diff,
    validate_output,
)
from sparkgate.engine import RecordedToolOracle, arg_similarity, run_agent
from sparkgate.schemas import (
    QualityVerdict,
    ReplayResult,
    ReplayStatus,
    ToolCall,
    ToolDef,
    ToolFidelity,
    Trace,
)

SCHEMA = {
    "type": "object",
    "required": ["action"],
    "properties": {"action": {"enum": ["a", "b"]}},
    "additionalProperties": False,
}


# ----------------------------------------------------------------- extract_json


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"action": "a"}', {"action": "a"}),
        ('```json\n{"action": "a"}\n```', {"action": "a"}),
        ('Sure! Here it is: {"action": "b"} hope that helps', {"action": "b"}),
        ("no json here", None),
        ("", None),
    ],
)
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_validate_output_reports_paths():
    ok, errs = validate_output(SCHEMA, '{"action": "z", "extra": 1}')
    assert not ok
    assert any("action" in e for e in errs)
    assert validate_output(SCHEMA, '{"action": "a"}') == (True, [])
    assert validate_output(SCHEMA, "plain prose")[0] is False


# ----------------------------------------------------------------- tool diff


def _calls(*names_args):
    return [ToolCall(name=n, arguments=a) for n, a in names_args]


def test_tool_diff_exact_and_args_changed():
    b = _calls(("lookup", {"id": "1"}), ("refund", {"amount": 10, "id": "1"}))
    c_same = _calls(("lookup", {"id": "1"}), ("refund", {"id": "1", "amount": 10}))
    assert tool_diff(b, c_same)[0] is ToolFidelity.EXACT
    c_args = _calls(("lookup", {"id": "1"}), ("refund", {"amount": 99, "id": "1"}))
    fid, _, _, changes = tool_diff(b, c_args)
    assert fid is ToolFidelity.ARGS_CHANGED
    assert "amount: 10 -> 99" in changes[0]


def test_tool_diff_multiset_missing_extra():
    b = _calls(("lookup", {}), ("check", {}), ("refund", {}))
    c = _calls(("lookup", {}), ("refund", {}), ("search", {}))
    fid, missing, extra, _ = tool_diff(b, c)
    assert fid is ToolFidelity.SEQUENCE_CHANGED
    assert missing == ["check"] and extra == ["search"]


def _trace(schema=SCHEMA):
    return Trace(id="t1", system="s", user="u", output_schema=schema)


def _replay(model, names, text, status=ReplayStatus.OK):
    return ReplayResult(trace_id="t1", model=model, calls=_calls(*[(n, {}) for n in names]),
                        final_text=text, status=status)


def test_regression_codes_priority():
    d = diff_replays(_trace(), _replay("b", ["x", "y"], '{"action":"a"}'),
                     _replay("c", ["x"], "oops"))
    assert d.regression_codes == ["schema_invalid", "dropped_tool:y"]
    assert primary_code(d.regression_codes) == "schema_invalid"


def test_both_invalid_schema_is_not_a_candidate_regression():
    d = diff_replays(_trace(), _replay("b", [], "bad"), _replay("c", [], "also bad"))
    assert d.candidate_schema_valid is False and d.regression_codes == []


def test_failed_candidate_is_error_code():
    d = diff_replays(_trace(), _replay("b", [], '{"action":"a"}'),
                     _replay("c", [], "", ReplayStatus.API_ERROR))
    assert d.regression_codes == ["error:api_error"]
    assert d.quality is QualityVerdict.NOT_JUDGED


# ----------------------------------------------------------------- oracle


def test_oracle_exact_fuzzy_reuse_and_unrecorded():
    rec = [
        ToolCall(name="lookup", arguments={"id": "A", "verbose": True}, result="R-A"),
        ToolCall(name="lookup", arguments={"id": "B"}, result="R-B"),
    ]
    o = RecordedToolOracle(rec)
    assert o.execute("lookup", {"id": "B"}).result == "R-B"           # exact, out of order
    fuzzy = o.execute("lookup", {"id": "A", "verbose": False})        # 1 of 3 pairs shared
    assert fuzzy.matched == "none"                                    # below 0.5 threshold
    again = o.execute("lookup", {"id": "B"})                          # idempotent re-read
    assert again.matched == "exact" and again.result == "R-B"
    close = RecordedToolOracle(rec).execute("lookup", {"verbose": True, "id": "A", "x": 1})
    assert close.matched == "fuzzy" and close.result == "R-A"
    assert json.loads(o.execute("refund", {}).result)["error"] == "tool_unavailable"


def test_arg_similarity_nested():
    assert arg_similarity({}, {}) == 1.0
    assert arg_similarity({"a": {"b": 1}}, {"a": {"b": 1}}) == 1.0
    assert arg_similarity({"a": [1, 2]}, {"a": [1, 3]}) == pytest.approx(1 / 3)


# ----------------------------------------------------------------- agent loop


async def test_run_agent_serves_recorded_results_and_stops():
    def agent(model, messages):
        if sum(m["role"] == "tool" for m in messages) == 0:
            return None, [("lookup", {"id": "A"})]
        assert messages[-1]["content"] == "R-A"
        return '{"action":"a"}', []

    client = ScriptedClient(agent)
    oracle = RecordedToolOracle([ToolCall(name="lookup", arguments={"id": "A"}, result="R-A")])
    res = await run_agent(client, model="m", trace_id="t", system="s", user="u",
                          tools=[ToolDef(name="lookup")], executor=oracle, max_turns=3)
    assert res.status is ReplayStatus.OK
    assert [c.matched for c in res.calls] == ["exact"]
    assert res.final_text == '{"action":"a"}'
    assert res.usage.cost_usd == pytest.approx(0.002)


async def test_run_agent_tool_loop_cap():
    client = ScriptedClient(lambda m, msgs: (None, [("lookup", {"id": "A"})]))
    res = await run_agent(client, model="m", trace_id="t", system="s", user="u",
                          tools=[ToolDef(name="lookup")], executor=RecordedToolOracle([]),
                          max_turns=2)
    assert res.status is ReplayStatus.TOOL_LOOP_EXCEEDED
    assert len(res.calls) == 3


def test_remote_refs_are_never_fetched():
    schema = {"$ref": "https://example.invalid/schema.json"}
    ok, errs = validate_output(schema, '{"a": 1}')
    assert not ok and "could not be resolved" in errs[0]
