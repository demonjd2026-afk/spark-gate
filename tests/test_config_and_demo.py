import json

import pytest
from pydantic import ValidationError

from demo.support_agent.agent import OUTPUT_SCHEMA
from demo.support_agent.backend import LiveBackend
from sparkgate.config import ConfigError, EndpointConfig, GateConfig
from sparkgate.diff import check_schema


def test_endpoint_requires_https_except_localhost(monkeypatch):
    assert EndpointConfig(base_url="https://api.meta.ai/v1/", api_key_env="K").resolve_url() \
        == "https://api.meta.ai/v1"
    assert EndpointConfig(base_url="http://localhost:8000/v1", api_key_env="K").resolve_url()
    with pytest.raises(ConfigError):
        EndpointConfig(base_url="http://evil.example/v1", api_key_env="K").resolve_url()
    with pytest.raises(ConfigError):
        EndpointConfig(base_url="https://x/v1", api_key_env="UNSET_KEY_XYZ").resolve_key()
    monkeypatch.setenv("K", "secret")
    key = EndpointConfig(base_url="https://x/v1", api_key_env="K").resolve_key()
    assert "secret" not in repr(key)


def test_config_rejects_dangling_references():
    base = {
        "baseline": "a", "candidates": ["b"], "traces": "t.jsonl",
        "endpoints": {"e": {"base_url": "https://x/v1", "api_key_env": "K"}},
        "models": {"a": {"endpoint": "e"}, "b": {"endpoint": "e"}},
        "judge": {"model": "a"}, "fixer": {"model": "a"},
    }
    GateConfig.model_validate(base)
    with pytest.raises(ValidationError, match="not defined"):
        GateConfig.model_validate({**base, "candidates": ["zzz"]})
    with pytest.raises(ValidationError, match="undefined endpoints"):
        GateConfig.model_validate({**base, "models": {**base["models"], "b": {"endpoint": "q"}}})
    with pytest.raises(ValidationError, match="baseline"):
        GateConfig.model_validate({**base, "candidates": ["a"]})


def test_demo_schema_is_valid():
    check_schema(OUTPUT_SCHEMA)


def _run(backend, name, **args):
    return json.loads(backend.execute(name, args).result)


def test_backend_enforces_refund_rules():
    b = LiveBackend()
    assert _run(b, "check_refund_eligibility", order_id="LK-1002")["reason"] == \
        "outside_30_day_window"
    assert _run(b, "check_refund_eligibility", order_id="LK-1003")["reason"] == "final_sale"
    big = _run(b, "check_refund_eligibility", order_id="LK-1005")
    assert big["eligible"] and big["requires_specialist"]
    assert _run(b, "issue_refund", order_id="LK-1005", amount=289, reason="x")["reason"] == \
        "requires_specialist_approval"
    assert _run(b, "issue_refund", order_id="LK-1001", amount=250, reason="x")["reason"] == \
        "amount_exceeds_max_refund"
    ok = _run(b, "issue_refund", order_id="LK-1001", amount=189, reason="x")
    assert ok["refund_id"].startswith("RF-")
    assert _run(b, "issue_refund", order_id="LK-1001", amount=1, reason="x")["reason"] == \
        "already_refunded"
    assert _run(b, "lookup_order", order_id=123)["error"] == "order_not_found"
    assert _run(b, "drop_tables")["error"] == "unknown_tool"
