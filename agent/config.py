"""Settings from the environment, with an optional env file.

DOCDESK_ENV_FILE points at a file outside the repo (so keys never sit in the
working tree); otherwise ``.env`` in the repo root is read if present.
Values already in the real environment win.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_env() -> dict[str, str]:
    path = Path(os.environ.get("DOCDESK_ENV_FILE") or ROOT / ".env")
    env = read_env_file(path)
    env.update({k: v for k, v in os.environ.items()})
    return env


@dataclass
class Settings:
    switch_api: str
    registration_key: str | None
    agent_name: str
    teammate: str
    repo_url: str
    repo_dir: Path
    state_dir: Path
    env: dict[str, str]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = load_env() if env is None else env
        repo_dir = Path(env.get("DOCDESK_REPO_DIR") or "demo/acme-shop")
        if not repo_dir.is_absolute():
            repo_dir = ROOT / repo_dir
        state = Path(env.get("DOCDESK_STATE_DIR") or ROOT / ".state")
        return cls(
            switch_api=(env.get("SWITCH_API") or "http://localhost:8000").rstrip("/"),
            registration_key=env.get("SWITCH_REGISTRATION_KEY") or None,
            agent_name=env.get("DOCDESK_AGENT_NAME") or "docdesk",
            teammate=env.get("DOCDESK_TEAMMATE") or "user",
            repo_url=env.get("DOCDESK_REPO_URL") or "https://github.com/suhailult777/acme-shop-demo",
            repo_dir=repo_dir,
            state_dir=state,
            env=env,
        )

    @property
    def creds_path(self) -> Path:
        return self.state_dir / f"{self.agent_name}.agent.json"
