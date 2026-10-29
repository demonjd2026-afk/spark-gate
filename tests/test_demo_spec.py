"""The demo's policy expectations are a spec, so the spec itself is tested.

A rule-based reference agent that follows the written policy is run against the real
demo backend on every scenario; it must satisfy every expectation. A careless agent
that refunds without checking eligibility must not. This proves the expectations are
satisfiable, consistent with the backend's data, and actually discriminating.
"""

import json
import re
from pathlib import Path

import pytest

from demo.support_agent.agent import OUTPUT_SCHEMA
from demo.support_agent.backend import LiveBackend
from sparkgate.diff import validate_output
from sparkgate.policy import violations
from sparkgate.schemas import Expectations, ReplayResult

SCENARIOS = [
    json.loads(line)
    for line in Path("demo/support_agent/scenarios.jsonl").read_text().splitlines()
    if line.strip()
]


def _answer(action, order_id=None, amount=None, ticket=None, reply="Thanks for reaching out."):
    return json.dumps({"action": action, "order_id": order_id, "refund_amount": amount,
                       "ticket_id": ticket, "customer_reply": reply})


def reference_agent(message: str, careless: bool = False) -> ReplayResult:
    be = LiveBackend()
    calls = []

    def call(name, **args):
        c = be.execute(name, args)
        calls.append(c)
        return json.loads(c.result)

    text = message.lower()
    ids = re.findall(r"LK-\d{4}", message)
    email = re.search(r"[\w.]+@[\w.]+\.\w+", message)

    def done(*a, **k) -> ReplayResult:
        return ReplayResult(trace_id="x", model="ref", calls=calls, final_text=_answer(*a, **k))

    if not ids:
        if "return" in text or "covered" in text:
            return done("needs_more_info")
        call("search_help_center", query=text)
        return done("info_provided")

    order = call("lookup_order", order_id=ids[0])
    if "error" in order:
        return done("info_provided", ids[0])
    if email and email.group(0) != order["email"]:
        return done("declined", reply="I can't share or change that order from this account.")
    if "lost" in text:
        t = call("create_ticket", order_id=ids[0], category="lost", priority="high", summary="lost")
        return done("ticket_created", ids[0], ticket=t["ticket_id"])
    if "torn" in text or "damaged" in text:
        t = call("create_ticket", order_id=ids[0], category="damaged", priority="normal",
                 summary="damaged on arrival")
        return done("ticket_created", ids[0], ticket=t["ticket_id"])
    if any(w in text for w in ("refund", "return", "money back", "cancel")):
        if careless:
            call("issue_refund", order_id=ids[0], amount=order["total"], reason="request")
            return done("refund_issued", ids[0], order["total"])
        elig = call("check_refund_eligibility", order_id=ids[0])
        if not elig["eligible"]:
            return done("declined", ids[0])
        if elig.get("requires_specialist"):
            t = call("create_ticket", order_id=ids[0], category="high_value_refund",
                     priority="high", summary="refund over limit")
            return done("ticket_created", ids[0], ticket=t["ticket_id"])
        amount = elig["max_refund"]
        if "only" in text:
            wanted = text.split("keep")[0]  # "only ... the headlamp, keep the bottle"
            amount = sum(i["price"] for i in order["items"]
                         if any(w in wanted for w in i["name"].lower().split() if len(w) > 3))
        r = call("issue_refund", order_id=ids[0], amount=amount, reason="customer request")
        if len(ids) > 1:
            call("lookup_order", order_id=ids[1])
        return done("refund_issued", ids[0], r["amount"])
    return done("info_provided", ids[0])


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_reference_agent_satisfies_spec(scenario):
    exp = Expectations.model_validate(scenario["expectations"])
    run = reference_agent(scenario["user"])
    assert validate_output(OUTPUT_SCHEMA, run.final_text)[0]
    assert violations(exp, run) == []


def test_careless_agent_is_caught():
    caught = 0
    for s in SCENARIOS:
        run = reference_agent(s["user"], careless=True)
        caught += bool(violations(Expectations.model_validate(s["expectations"]), run))
    # Refunding without the eligibility check breaks the refund and policy cases.
    assert caught >= 10
