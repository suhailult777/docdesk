"""doc-fixer: the agent in #acme-docs-maint that turns a doc gap into a verified PR.

It joins Switch as its own agent over the agent protocol and only acts on
handoffs from DocDesk (a message with a ``docdesk-gap`` block). For each gap:

1. fresh branch of the demo repo from origin/main,
2. edit only the listed files: OpenCode (free model) first, and if it times
   out, edits other files or leaves the old term in place, a plain
   replacement of the checked old -> new terms,
3. run the doc's commands in the sandbox before and after (``agent.verify``);
   if a changed command fails, stop and say why: the fix never reaches a human,
4. commit, push, ``gh pr create``, then post the PR and the sandbox report
   in the room for ``@docs-owner`` and report the status to ``@docdesk``.

It never merges. Merging waits for the docs owner (Day 3).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .client import SwitchAgent, load_or_register
from .config import Settings
from .ledger import parse_handoff
from .llm import OpenCodeLLM
from .verify import Verdict, compare, verify_files

log = logging.getLogger("docdesk.fixer")

DESCRIPTION = "Fixes doc gaps that DocDesk checked, proves the doc's commands run in a sandbox, opens a PR."
STATUS_MARK = "docdesk-status"


def git(repo: Path, *args: str, check: bool = True, timeout: int = 60) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def prepare_branch(workdir: Path, repo_url: str, branch: str) -> None:
    if not (workdir / ".git").exists():
        workdir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", repo_url, str(workdir)], check=True, timeout=120)
    git(workdir, "fetch", "-q", "origin")
    git(workdir, "reset", "-q", "--hard")
    git(workdir, "clean", "-qfd")
    git(workdir, "checkout", "-q", "-B", branch, "origin/main")


def replace_terms(workdir: Path, files: list[str], replacements: list[list[str]]) -> list[str]:
    """The fallback fix: swap each checked old term for the new one."""
    changed = []
    for f in files:
        p = workdir / f
        if not p.exists():
            continue
        text = p.read_text()
        new = text
        for old, rep in replacements:
            new = new.replace(old, rep)
        if new != text:
            p.write_text(new)
            changed.append(f)
    return changed


def changed_files(workdir: Path) -> list[str]:
    return [line for line in git(workdir, "diff", "--name-only").splitlines() if line]


def edit_ok(workdir: Path, files: list[str], replacements: list[list[str]]) -> bool:
    """An edit counts only if it touched just the listed files and removed the old terms."""
    changed = changed_files(workdir)
    if not changed or not set(changed) <= set(files):
        return False
    for f in files:
        text = (workdir / f).read_text() if (workdir / f).exists() else ""
        if any(old in text for old, _ in replacements):
            return False
    return True


def opencode_prompt(gap: dict[str, Any], style: str) -> str:
    reps = "\n".join(f"- `{o}` is now `{n}`" for o, n in gap.get("replacements", []))
    return (
        "You are fixing stale documentation in this git repository.\n"
        f"Edit ONLY these files: {', '.join(gap['files'])}. Do not touch any other file. Do not run git.\n"
        f"These facts were checked against the code (commits {', '.join(gap.get('commits', []))}):\n{reps}\n"
        "Update every place in those files that uses the old value, including commands in code blocks, "
        "so each command runs as written. Keep the rest of the text as it is.\n"
        f"Style guide:\n{style}\n"
    )


def edit_with_opencode(workdir: Path, gap: dict[str, Any], style: str, oc: OpenCodeLLM, timeout: int) -> bool:
    if not oc.available():
        return False
    try:
        subprocess.run(
            [oc.binary, "run", "-m", oc.model, opencode_prompt(gap, style)],
            cwd=workdir, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log.warning("opencode timed out after %ss", timeout)
    if edit_ok(workdir, gap["files"], gap.get("replacements", [])):
        return True
    git(workdir, "checkout", "-q", "--", ".")
    git(workdir, "clean", "-qfd")
    return False


@dataclass
class FixResult:
    ok: bool
    editor: str
    files: list[str]
    verdict: Verdict | None
    pr_url: str = ""
    error: str = ""


def slug(gap_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", gap_id.lower())


def pr_title(gap: dict[str, Any]) -> str:
    reps = ", ".join(f"`{o}` is now `{n}`" for o, n in gap.get("replacements", [])[:2])
    return f"docs: fix doc gap {gap['gap']}" + (f" ({reps})" if reps else "")


def pr_body(gap: dict[str, Any], handoff: str, verdict: Verdict, before_report: str, editor: str) -> str:
    facts = handoff.split("```")[0].strip()
    return (
        f"Opened by the DocDesk doc-fixer agent from Switch room #acme-docs-maint (edited with {editor}).\n\n"
        f"## Doc gap {gap['gap']}\n\n{facts}\n\n"
        f"## Sandbox check\n\nBefore the fix:\n\n{before_report}\n\nAfter the fix:\n\n{verdict.report()}\n\n"
        "## Review\n\nDocs owner (@docs-owner in Switch): please review the diff and approve or decline in "
        "#acme-docs-maint. This PR is not merged until you approve.\n"
    )


def open_pr(workdir: Path, gap: dict[str, Any], branch: str, title: str, body: str,
            author: tuple[str, str], gh: str = "gh") -> str:
    name, email = author
    git(workdir, "add", *gap["files"])
    git(workdir, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", title)
    git(workdir, "push", "-q", "-u", "origin", branch, timeout=120)
    repo = re.sub(r"^https://github.com/", "", gap["repo"]).removesuffix(".git").rstrip("/")
    p = subprocess.run(
        [gh, "pr", "create", "--repo", repo, "--base", "main", "--head", branch, "--title", title, "--body", body],
        capture_output=True, text=True, timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {p.stderr.strip()[:300]}")
    return p.stdout.strip().splitlines()[-1]


def fix_gap(
    gap: dict[str, Any], handoff: str, workdir: Path, style: str, author: tuple[str, str],
    oc: OpenCodeLLM | None = None, oc_timeout: int = 150,
    verifier: Callable[[Path, list[str]], list] = verify_files,
    opener: Callable[..., str] = open_pr, progress: Callable[[str], None] = lambda s: None,
) -> FixResult:
    branch = f"docfix/{slug(gap['gap'])}-{time.strftime('%m%d-%H%M%S')}"
    prepare_branch(workdir, gap["repo"], branch)
    before = verifier(workdir, gap["files"])
    editor = "OpenCode"
    progress(f"Editing {', '.join(gap['files'])} with OpenCode (free model)...")
    if not (oc and edit_with_opencode(workdir, gap, style, oc, oc_timeout)):
        editor = "checked term replacement (OpenCode fallback)"
        if not replace_terms(workdir, gap["files"], gap.get("replacements", [])):
            return FixResult(False, editor, [], None, error="nothing to change in the listed files")
    files = changed_files(workdir)
    progress(f"Edited {', '.join(files)} ({editor}). Running the doc's commands in the sandbox...")
    after = verifier(workdir, files)
    verdict = compare([r for r in before if r.cmd.file in files], after)
    if not verdict.ok:
        return FixResult(False, editor, files, verdict, error="a changed command fails in the sandbox")
    before_report = "\n".join(f"- {r.line()}" for r in before if r.cmd.file in files) or "- no commands"
    title = pr_title(gap)
    url = opener(workdir, gap, branch, title, pr_body(gap, handoff, verdict, before_report, editor), author)
    return FixResult(True, editor, files, verdict, pr_url=url)


def status_block(data: dict[str, Any]) -> str:
    return f"```{STATUS_MARK}\n{json.dumps(data)}\n```"


def parse_status(body: str) -> dict[str, Any] | None:
    start = body.find(f"```{STATUS_MARK}")
    if start < 0:
        return None
    rest = body[start + len(STATUS_MARK) + 3:]
    try:
        return json.loads(rest[: rest.find("```")] if "```" in rest else rest)
    except json.JSONDecodeError:
        return None


class Fixer:
    def __init__(self, agent: Any, settings: Settings, docdesk_name: str = "docdesk"):
        self.agent = agent
        self.s = settings
        self.docdesk = docdesk_name
        self.workdir = settings.state_dir / "fixer" / "repo"
        self.lock = asyncio.Lock()
        self.seen: set[str] = set()
        env = settings.env
        self.author = (env.get("DOCDESK_GIT_NAME") or "docdesk-fixer",
                       env.get("DOCDESK_GIT_EMAIL") or "docdesk-fixer@users.noreply.github.com")
        self.oc = OpenCodeLLM(env.get("OPENCODE_MODEL") or "opencode/deepseek-v4.1-flash-free")
        self.oc_timeout = int(env.get("FIXER_OPENCODE_TIMEOUT") or 150)

    async def say(self, room: str, body: str, thread: str | None, names=(), roles=()) -> None:
        async with self.agent.room_lock:
            await self.agent.op("connect_to_room", room_id=room, include_general_instructions=False)
            if names or roles:
                args: dict[str, Any] = {"body": body, "target_names": list(names), "target_roles": list(roles)}
                if thread:
                    args["thread_id"] = thread
                try:
                    await self.agent.op("send_targeted_message", **args)
                    return
                except Exception as e:  # noqa: BLE001 - e.g. nobody holds the role yet
                    log.warning("targeted message failed (%s), posting with plain mentions", e)
                    body = " ".join(f"@{x}" for x in [*names, *roles]) + " " + body
            await self.agent.op("post_message", body=body, **({"thread_id": thread} if thread else {}))

    async def style_guide(self, room: str) -> str:
        try:
            async with self.agent.room_lock:
                refs = await self.agent.op("connect_to_room", room_id=room, include_general_instructions=False)
                ids = [d["id"] for d in refs.get("documents", []) if d.get("name") == "docs-style-guide"]
                docs = await self.agent.op("load_internal_documents", ids=ids) if ids else []
            return docs[0]["content"] if docs else ""
        except Exception:  # noqa: BLE001
            return ""

    async def on_message(self, data: dict[str, Any]) -> None:
        p = data.get("payload") or {}
        body = p.get("body") or ""
        mid = p.get("message_id")
        if not p.get("addressed") or p.get("sender_name") == self.s.agent_name or mid in self.seen:
            return
        gap = parse_handoff(body)
        if not gap:
            return
        self.seen.add(mid)
        room = data["room_id"]
        thread = p.get("thread_id") or mid
        async with self.lock:  # one fix at a time
            await self.say(room, f"Picked up doc gap {gap['gap']}. Fixing {', '.join(gap['files'])} on a new branch.", thread)
            style = await self.style_guide(room)
            loop = asyncio.get_running_loop()

            def progress(msg: str) -> None:
                asyncio.run_coroutine_threadsafe(self.say(room, msg, thread), loop)

            try:
                r = await asyncio.to_thread(
                    fix_gap, gap, body, self.workdir, style, self.author, self.oc, self.oc_timeout,
                    progress=progress,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("fix failed")
                r = FixResult(False, "", [], None, error=str(e)[:300])
            await self.report(room, thread, gap, r)

    async def report(self, room: str, thread: str, gap: dict[str, Any], r: FixResult) -> None:
        if r.ok:
            status = {"gap": gap["gap"], "status": "PR open, verified", "pr": r.pr_url,
                      "help_room_id": gap.get("help_room_id"), "thread_id": gap.get("thread_id")}
            body = (f"Doc fix for {gap['gap']} is ready for review: {r.pr_url}\n"
                    f"Changed: {', '.join(f'`{f}`' for f in r.files)} (edited with {r.editor}).\n"
                    f"{r.verdict.report()}\nI won't merge it. Please review and approve or decline here.\n\n"
                    + status_block(status))
            await self.say(room, body, thread, names=[self.docdesk], roles=["docs-owner"])
        else:
            status = {"gap": gap["gap"], "status": "fix blocked", "pr": "",
                      "help_room_id": gap.get("help_room_id"), "thread_id": gap.get("thread_id")}
            why = r.error
            if r.verdict:
                why += "\n" + "\n".join(f"- {b.line()}" for b in r.verdict.blocking)
            body = (f"I did not open a PR for {gap['gap']}: {why}\n"
                    "A fix whose commands fail does not go to review. It needs a person to look.\n\n"
                    + status_block(status))
            await self.say(room, body, thread, names=[self.docdesk], roles=["docs-owner"])


async def run(settings: Settings) -> None:
    creds = load_or_register(
        settings.creds_path, settings.switch_api, settings.registration_key,
        settings.agent_name, DESCRIPTION, "doc-fixer",
    )
    agent = SwitchAgent(settings.switch_api, creds, settings.creds_path)
    fixer = Fixer(agent, settings, settings.env.get("DOCDESK_AGENT_NAME") or "docdesk")
    agent.on("message", fixer.on_message)
    await agent.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the doc-fixer Switch agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings.from_env()
    s.agent_name = s.env.get("DOCDESK_FIXER_NAME") or "doc-fixer"
    asyncio.run(run(s))


if __name__ == "__main__":
    main()
