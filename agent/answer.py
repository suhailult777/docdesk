"""Turn a question into a short, cited answer."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import ChainLLM, LLMError
from .rag import Hit, RepoIndex

SYSTEM = """You are DocDesk, a teammate in a team chat. You answer questions about one
software repo using only the numbered sources you are given (docs and code).
Rules:
- Answer in at most 6 short lines or bullets. Plain language.
- After each claim put the source number in square brackets, like [1].
- Prefer what the docs say, and quote exact commands, paths and variable names.
- If the sources do not answer the question, say "I don't know from the repo" and stop.
- Never invent files, commands or numbers."""

MIN_CONFIDENCE = 0.34


@dataclass
class Answer:
    text: str
    hits: list[Hit]
    confidence: float
    provider: str | None


def build_prompt(question: str, hits: list[Hit], max_chars: int = 1800) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] {h.chunk.path}:{h.chunk.start}-{h.chunk.end}\n{h.chunk.text[:max_chars]}\n")
    return "Sources:\n" + "\n".join(parts) + f"\nQuestion: {question}\n"


def format_reply(body: str, hits: list[Hit], index: RepoIndex, confidence: float) -> str:
    used = sorted({int(n) for n in re.findall(r"\[(\d+)\]", body) if 0 < int(n) <= len(hits)})
    if not used:
        used = list(range(1, min(len(hits), 2) + 1))
    cites = []
    for n in used:
        c = hits[n - 1].chunk
        cites.append(f"[{n}] [`{c.path}:{c.start}-{c.end}`]({index.link(c)})")
    footer = f"Sources (commit `{index.sha[:7]}`, confidence {int(confidence * 100)}%): " + "  ".join(cites)
    return f"{body.strip()}\n\n{footer}"


def answer(question: str, index: RepoIndex, llm: ChainLLM, k: int = 4) -> Answer:
    hits = index.search(question, k)
    conf = index.confidence(hits, question)
    if not hits or conf < MIN_CONFIDENCE:
        return Answer(
            "I don't know that from the repo yet, so I won't guess. "
            "If you know the answer, reply here and I'll note it as a doc gap.",
            hits, conf, None,
        )
    try:
        body = llm.complete(SYSTEM, build_prompt(question, hits))
    except LLMError:
        body = "My language model is unavailable right now. The closest sources are below."
    return Answer(format_reply(body, hits, index, conf), hits, conf, llm.last_used)


def strip_mentions(text: str) -> str:
    return re.sub(r"@[\w.-]+", "", text).strip()
