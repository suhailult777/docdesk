"""Create a Switch registration key and save it to the DocDesk env file.

Logs in to the Gateway as the admin from the stack's .env, creates an API
key of type "registration" (POST /gateway/api-keys) and appends it as
SWITCH_REGISTRATION_KEY to DOCDESK_ENV_FILE (or .env). Prints no secret.

Why not the bootstrap AGENT_REGISTRATION_TOKEN: an agent registered with it
belongs to a separate non-admin account, so it refuses the human teammate.

  python scripts/registration_key.py --stack-env .switch-stack/.env
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import ROOT, read_env_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack-env", default=os.environ.get("SWITCH_STACK_ENV", ".switch-stack/.env"))
    ap.add_argument("--api", default=os.environ.get("SWITCH_API", "http://localhost:8000"))
    args = ap.parse_args()
    target = Path(os.environ.get("DOCDESK_ENV_FILE") or ROOT / ".env")
    if read_env_file(target).get("SWITCH_REGISTRATION_KEY"):
        print(f"{target} already has SWITCH_REGISTRATION_KEY, nothing to do")
        return 0
    stack = read_env_file(Path(args.stack_env))
    with httpx.Client(base_url=args.api, timeout=30) as c:
        r = c.post("/gateway/auth/login", json={
            "email": stack["GATEWAY_ADMIN_EMAIL"], "password": stack["GATEWAY_ADMIN_PASSWORD"]})
        r.raise_for_status()
        r = c.post("/gateway/api-keys", json={"label": "docdesk registration"})
        r.raise_for_status()
        key = r.json()["key"]
    with target.open("a") as f:
        f.write(f"\nSWITCH_REGISTRATION_KEY={key}\n")
    os.chmod(target, 0o600)
    print(f"saved a new registration key to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
