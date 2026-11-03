"""One-time Switch setup for the demo.

1. Register the docdesk agent with a registration key (once; the key is
   kept in .state/, which git ignores).
2. Provision the room group from switch/docdesk.group.template.yaml with
   the agent operation create_room_from_yaml, as the agent's owner.
3. Save the created room ids to .state/rooms.json.

Safe to run again: it reuses the saved agent and skips the template when
the rooms already exist.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.client import SwitchAgent, SwitchError, load_or_register  # noqa: E402
from agent.config import ROOT, Settings  # noqa: E402
from agent.main import DESCRIPTION  # noqa: E402

TEMPLATE = ROOT / "switch" / "docdesk.group.template.yaml"


async def main() -> int:
    s = Settings.from_env()
    creds = load_or_register(
        s.creds_path, s.switch_api, s.registration_key, s.agent_name, DESCRIPTION, "DocDesk"
    )
    print(f"agent {s.agent_name}: {creds.agent_id}")
    agent = SwitchAgent(s.switch_api, creds)
    rooms_file = s.state_dir / "rooms.json"

    existing = await agent.op("list_rooms", with_connection=False)
    listed = existing.get("rooms", []) if isinstance(existing, dict) else (existing or [])
    names = {r.get("name"): r.get("id") or r.get("room_id") for r in listed}
    if "acme-help" in names and "acme-docs-maint" in names:
        print("rooms already exist, skipping template")
        rooms = {"acme-help": names["acme-help"], "acme-docs-maint": names["acme-docs-maint"]}
    else:
        try:
            result = await agent.op(
                "create_room_from_yaml", with_connection=False,
                yaml=TEMPLATE.read_text(),
                inputs={"teammate": s.teammate, "docdesk_agent": s.agent_name, "repo_url": s.repo_url},
            )
        except SwitchError as e:
            print(f"template run failed: {e}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2)[:1500])
        rooms = {r.get("room_name") or r.get("name"): r.get("room_id") or r.get("id")
                 for r in result.get("rooms", [])}
    s.state_dir.mkdir(parents=True, exist_ok=True)
    rooms_file.write_text(json.dumps(rooms, indent=2))
    print(f"rooms: {rooms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
