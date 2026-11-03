"""Protocol client tests against a fake socket and a fake HTTP server."""
import asyncio
import json

import httpx
import pytest

from agent.client import Credentials, Stop, SwitchAgent, SwitchError, register_agent


class FakeWS:
    def __init__(self, frames):
        self.frames = [json.dumps(f) for f in frames]
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def make_agent(tmp_path, handler=None):
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={"result": {}})))
    creds = Credentials(agent_id="a1", api_key="k1", connection_id="c1")
    return SwitchAgent("http://switch", creds, tmp_path / "s.json", http=httpx.AsyncClient(transport=transport))


async def test_ping_is_answered_with_cursor(tmp_path):
    agent = make_agent(tmp_path)
    ws = FakeWS([])
    await agent.handle_frame(ws, json.dumps({"event": "message", "id": 41, "data": {"payload": {}}}))
    await agent.handle_frame(ws, json.dumps({"event": "ping"}))
    assert ws.sent == [{"type": "pong", "cursor": 41}]


async def test_connection_state_sets_generation_and_persists(tmp_path):
    agent = make_agent(tmp_path)
    await agent.handle_frame(FakeWS([]), json.dumps(
        {"event": "connection_state", "data": {"generation": 7, "rooms": ["r1"], "cursor": 3}}))
    assert agent.creds.generation == 7 and agent.rooms == ["r1"] and agent.cursor == 3
    saved = Credentials.load(tmp_path / "s.json")
    assert saved.generation == 7 and saved.cursor == 3
    url = agent.ws_url()
    assert "expected_generation=7" in url and "start_from=3" in url and "connection_id=c1" in url


async def test_taken_over_stops_for_good(tmp_path):
    agent = make_agent(tmp_path)
    with pytest.raises(Stop):
        await agent.handle_frame(FakeWS([]), json.dumps({"event": "evicted", "data": {"code": "taken_over"}}))


async def test_heartbeat_lapse_does_not_stop(tmp_path):
    agent = make_agent(tmp_path)
    await agent.handle_frame(FakeWS([]), json.dumps({"event": "evicted", "data": {"code": "heartbeat_lapsed"}}))


async def test_slow_handler_does_not_block_pongs(tmp_path):
    agent = make_agent(tmp_path)
    started = asyncio.Event()

    async def slow(data):
        started.set()
        await asyncio.sleep(10)

    agent.on("message", slow)
    ws = FakeWS([])
    await agent.handle_frame(ws, json.dumps({"event": "message", "id": 1, "data": {"payload": {}}}))
    await agent.handle_frame(ws, json.dumps({"event": "ping"}))
    assert ws.sent == [{"type": "pong", "cursor": 1}]
    await asyncio.wait_for(started.wait(), 1)
    for t in list(agent._tasks):
        t.cancel()


async def test_op_sends_connection_header_and_unwrapped_args(tmp_path):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["conn"] = request.headers.get("x-switch-connection-id")
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"event_id": "e1"}})

    agent = make_agent(tmp_path, handler)
    res = await agent.op("post_message", body="hi", thread_id="t1")
    assert res == {"event_id": "e1"}
    assert seen == {"path": "/agents/a1/ops/post_message", "conn": "c1", "auth": "Bearer k1",
                    "body": {"body": "hi", "thread_id": "t1"}}


async def test_op_error_raises_with_status(tmp_path):
    agent = make_agent(tmp_path, lambda r: httpx.Response(403, json={"detail": "no"}))
    with pytest.raises(SwitchError) as e:
        await agent.op("update_agent_detail", addressing="anyone")
    assert e.value.status == 403


def test_register_uses_registration_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "a9", "api_key": "secret"})

    creds = register_agent("http://switch", "regkey", "docdesk", "desc",
                           http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert creds.agent_id == "a9" and creds.api_key == "secret"
    assert seen["auth"] == "Bearer regkey"
    assert seen["body"]["integration_profile"]["connection_model"] == "always_on"
    assert "overwrite" not in seen["body"]
