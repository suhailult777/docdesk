"""Run DocDesk: connect to Switch and answer addressed messages in-thread."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import time
from typing import Any

from pathlib import Path

from .answer import answer, strip_mentions
from .client import SwitchAgent, load_or_register
from .config import Settings
from .corrections import check_correction, extract_terms, looks_like_correction, reply_text
from .fixer import parse_status
from .ledger import Gap, GapStore, RoomLedger, parse_handoff
from .llm import ChainLLM, LLMError, make_llm
from .rag import RepoIndex

log = logging.getLogger("docdesk")

DESCRIPTION = (
    "Answers team questions from the repo with file and commit citations, "
    "and turns corrections into doc fixes."
)


def ensure_repo(settings: Settings) -> None:
    """Clone the target repo on first run, pull on later runs."""
    d = settings.repo_dir
    if (d / ".git").exists():
        try:
            subprocess.run(["git", "-C", str(d), "pull", "--ff-only", "-q"], check=False, timeout=20)
        except subprocess.TimeoutExpired:
            log.warning("git pull timed out, indexing the checkout as it is")
        return
    d.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", settings.repo_url, str(d)], check=True)


class ThreadMemory:
    """What DocDesk answered in each thread, so a later reply can correct it."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, dict[str, str]] = {}
        if path and path.exists():
            self.data = json.loads(path.read_text())

    def get(self, thread: str | None) -> dict[str, str] | None:
        return self.data.get(thread) if thread else None

    def put(self, thread: str, **entry: str) -> None:
        self.data[thread] = entry
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1))


CLASSIFY = (
    "A teammate replied to an answer in a team chat. Is the reply saying the answer is wrong or "
    "outdated? Reply with one word: yes or no."
)


