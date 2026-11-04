"""The doc-gap ledger: one row per checked correction, kept in Switch.

Each gap lives in two places:

* ``.state/gaps.json`` on disk, so ids are stable and the same correction
  in the same thread never files two gaps (idempotent across restarts),
* the ``doc-gap-ledger`` room document in #acme-docs-maint, so people and
  the fixer agent read the same table inside Switch.

Only the agent that created a room document can update it, so DocDesk
creates the ledger itself (not the template) and the fixer reports status
changes back to DocDesk with a targeted message.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("docdesk.ledger")

LEDGER_NAME = "doc-gap-ledger"
LEDGER_DESCRIPTION = "Every doc gap DocDesk found, and what happened to it"
LEDGER_INSTRUCTIONS = (
    "One row per gap. DocDesk writes every row. The fixer reports status and PR "
    "changes to @docdesk, which updates this table. Never delete a row."
)
HANDOFF_MARK = "docdesk-gap"


@dataclass
class Gap:
    id: str
    thread_id: str
    help_room_id: str
    asker: str
    question: str
    wrong_answer: str
    correction: str
    evidence: str
    files: list[str]
    replacements: list[list[str]]
    commits: list[str] = field(default_factory=list)
    code_files: list[str] = field(default_factory=list)  # code that backs the correction
    status: str = "open"
    pr: str = ""

    def key(self) -> str:
        return f"{self.thread_id}|{','.join(sorted(self.files))}"

    def row(self) -> str:
        wrong = "; ".join(f"`{o}` should be `{n}`" for o, n in self.replacements) or _cell(self.correction, 80)
        files = ", ".join(f"`{f}`" for f in self.files)
        pr = f"[PR]({self.pr})" if self.pr else ""
        return f"| {self.id} | {files} | {_cell(wrong, 120)} | {_cell(self.evidence, 200)} | {self.status} | {pr} |"


def _cell(text: str, limit: int) -> str:
    t = " ".join(text.split()).replace("|", "\\|")
    return t if len(t) <= limit else t[: limit - 3] + "..."


class GapStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.gaps: list[Gap] = []
        if self.path.exists():
            self.gaps = [Gap(**g) for g in json.loads(self.path.read_text())]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(g) for g in self.gaps], indent=2))
        tmp.replace(self.path)

    def find(self, gap_id: str) -> Gap | None:
        return next((g for g in self.gaps if g.id == gap_id), None)

    def add(self, gap: Gap) -> tuple[Gap, bool]:
        """Add a gap and give it the next id. Returns (gap, created)."""
        for g in self.gaps:
            if g.key() == gap.key():
                return g, False
        gap.id = f"G-{len(self.gaps) + 1}"
        self.gaps.append(gap)
        self.save()
        return gap, True

    def update(self, gap_id: str, **changes: Any) -> Gap | None:
        g = self.find(gap_id)
        if not g:
            return None
        for k, v in changes.items():
            setattr(g, k, v)
        self.save()
        return g

    def render(self) -> str:
        lines = [
            "# Doc gap ledger",
            "",
            "Written by DocDesk. Every gap was checked against the code and commit history first.",
            "",
            "| id | file | what was wrong | evidence | status | PR |",
            "|---|---|---|---|---|---|",
        ]
        lines += [g.row() for g in self.gaps]
        return "\n".join(lines) + "\n"


def handoff_text(gap: Gap, repo_url: str) -> str:
    """The message the fixer gets. Human readable, with a JSON block it parses."""
    payload = {
        "gap": gap.id, "repo": repo_url, "files": gap.files,
        "replacements": gap.replacements, "commits": gap.commits, "code_files": gap.code_files,
        "help_room_id": gap.help_room_id, "thread_id": gap.thread_id,
    }
    files = ", ".join(f"`{f}`" for f in gap.files)
    return (
        f"Doc gap **{gap.id}** from #acme-help, checked against the code.\n"
        f"- Question: {gap.question}\n"
        f"- Wrong answer (from the docs): {_cell(gap.wrong_answer, 300)}\n"
        f"- Correction by @{gap.asker}: {gap.correction}\n"
        f"- Evidence: {gap.evidence}\n"
        f"- Files to fix: {files}\n"
        "Please fix only these files, follow docs-style-guide, run the doc's commands in the sandbox "
        "and open a pull request for the docs owner to review.\n\n"
        f"```{HANDOFF_MARK}\n{json.dumps(payload)}\n```"
    )


def parse_handoff(body: str) -> dict[str, Any] | None:
    start = body.find(f"```{HANDOFF_MARK}")
    if start < 0:
        return None
    rest = body[start + len(HANDOFF_MARK) + 3:]
    end = rest.find("```")
    try:
        return json.loads(rest[:end] if end >= 0 else rest)
    except json.JSONDecodeError:
        return None


class RoomLedger:
    """Keeps the room document in sync with the store, through Switch ops."""

    def __init__(self, agent: Any, store: GapStore, room_id: str):
        self.agent = agent
        self.store = store
        self.room_id = room_id
        self.doc_id: str | None = None

    async def _find_doc(self) -> str | None:
        refs = await self.agent.op("list_references")
        for d in (refs or {}).get("documents", []):
            if d.get("name") == LEDGER_NAME:
                return d.get("id")
        return None

    async def sync(self) -> None:
        """Write the whole table. Caller holds the agent's room lock."""
        await self.agent.op("connect_to_room", room_id=self.room_id, include_general_instructions=False)
        content = self.store.render()
        if not self.doc_id:
            self.doc_id = await self._find_doc()
        if self.doc_id:
            try:
                await self.agent.op("update_room_document", document_id=self.doc_id, content=content)
                return
            except Exception as e:  # noqa: BLE001 - e.g. a ledger someone else created
                log.warning("could not update ledger %s: %s", self.doc_id, e)
                raise
        res = await self.agent.op(
            "create_room_document", name=LEDGER_NAME, description=LEDGER_DESCRIPTION,
            instructions=LEDGER_INSTRUCTIONS, content=content,
        )
        self.doc_id = (res or {}).get("document_id")
        log.info("created %s room document %s", LEDGER_NAME, self.doc_id)

    async def hand_off(self, gap: Gap, fixer: str, repo_url: str) -> None:
        """Tell the fixer in the docs room. Caller holds the room lock."""
        await self.agent.op("connect_to_room", room_id=self.room_id, include_general_instructions=False)
        await self.agent.op("send_targeted_message", body=handoff_text(gap, repo_url), target_names=[fixer])
