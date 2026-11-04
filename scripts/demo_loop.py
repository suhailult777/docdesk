"""Run the Day 2 loop end to end from Mattermost, as a human teammate.

1. ask a question in #acme-help and wait for DocDesk's cited answer,
2. reply in that thread with a correction,
3. wait for DocDesk to check it against the code and file a doc gap,
4. watch #acme-docs-maint until doc-fixer posts a PR link (or gives up),
5. wait for DocDesk's update back in the original thread.

  python scripts/demo_loop.py "how do I change the port?" \
      "That's outdated, we use \\`npm run dev\\` and the server reads \\`PORT\\` now, not \\`SHOP_PORT\\`."
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
from scripts.ask import find_channel, login  # noqa: E402


def stamp(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_thread(c: httpx.Client, root: str, me: str, after: int, wait: int, contains: str = "") -> dict | None:
    t0 = time.time()
    while time.time() - t0 < wait:
        posts = c.get(f"/api/v4/posts/{root}/thread").json()["posts"].values()
        new = sorted((p for p in posts if p["user_id"] != me and p["create_at"] > after and contains in p["message"]),
                     key=lambda p: p["create_at"])
        if new:
            return new[0]
        time.sleep(2)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("correction")
    ap.add_argument("--wait", type=int, default=420)
    a = ap.parse_args()
    env = load_env()
    c = httpx.Client(base_url=env.get("MATTERMOST_URL", "http://localhost:8065"), timeout=30)
    login(c, env)
    team = env.get("MATTERMOST_TEAM", "switch")
    help_ch, maint_ch = find_channel(c, team, "acme-help"), find_channel(c, team, "acme-docs-maint")
    me = c.get("/api/v4/users/me").json()["id"]
    t_start = time.time()

    q = c.post("/api/v4/posts", json={"channel_id": help_ch["id"], "message": a.question}).json()
    stamp(f"#acme-help asked: {a.question}")
    ans = wait_thread(c, q["id"], me, q["create_at"], 150)
    if not ans:
        stamp("no answer")
        return 1
    stamp(f"DocDesk answered:\n{ans['message']}\n")

    cor = c.post("/api/v4/posts", json={"channel_id": help_ch["id"], "message": a.correction,
                                         "root_id": q["id"]}).json()
    stamp(f"correction in thread: {a.correction}")
    chk = wait_thread(c, q["id"], me, cor["create_at"], 120)
    if not chk:
        stamp("no reply to the correction")
        return 1
    stamp(f"DocDesk checked it:\n{chk['message']}\n")

    since = cor["create_at"]
    seen: set[str] = set()
    pr_seen = False
    t0 = time.time()
    while time.time() - t0 < a.wait and not pr_seen:
        posts = c.get(f"/api/v4/channels/{maint_ch['id']}/posts", params={"since": since}).json()
        for pid in sorted(posts.get("posts", {}), key=lambda i: posts["posts"][i]["create_at"]):
            p = posts["posts"][pid]
            if pid in seen or p["create_at"] <= since:
                continue
            seen.add(pid)
            stamp(f"#acme-docs-maint:\n{p['message']}\n")
            if "docdesk-status" in p["message"]:
                pr_seen = True
        time.sleep(3)
    if not pr_seen:
        stamp("doc-fixer did not report in time")
        return 1
    upd = wait_thread(c, q["id"], me, chk["create_at"], 60, contains="Update on doc gap")
    if upd:
        stamp(f"back in the original #acme-help thread:\n{upd['message']}")
    stamp(f"loop took {time.time() - t_start:.0f}s")
    return 0 if upd else 1


if __name__ == "__main__":
    raise SystemExit(main())
