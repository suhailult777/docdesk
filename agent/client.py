"""A small client for the Switch agent protocol.

Switch has no client library, so this module implements the wire contract
from docs.switchagents.ai (internals/agent-protocol, agent-connection):

* register once with ``POST /agents`` and a registration key, keep the
  returned API key on disk
* open ``/agents/{id}/connection/ws`` with a connection id we reuse forever
* answer every ``ping`` with ``{"type": "pong", "cursor": N}`` before doing
  anything else (6 s without a pong and the server evicts us)
* resume after a drop with ``start_from`` and ``expected_generation``
* stop for good on ``taken_over``
* call operations with ``POST /agents/{id}/ops/{op}`` and the
  ``X-Switch-Connection-Id`` header
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import websockets

log = logging.getLogger("docdesk.client")

# Eviction codes after which reconnecting is wrong (agent-connection docs).
FINAL_EVICTIONS = {"taken_over"}
# Close codes that mean "do not retry": bad request, bad auth, taken over.
FINAL_CLOSE_CODES = {4400, 4401, 4403, 4409}


class SwitchError(RuntimeError):
    def __init__(self, status: int, detail: str, op: str | None = None):
        self.status = status
        self.detail = detail
        self.op = op
        super().__init__(f"{op or 'request'} failed with {status}: {detail}")


class Stop(Exception):
    """Raised inside the run loop when the connection must not be reopened."""


@dataclass
class Credentials:
    agent_id: str
    api_key: str
    connection_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    generation: int | None = None
    cursor: int | None = None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Credentials | None":
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})


def integration_profile() -> dict[str, Any]:
    return {
        "connection_model": "always_on",
        "message_exchange": True,
        "pre_invocation_mediation": [],
        "post_invocation_mediation": [],
        "event_reporting": [],
    }


def register_agent(
    base_url: str,
    registration_key: str,
    name: str,
    description: str,
    display_name: str | None = None,
    overwrite: bool = False,
    http: httpx.Client | None = None,
) -> Credentials:
    """Register an agent once. The API key in the answer is shown only once."""
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "connector_type": "docdesk-python",
        "integration_profile": integration_profile(),
    }
    if display_name:
        body["display_name"] = display_name
    if overwrite:
        body["overwrite"] = True
    c = http or httpx.Client(timeout=30)
    r = c.post(
        f"{base_url}/agents",
        json=body,
        headers={"Authorization": f"Bearer {registration_key}"},
    )
    if r.status_code >= 400:
        raise SwitchError(r.status_code, r.text[:300], "register")
    data = r.json()
    return Credentials(agent_id=data["id"], api_key=data["api_key"])


def load_or_register(
    state_path: Path,
    base_url: str,
    registration_key: str | None,
    name: str,
    description: str,
    display_name: str | None = None,
) -> Credentials:
    """Keep the registration across launches: registering again is a 409."""
    creds = Credentials.load(state_path)
    if creds:
        return creds
    if not registration_key:
        raise SystemExit(
            "No saved agent key and no SWITCH_REGISTRATION_KEY. "
            "Create a registration key on the Gateway API Keys page."
        )
    creds = register_agent(base_url, registration_key, name, description, display_name)
    creds.save(state_path)
    log.info("registered agent %s (%s)", name, creds.agent_id)
    return creds


Handler = Callable[[dict[str, Any]], Awaitable[None]]


class SwitchAgent:
    """One agent connection: operations over HTTP, events over a WebSocket."""

    def __init__(
        self,
        base_url: str,
        creds: Credentials,
        state_path: Path | None = None,
        http: httpx.AsyncClient | None = None,
        connect: Callable[..., Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.creds = creds
        self.state_path = state_path
        self.http = http or httpx.AsyncClient(timeout=60)
        self._connect = connect or websockets.connect
        self.cursor: int | None = creds.cursor
        self.rooms: list[str] = []
        self.handlers: dict[str, list[Handler]] = {}
        self.connected = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # ---- operations ----------------------------------------------------
    def _headers(self, with_connection: bool = True) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.creds.api_key}"}
        if with_connection:
            h["X-Switch-Connection-Id"] = self.creds.connection_id
        return h

    async def op(self, name: str, with_connection: bool = True, **args: Any) -> Any:
        """Call one operation. Arguments go in the body unwrapped."""
        url = f"{self.base_url}/agents/{self.creds.agent_id}/ops/{name}"
        r = await self.http.post(url, json=args, headers=self._headers(with_connection))
        if r.status_code >= 400:
            raise SwitchError(r.status_code, r.text[:500], name)
        return r.json().get("result")

    async def list_ops(self) -> dict[str, Any]:
        url = f"{self.base_url}/agents/{self.creds.agent_id}/ops"
        r = await self.http.get(url, headers=self._headers(False))
        r.raise_for_status()
        return r.json().get("operations", {})

    async def reply(self, room_id: str, body: str, thread_id: str | None) -> Any:
        """Claim the room for this connection, then post in the thread."""
        await self.op("connect_to_room", room_id=room_id, include_general_instructions=False)
        args: dict[str, Any] = {"body": body}
        if thread_id:
            args["thread_id"] = thread_id
        return await self.op("post_message", **args)

    # ---- events --------------------------------------------------------
    def on(self, event: str, handler: Handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def ws_url(self, rooms: list[str] | None = None) -> str:
        q: dict[str, Any] = {"connection_id": self.creds.connection_id, "scope": "all"}
        if rooms:
            q["rooms"] = ",".join(rooms)
        if self.cursor is not None:
            q["start_from"] = self.cursor
        if self.creds.generation is not None:
            q["expected_generation"] = self.creds.generation
        q["client"] = "docdesk"
        q["client_version"] = "0.1.0"
        base = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/agents/{self.creds.agent_id}/connection/ws?{urlencode(q)}"

    def _persist(self) -> None:
        if self.state_path:
            self.creds.cursor = self.cursor
            self.creds.save(self.state_path)

    async def handle_frame(self, ws: Any, raw: str | bytes) -> None:
        """Process one frame. Pings are answered first, events after."""
        frame = json.loads(raw)
        kind = frame.get("event") or frame.get("type")
        if kind == "ping":
            await ws.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
            return
        data = frame.get("data") or {}
        if kind == "connection_state":
            self.creds.generation = data.get("generation")
            self.rooms = list(data.get("rooms") or [])
            if self.cursor is None:
                self.cursor = data.get("cursor")
            self._persist()
            self.connected.set()
            log.info("connected: rooms=%s generation=%s", self.rooms, self.creds.generation)
        elif kind == "evicted":
            code = data.get("code")
            log.warning("evicted: %s %s", code, data.get("reason"))
            if code in FINAL_EVICTIONS or code == "closed":
                raise Stop(code)
        elif kind == "refused":
            raise Stop(f"refused {data.get('status')}: {data.get('detail')}")
        elif kind == "gap":
            # Take resumed_at as the new cursor and re-read history later.
            self.cursor = data.get("resumed_at", self.cursor)
            log.warning("gap: %s", data.get("reason"))
        elif kind == "subscription_changed":
            self.rooms = list(data.get("rooms") or [])
        if frame.get("id") is not None:
            self.cursor = frame["id"]
            # Save the cursor per event, so a restart resumes after it and
            # does not replay (and re-answer) what we already handled.
            self._persist()
        for h in self.handlers.get(kind, []):
            # Never block the read loop: a slow LLM call must not delay pongs.
            t = asyncio.create_task(self._safe(h, data))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _safe(self, h: Handler, data: dict[str, Any]) -> None:
        try:
            await h(data)
        except Exception:  # noqa: BLE001 - one bad event must not kill the agent
            log.exception("handler failed")

    async def run(self, rooms: list[str] | None = None, max_backoff: float = 30.0) -> None:
        """Connect, read frames forever, reconnect with resume on a drop."""
        backoff = 1.0
        headers = {"Authorization": f"Bearer {self.creds.api_key}"}
        while True:
            try:
                async with self._connect(self.ws_url(rooms), additional_headers=headers) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        await self.handle_frame(ws, raw)
            except Stop as e:
                log.error("stopping, not reconnecting: %s", e)
                return
            except websockets.ConnectionClosed as e:
                code = getattr(e.rcvd, "code", None) if getattr(e, "rcvd", None) else None
                if code in FINAL_CLOSE_CODES:
                    log.error("socket closed with %s, not reconnecting", code)
                    return
                log.warning("socket closed (%s), reconnecting in %.0fs", code, backoff)
            except (OSError, websockets.InvalidHandshake, asyncio.TimeoutError) as e:
                # Includes InvalidMessage, which a restarting server produces.
                log.warning("connect failed (%s), retrying in %.0fs", e, backoff)
            self.connected.clear()
            self._persist()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