class DocDesk:
    def __init__(
        self, agent: SwitchAgent, index: RepoIndex, llm: ChainLLM, name: str,
        repo_dir: Path | None = None, repo_url: str = "", state_dir: Path | None = None,
        maint_room: str | None = None, fixer: str = "doc-fixer",
    ):
        self.agent = agent
        self.index = index
        self.llm = llm
        self.name = name
        self.repo_dir = repo_dir
        self.repo_url = repo_url
        self.fixer = fixer
        self.maint_room = maint_room
        self.seen: set[str] = set()
        # One LLM call at a time: the OpenCode CLI times out when two runs overlap.
        self.llm_lock = asyncio.Lock()
        self.room_lock = getattr(agent, "room_lock", None) or asyncio.Lock()
        self.threads = ThreadMemory(state_dir / "threads.json" if state_dir else None)
        self.gaps = GapStore((state_dir or Path("/tmp")) / "gaps.json") if state_dir else None
        self.ledger = RoomLedger(agent, self.gaps, maint_room) if (self.gaps and maint_room) else None

    def should_answer(self, data: dict[str, Any]) -> bool:
        p = data.get("payload") or {}
        if not p.get("addressed"):
            return False
        if p.get("sender_name") == self.name:  # never answer ourselves
            return False
        mid = p.get("message_id")
        if mid in self.seen:  # a replay after resume
            return False
        if mid:
            self.seen.add(mid)
        return bool(strip_mentions(p.get("body") or ""))

    async def on_message(self, data: dict[str, Any]) -> None:
        if not self.should_answer(data):
            return
        p = data["payload"]
        raw = p.get("body") or ""
        status = parse_status(raw)
        if status:
            if p.get("sender_name") == self.fixer:
                await self.on_fixer_status(status)
            return
        if parse_handoff(raw):
            return
        text = strip_mentions(raw)
        thread = p.get("thread_id") or p.get("message_id")
        earlier = self.threads.get(p.get("thread_id"))
        if earlier and self.repo_dir and await self.is_correction(text, earlier):
            await self.on_correction(data, text, thread, earlier)
            return
        await self.on_question(data, text, thread)

    async def is_correction(self, text: str, earlier: dict[str, str]) -> bool:
        if looks_like_correction(text):
            return True
        # No keyword, but it names a command, variable or path: ask the LLM when one is
        # configured (the offline fake can't judge, so it never decides this).
        if not extract_terms(text) or all(p.name == "fake" for p in self.llm.providers):
            return False
        prompt = f"Answer: {earlier.get('answer', '')[:800]}\n\nReply: {text}\n"
        try:
            async with self.llm_lock:
                out = await asyncio.to_thread(self.llm.complete, CLASSIFY, prompt)
        except LLMError:
            return False
        return out.strip().lower().startswith("yes")

    async def on_question(self, data: dict[str, Any], question: str, thread: str) -> None:
        p = data["payload"]
        t0 = time.monotonic()
        log.info("question from %s in %s: %r", p.get("sender_name"), data.get("room_id"), question)
        async with self.llm_lock:
            a = await asyncio.to_thread(answer, question, self.index, self.llm)
        await self.agent.reply(data["room_id"], a.text, thread)
        self.threads.put(thread, question=question, answer=a.text, asker=p.get("sender_name") or "",
                         room=data["room_id"])
        log.info(
            "answered in %.1fs via %s, confidence %.2f, cites %s",
            time.monotonic() - t0, a.provider, a.confidence,
            [h.chunk.ref(self.index.sha) for h in a.hits[:3]],
        )

    async def on_correction(self, data: dict[str, Any], text: str, thread: str, earlier: dict[str, str]) -> None:
        p = data["payload"]
        room = data["room_id"]
        log.info("correction from %s in thread %s: %r", p.get("sender_name"), thread, text)
        ev = await asyncio.to_thread(check_correction, self.repo_dir, text, earlier.get("answer", ""))
        gap = None
        created = False
        if ev.confirmed and ev.files and self.gaps is not None:
            gap, created = self.gaps.add(Gap(
                id="", thread_id=thread, help_room_id=room, asker=p.get("sender_name") or "",
                question=earlier.get("question", ""), wrong_answer=earlier.get("answer", "").split("\n\nSources")[0],
                correction=text, evidence=ev.summary(), files=ev.files,
                replacements=[list(r) for r in ev.replacements()],
                commits=sorted({c.sha for c in ev.commits.values()}),
                code_files=sorted({r.path for refs in ev.code_refs.values() for r in refs})[:4],
            ))
        msg = reply_text(ev, gap.id if gap else None)
        if gap and not created:
            msg += f"\n(Already logged as {gap.id}, status: {gap.status}.)"
        await self.agent.reply(room, msg, thread)
        log.info("correction %s: %s", "confirmed" if ev.confirmed else "not confirmed", ev.summary())
        if gap and created and self.ledger:
            async with self.room_lock:
                await self.ledger.sync()
                await self.ledger.hand_off(gap, self.fixer, self.repo_url)
            log.info("filed %s for %s and handed it to %s", gap.id, gap.files, self.fixer)

    async def on_fixer_status(self, st: dict[str, Any]) -> None:
        if not self.gaps or not self.ledger:
            return
        g = self.gaps.update(st.get("gap", ""), status=st.get("status", ""), pr=st.get("pr", ""))
        if not g:
            return
        async with self.room_lock:
            await self.ledger.sync()
        if g.pr:
            msg = (f"Update on doc gap {g.id}: the doc fix is in {g.pr} and its commands passed in a sandbox. "
                   "It merges after the docs owner approves it in #acme-docs-maint.")
        else:
            msg = f"Update on doc gap {g.id}: the fixer could not make a fix that passes its own checks yet, so a person will look."
        await self.agent.reply(g.help_room_id, msg, g.thread_id)
        log.info("%s is now %r %s", g.id, g.status, g.pr)


async def run(settings: Settings) -> None:
    ensure_repo(settings)
    index = RepoIndex.from_repo(settings.repo_dir, settings.repo_url)
    llm = make_llm(settings.env)
    log.info("indexed %d chunks at %s, llm chain %s", len(index.chunks), index.sha[:7], llm.name)
    creds = load_or_register(
        settings.creds_path, settings.switch_api, settings.registration_key,
        settings.agent_name, DESCRIPTION, "DocDesk",
    )
    agent = SwitchAgent(settings.switch_api, creds, settings.creds_path)
    rooms_file = settings.state_dir / "rooms.json"
    rooms = json.loads(rooms_file.read_text()) if rooms_file.exists() else {}
    desk = DocDesk(
        agent, index, llm, settings.agent_name, repo_dir=settings.repo_dir, repo_url=settings.repo_url,
        state_dir=settings.state_dir, maint_room=rooms.get("acme-docs-maint"),
        fixer=settings.env.get("DOCDESK_FIXER_NAME") or "doc-fixer",
    )
    agent.on("message", desk.on_message)
    await agent.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the DocDesk Switch agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(Settings.from_env()))


if __name__ == "__main__":
    main()
