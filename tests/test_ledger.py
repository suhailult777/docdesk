from agent.ledger import Gap, GapStore, RoomLedger, handoff_text, parse_handoff


def gap(thread="t1", files=("README.md",)):
    return Gap(id="", thread_id=thread, help_room_id="help", asker="user", question="how do I change the port?",
               wrong_answer="Set `SHOP_PORT`", correction="it's `PORT` now, not `SHOP_PORT`",
               evidence="`src/server.js:1` has `PORT` since abc123", files=list(files),
               replacements=[["SHOP_PORT", "PORT"]], commits=["abc123"])


def test_ids_are_sequential_and_idempotent(tmp_path):
    s = GapStore(tmp_path / "gaps.json")
    g1, new1 = s.add(gap())
    g1b, new1b = s.add(gap())
    g2, new2 = s.add(gap(thread="t2"))
    assert (g1.id, new1, g1b.id, new1b, g2.id, new2) == ("G-1", True, "G-1", False, "G-2", True)
    assert [g.id for g in GapStore(tmp_path / "gaps.json").gaps] == ["G-1", "G-2"]  # survives restart


def test_render_table_and_update(tmp_path):
    s = GapStore(tmp_path / "gaps.json")
    s.add(gap())
    s.update("G-1", status="PR open, verified", pr="https://github.com/o/r/pull/1")
    md = s.render()
    assert "| G-1 | `README.md` | `SHOP_PORT` should be `PORT` |" in md
    assert "PR open, verified | [PR](https://github.com/o/r/pull/1) |" in md


def test_handoff_roundtrip():
    g = gap()
    g.id = "G-1"
    text = handoff_text(g, "https://github.com/o/r")
    assert "Doc gap **G-1**" in text and "docs owner" in text
    data = parse_handoff(text)
    assert data["gap"] == "G-1" and data["files"] == ["README.md"] and data["replacements"] == [["SHOP_PORT", "PORT"]]
    assert parse_handoff("no block here") is None


class FakeOps:
    def __init__(self, docs=None, fail_update=False):
        self.calls = []
        self.docs = docs or []
        self.fail_update = fail_update

    async def op(self, op_name, /, **args):
        name = op_name
        self.calls.append((name, args))
        if name == "list_references":
            return {"documents": self.docs}
        if name == "create_room_document":
            return {"document_id": "doc-1"}
        return {}


async def test_creates_ledger_once_then_updates(tmp_path):
    s = GapStore(tmp_path / "gaps.json")
    s.add(gap())
    ops = FakeOps()
    led = RoomLedger(ops, s, "maint")
    await led.sync()
    await led.sync()
    names = [c[0] for c in ops.calls]
    assert names.count("create_room_document") == 1
    assert names[-1] == "update_room_document" and ops.calls[-1][1]["document_id"] == "doc-1"
    await led.hand_off(s.gaps[0], "doc-fixer", "https://github.com/o/r")
    assert ops.calls[-1][0] == "send_targeted_message" and ops.calls[-1][1]["target_names"] == ["doc-fixer"]
