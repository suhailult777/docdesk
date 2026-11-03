"""A small retrieval index over one git repo, docs and code together.

Every chunk keeps its file path, line range and the commit it was read at,
so an answer can cite ``docs/api.md:7-12@3df5b73`` and link straight to the
lines on GitHub.

Ranking is BM25 over words plus code identifiers split on case and
underscores (``SHOP_PORT`` also matches ``port``). It needs no model, no key
and no network, so tests and judges get the same results every run.
"""
from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

TEXT_EXT = {".md", ".js", ".ts", ".json", ".py", ".yml", ".yaml", ".toml", ".sh", ".txt"}
SKIP_DIRS = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}
STOP = set(
    "a an and are as at be by can do does for from how i in is it of on or our "
    "the this to we what when where which who why with you your me my".split()
)


@dataclass
class Chunk:
    path: str
    start: int
    end: int
    text: str
    kind: str  # "doc" or "code"

    def ref(self, sha: str) -> str:
        return f"{self.path}:{self.start}-{self.end}@{sha[:7]}"


@dataclass
class Hit:
    chunk: Chunk
    score: float


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for word in re.findall(r"[A-Za-z0-9_]+", text):
        low = word.lower()
        parts = [p.lower() for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", word) if p]
        for t in {low, *parts}:
            if len(t) > 1 and t not in STOP:
                out.append(t)
    return out


def chunk_markdown(path: str, text: str) -> list[Chunk]:
    """Split on headings so each chunk is one section."""
    lines = text.splitlines()
    chunks: list[Chunk] = []
    start = 0
    for i, line in enumerate(lines):
        if i > start and re.match(r"^#{1,4} ", line):
            chunks.append(Chunk(path, start + 1, i, "\n".join(lines[start:i]), "doc"))
            start = i
    if start < len(lines):
        chunks.append(Chunk(path, start + 1, len(lines), "\n".join(lines[start:]), "doc"))
    return [c for c in chunks if c.text.strip()]


def chunk_code(path: str, text: str, size: int = 40, overlap: int = 10) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    i = 0
    while i < len(lines):
        j = min(i + size, len(lines))
        body = "\n".join(lines[i:j])
        if body.strip():
            chunks.append(Chunk(path, i + 1, j, body, "code"))
        if j == len(lines):
            break
        i = j - overlap
    return chunks


def git_sha(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "0000000"


class RepoIndex:
    def __init__(self, chunks: list[Chunk], sha: str, repo_url: str | None = None):
        self.chunks = chunks
        self.sha = sha
        self.repo_url = (repo_url or "").rstrip("/").removesuffix(".git")
        self._tfs = [Counter(tokenize(c.path + "\n" + c.text)) for c in chunks]
        self._lens = [sum(tf.values()) for tf in self._tfs]
        self._avg = (sum(self._lens) / len(self._lens)) if self._lens else 1.0
        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def from_repo(cls, repo: Path, repo_url: str | None = None) -> "RepoIndex":
        repo = Path(repo)
        chunks: list[Chunk] = []
        for f in sorted(repo.rglob("*")):
            rel = f.relative_to(repo)
            if any(p in SKIP_DIRS for p in rel.parts) or not f.is_file():
                continue
            if f.suffix not in TEXT_EXT and f.name not in {"LICENSE", "Makefile", "Dockerfile"}:
                continue
            if f.name in {"LICENSE", "package-lock.json"}:
                continue
            text = f.read_text(errors="replace")
            if f.suffix == ".md":
                chunks.extend(chunk_markdown(rel.as_posix(), text))
            else:
                chunks.extend(chunk_code(rel.as_posix(), text))
        return cls(chunks, git_sha(repo), repo_url)

    def search(self, query: str, k: int = 5, k1: float = 1.5, b: float = 0.75) -> list[Hit]:
        q = tokenize(query)
        hits: list[Hit] = []
        for i, tf in enumerate(self._tfs):
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                f = tf[t]
                s += self._idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * self._lens[i] / self._avg))
            if s > 0:
                hits.append(Hit(self.chunks[i], s))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def link(self, chunk: Chunk) -> str:
        if not self.repo_url:
            return chunk.ref(self.sha)
        return f"{self.repo_url}/blob/{self.sha}/{chunk.path}#L{chunk.start}-L{chunk.end}"

    def confidence(self, hits: list[Hit], query: str) -> float:
        """0..1: how much of the question the best chunks cover."""
        if not hits:
            return 0.0
        q = set(tokenize(query))
        if not q:
            return 0.0
        covered = set()
        for h in hits[:3]:
            covered |= q & set(tokenize(h.chunk.path + " " + h.chunk.text))
        return round(len(covered) / len(q), 2)
