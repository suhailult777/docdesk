import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit(repo: Path, files: dict[str, str], msg: str) -> None:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    """A tiny repo where the docs went stale in three commits, like acme-shop-demo."""
    repo = tmp_path / "shop"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    commit(repo, {
        "README.md": "# shop\n\nRun it:\n\n```bash\nnpm start\n```\n\nSet `SHOP_PORT` to change the port:\n\n"
                     "```bash\nSHOP_PORT=8080 npm start\n```\n",
        "docs/api.md": "| POST | `/api/v1/orders` | place an order |\n",
        "package.json": '{\n  "scripts": {\n    "start": "node src/server.js"\n  }\n}\n',
        "src/server.js": "const port = process.env.SHOP_PORT || 3000;\n",
        "src/app.js": 'if (path === "/api/v1/orders") {}\n',
    }, "initial shop")
    commit(repo, {"src/server.js": "const port = process.env.PORT || 3000;\n"},
           "read PORT instead of SHOP_PORT")
    commit(repo, {"package.json": '{\n  "scripts": {\n    "dev": "node --watch src/server.js"\n  }\n}\n'},
           "rename start script to dev")
    commit(repo, {"src/app.js": 'if (path === "/api/checkout") {}\n',
                  "test/app.test.js": 'expect404("/api/v1/orders");\n'},
           "move orders to /api/checkout")
    return repo
