"""Post a question in a Mattermost channel as a human teammate, then wait
for the agent's reply in the same thread and print it.

Used by `make demo` to seed questions and as an end-to-end check.
Reads MATTERMOST_URL, MATTERMOST_TEAM, MATTERMOST_USER, MATTERMOST_PASSWORD.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import load_env  # noqa: E402


def login(c: httpx.Client, env: dict[str, str]) -> None:
    r = c.post("/api/v4/users/login", json={"login_id": env["MATTERMOST_USER"], "password": env["MATTERMOST_PASSWORD"]})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.headers['Token']}"


def find_channel(c: httpx.Client, team: str, display: str) -> dict:
    t = c.get(f"/api/v4/teams/name/{team}").json()
    me = c.get("/api/v4/users/me").json()
    chans = c.get(f"/api/v4/users/{me['id']}/teams/{t['id']}/channels").json()
    for ch in chans:
        if display in (ch.get("display_name"), ch.get("name")):
            return ch
    raise SystemExit(f"channel {display!r} not found; is the user a member?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--channel", default="acme-help")
    ap.add_argument("--wait", type=int, default=120)
    args = ap.parse_args()
    env = load_env()
    c = httpx.Client(base_url=env.get("MATTERMOST_URL", "http://localhost:8065"), timeout=30)
    login(c, env)
    ch = find_channel(c, env.get("MATTERMOST_TEAM", "switch"), args.channel)
    me = c.get("/api/v4/users/me").json()["id"]
    post = c.post("/api/v4/posts", json={"channel_id": ch["id"], "message": args.question}).json()
    print(f"[{time.strftime('%H:%M:%S')}] asked in #{args.channel}: {args.question}")
    t0 = time.time()
    while time.time() - t0 < args.wait:
        time.sleep(2)
        thread = c.get(f"/api/v4/posts/{post['id']}/thread").json()
        replies = [p for p in thread["posts"].values() if p["id"] != post["id"] and p["user_id"] != me]
        if replies:
            r = sorted(replies, key=lambda p: p["create_at"])[0]
            print(f"[{time.strftime('%H:%M:%S')}] reply after {time.time() - t0:.1f}s:\n{r['message']}")
            return 0
    print("no reply in time", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
