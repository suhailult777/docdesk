from agent.fixer import fix_gap, parse_status, pr_title, replace_terms, status_block
from agent.verify import Command, Result


def gap(shop):
    return {"gap": "G-1", "repo": str(shop), "files": ["README.md"],
            "replacements": [["SHOP_PORT", "PORT"], ["npm start", "npm run dev"]], "commits": ["abc1234"]}


def fake_verifier(repo, files):
    """Pretend sandbox: a command passes when it no longer uses the removed names."""
    out = []
    for f in files:
        for line in (repo / f).read_text().splitlines():
            if "npm" in line and not line.startswith("#"):
                ok = "SHOP_PORT" not in line and "npm start" not in line
                out.append(Result(Command(f, line.strip(), True), ok, "" if ok else "Missing script"))
    return out


def test_replace_terms_only_touches_listed_files(shop):
    changed = replace_terms(shop, ["README.md"], [["SHOP_PORT", "PORT"]])
    assert changed == ["README.md"] and "SHOP_PORT" not in (shop / "README.md").read_text()
    assert "SHOP_PORT" not in (shop / "docs/api.md").read_text()  # untouched file stays as it was


def test_fix_gap_falls_back_verifies_and_opens_pr(shop, tmp_path):
    opened = {}

    def opener(workdir, g, branch, title, body, author):
        opened.update(branch=branch, title=title, body=body, diff=(workdir / "README.md").read_text())
        return "https://github.com/o/shop/pull/1"

    r = fix_gap(gap(shop), "Doc gap **G-1** ...", tmp_path / "work", "style", ("t", "t@example.com"),
                oc=None, verifier=fake_verifier, opener=opener)
    assert r.ok and r.pr_url.endswith("/pull/1") and r.files == ["README.md"]
    assert "fallback" in r.editor
    assert opened["branch"].startswith("docfix/g-1-")
    assert "PORT=8080 npm run dev" in opened["diff"]
    assert "Before the fix" in opened["body"] and "docs-owner role" in opened["body"]
    assert pr_title(gap(shop)).startswith("docs: fix doc gap G-1")


def test_fix_gap_blocks_when_a_changed_command_fails(shop, tmp_path):
    g = gap(shop)
    g["replacements"] = [["SHOP_PORT", "PORT"]]  # npm start still broken after this edit
    called = []
    r = fix_gap(g, "", tmp_path / "work", "", ("t", "t@example.com"), oc=None,
                verifier=fake_verifier, opener=lambda *a: called.append(a))
    assert not r.ok and not called
    assert r.verdict.blocking[0].cmd.text == "PORT=8080 npm start"


def test_status_block_roundtrip():
    assert parse_status("done\n" + status_block({"gap": "G-2", "pr": "u"})) == {"gap": "G-2", "pr": "u"}
    assert parse_status("nothing") is None


def test_opencode_is_locked_to_its_checkout():
    import json
    from agent.fixer import EDIT_ONLY
    from agent.llm import READ_ONLY
    edit, read = json.loads(EDIT_ONLY)["permission"], json.loads(READ_ONLY)["permission"]
    assert edit["external_directory"] == "deny" and edit["bash"] == "deny" and edit["edit"] == "allow"
    assert set(read.values()) == {"deny"}


class RewriteLLM:
    name = "openrouter"

    def __init__(self, broken=False):
        self.broken = broken

    def complete(self, system, prompt):
        text = prompt.split("File README.md:\n", 1)[1]
        if self.broken:
            return "Sure! Here you go."
        return "```markdown\n" + text.replace("SHOP_PORT", "PORT").replace("npm start", "npm run dev") + "```"


def test_fix_gap_uses_the_chat_model_first(shop, tmp_path):
    r = fix_gap(gap(shop), "", tmp_path / "work", "", ("t", "t@example.com"), oc=None, llm=RewriteLLM(),
                verifier=fake_verifier, opener=lambda *a: "https://github.com/o/shop/pull/2")
    assert r.ok and r.editor == "openrouter"


def test_bad_rewrite_falls_back_to_replacement(shop, tmp_path):
    r = fix_gap(gap(shop), "", tmp_path / "work", "", ("t", "t@example.com"), oc=None, llm=RewriteLLM(broken=True),
                verifier=fake_verifier, opener=lambda *a: "https://github.com/o/shop/pull/3")
    assert r.ok and "fallback" in r.editor


def test_pr_body_never_pings_github_users():
    from agent.fixer import no_github_mentions
    assert no_github_mentions("Correction by @user, for @docs-owner; mail a@b.c; `@x`") == \
        "Correction by `@user`, for `@docs-owner`; mail a@b.c; `@x`"


def test_replace_terms_longest_first(tmp_path):
    (tmp_path / "a.md").write_text("GET /api/v1/products and POST /api/v1/orders\n")
    replace_terms(tmp_path, ["a.md"], [["/api/v1", "/api"], ["/api/v1/orders", "/api/checkout"]])
    assert (tmp_path / "a.md").read_text() == "GET /api/products and POST /api/checkout\n"
