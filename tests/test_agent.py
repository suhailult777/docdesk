from pathlib import Path

from agent.llm import ChainLLM, FakeLLM
from agent.main import DocDesk
from agent.rag import RepoIndex


class FakeAgent:
    def __init__(self):
        self.replies = []

    async def reply(self, room_id, body, thread_id):
        self.replies.append((room_id, thread_id, body))


def desk(tmp_path: Path) -> tuple[DocDesk, FakeAgent]:
    (tmp_path / "README.md").write_text("# shop\n\n## Run it locally\n\nRun `npm start`.\n")
    fa = FakeAgent()
    return DocDesk(fa, RepoIndex.from_repo(tmp_path), ChainLLM([FakeLLM()]), "docdesk"), fa


def msg(body, addressed=True, sender="user", mid="m1", thread=None):
    return {"room_id": "r1", "payload": {"addressed": addressed, "sender_name": sender,
                                          "message_id": mid, "thread_id": thread, "body": body}}


async def test_answers_in_thread_of_the_question(tmp_path):
    d, fa = desk(tmp_path)
    await d.on_message(msg("@docdesk how do I run it locally?"))
    assert len(fa.replies) == 1
    room, thread, body = fa.replies[0]
    assert room == "r1" and thread == "m1" and "npm start" in body


async def test_keeps_existing_thread(tmp_path):
    d, fa = desk(tmp_path)
    await d.on_message(msg("@docdesk run locally?", mid="m2", thread="root1"))
    assert fa.replies[0][1] == "root1"


async def test_ignores_unaddressed_self_and_replays(tmp_path):
    d, fa = desk(tmp_path)
    await d.on_message(msg("run locally?", addressed=False))
    await d.on_message(msg("@docdesk run locally?", sender="docdesk", mid="x"))
    await d.on_message(msg("@docdesk run locally?", mid="dup"))
    await d.on_message(msg("@docdesk run locally?", mid="dup"))
    assert len(fa.replies) == 1


class OpsAgent(FakeAgent):
    """FakeAgent that also records Switch ops (connect, ledger, handoff)."""

    def __init__(self):
        super().__init__()
        self.ops = []

    async def op(self, op_name, /, **args):
        self.ops.append((op_name, args))
        if op_name == "list_references":
            return {"documents": []}
        if op_name == "create_room_document":
            return {"document_id": "ledger-1"}
        return {}


def loop_desk(shop, tmp_path):
    fa = OpsAgent()
    d = DocDesk(fa, RepoIndex.from_repo(shop), ChainLLM([FakeLLM()]), "docdesk", repo_dir=shop,
                repo_url="https://github.com/o/shop", state_dir=tmp_path / "state", maint_room="maint")
    return d, fa


async def test_correction_in_thread_files_gap_and_hands_off(shop, tmp_path):
    d, fa = loop_desk(shop, tmp_path)
    await d.on_message(msg("@docdesk how do I change the port?", mid="q1"))
    assert "SHOP_PORT" in fa.replies[0][2]
    await d.on_message(msg("that's outdated, the server reads `PORT` now, not `SHOP_PORT`", mid="c1", thread="q1"))
    room, thread, body = fa.replies[1]
    assert thread == "q1" and body.startswith("You're right.") and "G-1" in body
    names = [o[0] for o in fa.ops]
    assert "create_room_document" in names and names[-1] == "send_targeted_message"
    handoff = fa.ops[-1][1]
    assert handoff["target_names"] == ["doc-fixer"] and "docdesk-gap" in handoff["body"]
    # the same correction again does not file a second gap
    await d.on_message(msg("outdated: it's `PORT`, not `SHOP_PORT`", mid="c2", thread="q1"))
    assert "Already logged as G-1" in fa.replies[2][2]
    assert [o[0] for o in fa.ops].count("send_targeted_message") == 1


async def test_unbacked_correction_files_nothing(shop, tmp_path):
    d, fa = loop_desk(shop, tmp_path)
    await d.on_message(msg("@docdesk how do I change the port?", mid="q1"))
    await d.on_message(msg("wrong, it's `APP_PORT` now", mid="c1", thread="q1"))
    assert "could not confirm" in fa.replies[1][2]
    assert not any(o[0] == "send_targeted_message" for o in fa.ops)


async def test_fixer_status_updates_ledger_and_original_thread(shop, tmp_path):
    from agent.fixer import status_block
    d, fa = loop_desk(shop, tmp_path)
    await d.on_message(msg("@docdesk how do I change the port?", mid="q1"))
    await d.on_message(msg("outdated, it's `PORT` now, not `SHOP_PORT`", mid="c1", thread="q1"))
    st = status_block({"gap": "G-1", "status": "PR open, verified", "pr": "https://github.com/o/shop/pull/7"})
    await d.on_message(msg("@docdesk ready " + st, sender="doc-fixer", mid="s1", thread="h1"))
    assert d.gaps.find("G-1").pr.endswith("/pull/7")
    assert fa.ops[-1][0] == "update_room_document" and "[PR](https://github.com/o/shop/pull/7)" in fa.ops[-1][1]["content"]
    room, thread, body = fa.replies[-1]
    assert thread == "q1" and "pull/7" in body
    # a status from anyone but the fixer is ignored
    await d.on_message(msg("@docdesk " + st.replace("G-1", "G-1"), sender="mallory", mid="s2"))
    assert len(fa.replies) == 3
