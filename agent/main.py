"""Run DocDesk: connect to Switch and answer addressed messages in-thread."""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import time
from typing import Any

from .answer import answer, strip_mentions
from .client import SwitchAgent, load_or_register
from .config import Settings
from .llm import ChainLLM, make_llm
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
        subprocess.run(["git", "-C", str(d), "pull", "--ff-only", "-q"], check=False)
        return
    d.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", settings.repo_url, str(d)], check=True)


class DocDesk:
    def __init__(self, agent: SwitchAgent, index: RepoIndex, llm: ChainLLM, name: str):
        self.agent = agent
        self.index = index
        self.llm = llm
        self.name = name
        self.seen: set[str] = set()

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
        question = strip_mentions(p.get("body") or "")
        thread = p.get("thread_id") or p.get("message_id")
        t0 = time.monotonic()
        log.info("question from %s in %s: %r", p.get("sender_name"), data.get("room_id"), question)
        a = await asyncio.to_thread(answer, question, self.index, self.llm)
        await self.agent.reply(data["room_id"], a.text, thread)
        log.info(
            "answered in %.1fs via %s, confidence %.2f, cites %s",
            time.monotonic() - t0, a.provider, a.confidence,
            [h.chunk.ref(self.index.sha) for h in a.hits[:3]],
        )


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
    desk = DocDesk(agent, index, llm, settings.agent_name)
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
