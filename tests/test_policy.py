import pytest

from sparkgate.diff import diff_replays, primary_code
from sparkgate.policy import check_answer, check_calls, describe, violations
from sparkgate.schemas import Expectations, ReplayResult, ReplayStatus, ToolCall, Trace


def _calls(*pairs):
    return [ToolCall(name=n, arguments=a) for n, a in pairs]


EXP = Expectations(
    must_call=["lookup", "check"],
    must_not_call=["delete"],
    call_before=[("check", "refund")],
    args={"refund.amount": {"max": 50}, "ticket.priority": {"one_of": ["high"]}},
    answer={"action": ["refund_issued"], "meta.ok": [True]},
    answer_must_not_contain=["secret-sku"],
    max_tool_calls=4,
)


def test_clean_run_has_no_violations():
    calls = _calls(("lookup", {}), ("check", {}), ("refund", {"amount": 50}))
    assert check_calls(EXP, calls) == []
    assert check_answer(EXP, '{"action": "refund_issued", "meta": {"ok": true}}') == []


def test_each_rule_fires():
    calls = _calls(("refund", {"amount": 80}), ("delete", {}), ("check", {}),
                   ("ticket", {"priority": "low"}), ("lookup", {}))
    v = check_calls(EXP, calls)
    assert "call_before:check>refund" in v
    assert "must_not_call:delete" in v
    assert "arg:refund.amount" in v
    assert "arg:ticket.priority" in v
    assert "too_many_tool_calls" in v
    assert check_calls(EXP, _calls(("lookup", {})))[0] == "must_call:check"


def test_answer_rules():
    assert check_answer(EXP, "not json") == ["answer:action", "answer:meta.ok"]
    assert "answer:meta.ok" in check_answer(EXP, '{"action":"refund_issued","meta":{"ok":1}}')
    assert check_answer(EXP, '{"action":"refund_issued","meta":{"ok":true},"x":"SECRET-SKU"}') \
        == ["answer_leak:0"]


def test_numeric_rule_rejects_non_numbers():
    exp = Expectations(args={"refund.amount": {"max": 50}})
    assert check_calls(exp, _calls(("refund", {"amount": "40"}))) == ["arg:refund.amount"]
    assert check_calls(exp, _calls(("refund", {"amount": True}))) == ["arg:refund.amount"]


def test_bad_arg_key_rejected():
    with pytest.raises(ValueError):
        Expectations(args={"amount": {"max": 1}})


def test_failed_run_and_no_expectations():
    run = ReplayResult(trace_id="t", model="m", status=ReplayStatus.API_ERROR)
    assert violations(EXP, run) == ["run_failed:api_error"]
    assert violations(None, run) is None


def test_describe_is_readable():
    assert describe("call_before:check>refund") == "Calls refund without calling check first"
    assert describe("must_not_call:issue_refund").startswith("Calls issue_refund")


def _run(model, calls, text):
    return ReplayResult(trace_id="t", model=model, calls=_calls(*calls), final_text=text)


def test_policy_regression_only_counts_new_violations():
    trace = Trace(id="t", system="s", user="u",
                  expectations=Expectations(must_not_call=["refund"], must_call=["check"]))
    base = _run("b", [("lookup", {})], "{}")                      # already misses `check`
    cand = _run("c", [("lookup", {}), ("refund", {})], "{}")    # also refunds: new violation
    d = diff_replays(trace, base, cand)
    assert d.baseline_policy_violations == ["must_call:check"]
    assert d.regression_codes[0] == "policy:must_not_call:refund"
    assert "policy:must_call:check" not in d.regression_codes
    assert primary_code(d.regression_codes) == "policy:must_not_call:refund"


def test_side_effect_arg_change_is_a_regression():
    trace = Trace(id="t", system="s", user="u")
    base = _run("b", [("refund", {"amount": 10, "reason": "a"})], "{}")
    cand = _run("c", [("refund", {"amount": 99, "reason": "b"})], "{}")
    assert diff_replays(trace, base, cand).regression_codes == []
    d = diff_replays(trace, base, cand, {"refund": ["reason"]})
    assert d.regression_codes == ["args_changed:refund"]
