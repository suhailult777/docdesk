"""Put the demo back to its starting point, in one command (`make reset-demo`).

Touches only the demo repo (DOCDESK_REPO_URL, default suhailult777/acme-shop-demo)
and DocDesk's local state:

1. closes every open PR whose branch starts with docfix/ and deletes those branches,
2. restores the stale docs (README.md, docs/*.md) from the baseline commit
   (DOCDESK_DEMO_BASELINE, default 3df5b73) with a normal new commit on main
   (no force push), if anything changed,
3. clears gaps.json, threads.json and fixer_prs.json in the state dir. DocDesk
   rewrites the (now empty) doc-gap-ledger room document when it starts.

Restart both agents afterwards (`make run`, `make run-fixer`).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import Settings  # noqa: E402

DOCS = ["README.md", "docs"]


def sh(*argv: str, cwd: Path | None = None, check: bool = True) -> str:
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=180)
    if check and p.returncode != 0:
        raise SystemExit(f"{' '.join(argv[:3])} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def main() -> int:
    s = Settings.from_env()
    repo = s.repo_url.removeprefix("https://github.com/").removesuffix(".git").rstrip("/")
    baseline = s.env.get("DOCDESK_DEMO_BASELINE") or "3df5b73"
    name = s.env.get("DOCDESK_GIT_NAME") or "docdesk-fixer"
    email = s.env.get("DOCDESK_GIT_EMAIL") or "docdesk-fixer@users.noreply.github.com"

    prs = json.loads(sh("gh", "pr", "list", "--repo", repo, "--state", "open", "--json", "number,headRefName"))
    for pr in prs:
        if pr["headRefName"].startswith("docfix/"):
            sh("gh", "pr", "close", str(pr["number"]), "--repo", repo, "--delete-branch",
               "--comment", "Closed by the DocDesk demo reset.")
            print(f"closed PR #{pr['number']} ({pr['headRefName']})")
    refs = json.loads(sh("gh", "api", f"repos/{repo}/git/matching-refs/heads/docfix/") or "[]")
    for r in refs:
        sh("gh", "api", "-X", "DELETE", f"repos/{repo}/git/{r['ref']}", check=False)
        print(f"deleted branch {r['ref'].removeprefix('refs/heads/')}")

    with tempfile.TemporaryDirectory() as tmp:
        w = Path(tmp) / "repo"
        sh("git", "clone", "-q", f"https://github.com/{repo}", str(w))
        sh("git", "checkout", "-q", baseline, "--", *DOCS, cwd=w)
        if sh("git", "status", "--porcelain", cwd=w).strip():
            sh("git", "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-am",
               f"demo reset: restore the stale docs from {baseline}", cwd=w)
            sh("git", "push", "-q", "origin", "HEAD:main", cwd=w)
            print(f"restored the stale docs from {baseline} on main")
        else:
            print("docs already match the baseline")

    if (s.repo_dir / ".git").exists():
        sh("git", "-C", str(s.repo_dir), "pull", "--ff-only", "-q", check=False)
    for f in ("gaps.json", "threads.json", "fixer_prs.json"):
        (s.state_dir / f).unlink(missing_ok=True)
    print("cleared the gap ledger and thread memory; restart both agents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
