import json

from fakes import ScriptedClient
from test_demo_spec import SCENARIOS, reference_agent

from demo.support_agent.agent import OUTPUT_SCHEMA, SYSTEM_PROMPT
from demo.support_agent.backend import TOOLS
from sparkgate.calibrate import calibrate_judge, run_mutations
from sparkgate.judge import PairwiseJudge
from sparkgate.report import calibration_markdown
from sparkgate.schemas import Trace

SIDE = {"issue_refund": ["reason"], "create_ticket": ["summary"]}


def demo_traces() -> list[Trace]:
    out = []
    for s in SCENARIOS:
        run = reference_agent(s["user"])
        out.append(Trace(id=s["id"], system=SYSTEM_PROMPT, user=s["user"], tools=TOOLS,
                         tool_calls=run.calls, final_text=run.final_text,
                         output_schema=OUTPUT_SCHEMA, expectations=s["expectations"]))
    return out


def test_mutation_suite_on_demo_spec():
    cal = run_mutations(demo_traces(), SIDE)
    ops = {o.operator: o for o in cal.operators}

    assert cal.false_positive_rate == 0.0          # unchanged runs are never flagged
    assert ops["unchanged"].mutants == len(SCENARIOS)
    for name in ("drop_tool", "swap_order", "repeat_side_effect", "side_effect_arg",
                 "broken_json", "answer_field", "forbidden_call"):
        assert ops[name].mutants > 0, name
        assert ops[name].rate == 1.0, (name, ops[name].missed_trace_ids)
    assert cal.detection_rate == 1.0
    # A changed argument on a read-only call is a documented blind spot of the
    # deterministic layer: it is measured and reported, not claimed.
    assert not ops["read_arg"].should_catch
    assert ops["read_arg"].rate is not None and ops["read_arg"].rate < 1.0
    assert "Seeded faults caught: **100%**" in calibration_markdown(cal)


def test_without_side_effect_config_arg_faults_are_partly_missed():
    ops = {o.operator: o for o in run_mutations(demo_traces(), {}).operators}
    assert ops["side_effect_arg"].mutants == 0  # nothing declared, nothing to mutate
    assert ops["repeat_side_effect"].mutants == 0


def _content_judge(model, prompt):
    """Prefers the run whose final answer matches the order id in the customer message."""
    customer = prompt.split("<customer>")[1].split("</customer>")[0]
    def score(run):
        return sum(tok in run for tok in customer.split() if tok.startswith("LK-"))
    a = prompt.split("<run_a>")[1].split("</run_a>")[0].split("Final answer:")[1]
    b = prompt.split("<run_b>")[1].split("</run_b>")[0].split("Final answer:")[1]
    if json.loads(a.strip()) == json.loads(b.strip()):
        return '{"winner": "tie", "reason": "same"}'
    w = "tie" if score(a) == score(b) else ("A" if score(a) > score(b) else "B")
    return json.dumps({"winner": w, "reason": "matches the order in question"})


async def test_judge_calibration_counts():
    traces = demo_traces()
    client = ScriptedClient(lambda *_: ("", []), judge=_content_judge)
    j = await calibrate_judge(PairwiseJudge(client, "judge"), "judge", traces, max_pairs=24)
    assert j.control_pairs == 24 and j.control_ties == 24
    assert j.degraded_pairs > 0
    # The scripted judge is a crude heuristic (order-id overlap), so it is not perfect;
    # the test checks the bookkeeping, not the heuristic.
    assert j.degraded_caught + j.degraded_inverted <= j.degraded_pairs
    assert j.degraded_caught > j.degraded_inverted
    assert j.asked_pairs == j.orders_agreed  # this scripted judge has no position bias
    assert j.cost_usd > 0
