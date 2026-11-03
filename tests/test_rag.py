from pathlib import Path

from agent.answer import answer, format_reply, strip_mentions
from agent.llm import ChainLLM, FakeLLM, LLMError, make_llm
from agent.rag import RepoIndex, chunk_markdown, tokenize


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("# shop\n\n## Run it locally\n\nRun `npm start`. Set `SHOP_PORT` to change the port.\n")
    (tmp_path / "docs" / "api.md").write_text("# API\n\n| GET | `/api/v1/products` | list products |\n")
    (tmp_path / "server.js").write_text("const port = Number(process.env.PORT || 3000);\n")
    return tmp_path


def test_tokenize_splits_identifiers():
    toks = tokenize("process.env.SHOP_PORT getUserName")
    assert {"shop_port", "shop", "port", "getusername", "get", "user", "name"} <= set(toks)


def test_markdown_chunks_keep_line_numbers():
    chunks = chunk_markdown("a.md", "# A\nx\n## B\ny\nz\n")
    assert [(c.start, c.end) for c in chunks] == [(1, 2), (3, 5)]


def test_search_and_citation_links(tmp_path):
    ix = RepoIndex.from_repo(make_repo(tmp_path), "https://github.com/o/r")
    hits = ix.search("how do I run it locally?")
    assert hits[0].chunk.path == "README.md"
    assert ix.link(hits[0].chunk).startswith(f"https://github.com/o/r/blob/{ix.sha}/README.md#L")
    port_hits = {h.chunk.path for h in ix.search("which env var sets the port")}
    assert {"server.js", "README.md"} <= port_hits


def test_answer_with_fake_llm_cites_sources(tmp_path):
    ix = RepoIndex.from_repo(make_repo(tmp_path), "https://github.com/o/r")
    a = answer("how do I run the shop locally?", ix, ChainLLM([FakeLLM()]))
    assert "npm start" in a.text
    assert "Sources (commit" in a.text and "README.md" in a.text
    assert a.provider == "fake"


def test_unknown_question_does_not_guess(tmp_path):
    ix = RepoIndex.from_repo(make_repo(tmp_path))
    a = answer("what is the kubernetes helm chart rollout strategy?", ix, ChainLLM([FakeLLM()]))
    assert "don't know" in a.text and a.provider is None


def test_format_reply_only_lists_used_citations(tmp_path):
    ix = RepoIndex.from_repo(make_repo(tmp_path), "https://github.com/o/r")
    hits = ix.search("run locally port")
    out = format_reply("Run `npm start` [1].", hits, ix, 0.9)
    assert "[1] [`" in out and "[2] [`" not in out and "confidence 90%" in out


def test_strip_mentions():
    assert strip_mentions("@docdesk how do I run it?") == "how do I run it?"


class Boom:
    name = "boom"

    def complete(self, system, prompt):
        raise LLMError("rate limited")


def test_chain_falls_back():
    chain = ChainLLM([Boom(), FakeLLM()])
    out = chain.complete("s", "Sources:\n[1] a.md\nhello world\n\nQuestion: hello\n")
    assert chain.last_used == "fake" and "[1]" in out


def test_make_llm_picks_by_keys():
    assert make_llm({"LLM_PROVIDER": "fake"}).name == "fake"
    chain = make_llm({"GEMINI_API_KEY": "x", "GROQ_API_KEY": "y"})
    assert chain.name.startswith("gemini+groq")
