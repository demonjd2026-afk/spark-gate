"""ModelRouter against a local OpenAI-compatible HTTP server (real SDK, real wire format)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from sparkgate.config import GateConfig
from sparkgate.providers import BudgetExceeded, ModelRouter


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict]] = []
    fail_next = 0

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"auth": self.headers.get("Authorization"), **body})
        if type(self).fail_next:
            type(self).fail_next -= 1
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"slow down"}}')
            return
        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "lookup_order", "arguments": '{"order_id":"LK-1001"}'}}]}
        if body["messages"][-1]["role"] == "tool":
            msg = {"role": "assistant", "content": '{"action":"info_provided"}'}
        payload = {
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
                      "total_tokens": 2_000_000,
                      "prompt_tokens_details": {"cached_tokens": 500_000}},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server():
    _Handler.seen, _Handler.fail_next = [], 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def _cfg(url: str, budget: float = 100) -> GateConfig:
    return GateConfig.model_validate({
        "baseline": "a", "candidates": ["b"], "traces": "t.jsonl",
        "endpoints": {"local": {"base_url": url, "api_key_env": "SG_TEST_KEY"}},
        "models": {
            "a": {"endpoint": "local", "wire_model": "vendor/a", "input_per_mtok": 1.25,
                  "output_per_mtok": 4.25, "cached_input_per_mtok": 0.15},
            "b": {"endpoint": "local"},
        },
        "judge": {"enabled": False, "model": "a"}, "fixer": {"enabled": False, "model": "a"},
        "runtime": {"budget_usd": budget, "max_retries": 2, "temperature": 0},
    })


async def test_router_wire_format_cost_and_retry(server, monkeypatch):
    monkeypatch.setenv("SG_TEST_KEY", "sk-test")
    _Handler.fail_next = 1  # first attempt is rate-limited, retry must succeed
    router = ModelRouter(_cfg(server))
    tools = [{"type": "function", "function": {"name": "lookup_order", "parameters": {}}}]
    resp = await router.chat("a", [{"role": "user", "content": "hi"}], tools)
    await router.aclose()

    assert len(_Handler.seen) == 2
    req = _Handler.seen[-1]
    assert req["model"] == "vendor/a" and req["auth"] == "Bearer sk-test"
    assert req["tool_choice"] == "auto" and req["temperature"] == 0
    assert resp.tool_calls[0].name == "lookup_order"
    assert resp.tool_calls[0].arguments == {"order_id": "LK-1001"}
    # 0.5M uncached * 1.25 + 0.5M cached * 0.15 + 1M out * 4.25
    assert resp.usage.cost_usd == pytest.approx(0.625 + 0.075 + 4.25)
    assert router.spent_usd == pytest.approx(resp.usage.cost_usd)


async def test_router_budget_ceiling(server, monkeypatch):
    monkeypatch.setenv("SG_TEST_KEY", "sk-test")
    router = ModelRouter(_cfg(server, budget=1))
    await router.chat("a", [{"role": "user", "content": "hi"}])
    with pytest.raises(BudgetExceeded):
        await router.chat("a", [{"role": "user", "content": "again"}])
    await router.aclose()
