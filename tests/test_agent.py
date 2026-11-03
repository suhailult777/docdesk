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
