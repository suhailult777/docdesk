from pathlib import Path

from agent import fixer as fx
from agent.config import Settings
from agent.fixer import PRBook, approvers_from_env, parse_decision, parse_status


def test_parse_decision():
    assert parse_decision("@doc-fixer approve G-2") == "approve"
    assert parse_decision("lgtm, ship it") == "approve"
    assert parse_decision("decline G-2, wrong file") == "decline"
    assert parse_decision("approve? no, don't merge") == "decline"
    assert parse_decision("what does this change?") is None


def test_approvers_allowlist():
    assert approvers_from_env({"DOCDESK_APPROVERS": "@Mohd, user "}) == {"mohd", "user"}
    assert approvers_from_env({"DOCDESK_DOCS_OWNER": "user"}) == {"user"}
    assert approvers_from_env({}) == set()


def test_prbook_finds_by_gap_thread_or_single(tmp_path):
    b = PRBook(tmp_path / "prs.json")
    b.put("G-2", pr="u2", repo="o/r", maint_thread="t2")
    assert b.for_message("approve", None)[0] == "G-2"  # the only one waiting
    b.put("G-4", pr="u4", repo="o/r", maint_thread="t4")
    assert b.for_message("approve g-4", None)[0] == "G-4"
    assert b.for_message("approve", "t2")[0] == "G-2"
    assert b.for_message("approve", None) is None  # ambiguous
    assert PRBook(tmp_path / "prs.json").data.keys() == {"G-2", "G-4"}


class Agent:
    def __init__(self):
        import asyncio
        self.room_lock = asyncio.Lock()
        self.sent = []

    async def op(self, op_name, /, **args):
        if op_name in ("post_message", "send_targeted_message"):
            self.sent.append(args)
        return {"roles": []}


def make_fixer(tmp_path, monkeypatch, merged):
    env = {"DOCDESK_APPROVERS": "user", "DOCDESK_STATE_DIR": str(tmp_path)}
    s = Settings.from_env(env)
    f = fx.Fixer(Agent(), s)
    f.prs.put("G-2", pr="https://github.com/o/r/pull/9", repo="o/r", maint_thread="t2")
    monkeypatch.setattr(fx, "merge_pr", lambda repo, pr, d, who: merged.append((pr, d, who)) or "abc1234")
    return f


def msg(body, sender, thread="t2"):
    return {"room_id": "maint", "payload": {"addressed": True, "sender_name": sender, "message_id": body + sender,
                                            "thread_id": thread, "body": body}}


async def test_only_an_approver_can_merge(tmp_path, monkeypatch):
    merged = []
    f = make_fixer(tmp_path, monkeypatch, merged)
    await f.on_message(msg("@doc-fixer approve G-2", "mallory"))
    assert merged == [] and "Only the docs owner" in f.agent.sent[-1]["body"]
    await f.on_message(msg("@doc-fixer approve G-2", "user"))
    assert merged == [("https://github.com/o/r/pull/9", "approve", "user")]
    st = parse_status(f.agent.sent[-1]["body"])
    assert st["merged"] == "abc1234" and st["approved_by"] == "user" and f.agent.sent[-1]["target_names"] == ["docdesk"]
    assert "G-2" not in f.prs.data  # done, a second approve does nothing
    await f.on_message(msg("@doc-fixer approve G-2 again", "user"))
    assert len(merged) == 1


async def test_decline_closes(tmp_path, monkeypatch):
    merged = []
    f = make_fixer(tmp_path, monkeypatch, merged)
    await f.on_message(msg("@doc-fixer decline G-2", "user"))
    assert merged[0][1] == "decline" and parse_status(f.agent.sent[-1]["body"])["declined_by"] == "user"
