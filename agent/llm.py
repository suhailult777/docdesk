"""LLM providers behind one tiny interface, all free to use.

* ``gemini``: Google AI Studio free tier, needs GEMINI_API_KEY
* ``groq``: Groq free tier (OpenAI-compatible API), needs GROQ_API_KEY
* ``opencode``: the OpenCode CLI with one of its free models, no key
* ``fake``: deterministic, offline, used by tests and as the last fallback

``make_llm`` builds a chain from the environment. If one provider fails
(rate limit, timeout, missing binary) the next one answers.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Protocol

import httpx

log = logging.getLogger("docdesk.llm")


class LLM(Protocol):
    name: str

    def complete(self, system: str, prompt: str) -> str: ...


class LLMError(RuntimeError):
    pass


class GeminiLLM:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-flash-latest", http: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.http = http or httpx.Client(timeout=60)

    def complete(self, system: str, prompt: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        # Key goes in a header, never in the URL, so it can't leak into logs.
        r = self.http.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        if r.status_code >= 400:
            raise LLMError(f"gemini {r.status_code}: {r.text[:200]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()


class GroqLLM:
    name = "groq"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile", http: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.http = http or httpx.Client(timeout=60)

    def complete(self, system: str, prompt: str) -> str:
        r = self.http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": self.model,
                "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if r.status_code >= 400:
            raise LLMError(f"groq {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"].strip()


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class OpenCodeLLM:
    """Runs ``opencode run -m <free model>``; no account or key needed."""

    name = "opencode"

    def __init__(self, model: str = "opencode/deepseek-v4.1-flash-free", binary: str | None = None, timeout: int = 60):
        self.model = model
        self.binary = binary or shutil.which("opencode") or os.path.expanduser("~/.local/bin/opencode")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.binary) and os.path.exists(self.binary)

    def complete(self, system: str, prompt: str) -> str:
        if not self.available():
            raise LLMError("opencode CLI not found")
        text = f"{system}\n\n{prompt}\n\nReply with the answer only. Do not run tools or edit files."
        try:
            p = subprocess.run(
                [self.binary, "run", "-m", self.model, text],
                capture_output=True, text=True, timeout=self.timeout, cwd="/tmp",
                stdin=subprocess.DEVNULL,  # opencode waits on an open stdin when run in the background
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError("opencode timed out") from e
        out = ANSI.sub("", p.stdout).strip()
        if p.returncode != 0 or not out:
            raise LLMError(f"opencode exit {p.returncode}: {ANSI.sub('', p.stderr)[:200]}")
        return out


class FakeLLM:
    """Offline stand-in. Quotes the most relevant lines of the top sources.

    The prompt built by ``answer.build_prompt`` lists sources as ``[n] path``
    followed by their text, so this can stay honest: it only repeats what
    the sources say and cites them by number.
    """

    name = "fake"

    def complete(self, system: str, prompt: str) -> str:
        q = prompt.split("Question:", 1)[-1].strip().splitlines()[0] if "Question:" in prompt else ""
        qwords = {w for w in re.findall(r"[a-z0-9_]+", q.lower()) if len(w) > 2}
        src_text = prompt.split("\nQuestion:", 1)[0]
        sources = re.findall(r"^\[(\d+)\] (\S+)\n(.*?)(?=^\[\d+\] |\Z)", src_text, flags=re.S | re.M)
        lines: list[str] = []
        for num, _path, body in sources[:3]:
            best = sorted(
                (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith(("```", "#", "|---"))),
                key=lambda ln: -len(qwords & set(re.findall(r"[a-z0-9_]+", ln.lower()))),
            )
            if best:
                lines.append(f"- {best[0]} [{num}]")
        if not lines:
            return "I could not find this in the repo, so I won't guess."
        return "Here is what the repo says (offline mode, no LLM key yet):\n" + "\n".join(lines)


class ChainLLM:
    def __init__(self, providers: list[LLM]):
        if not providers:
            providers = [FakeLLM()]
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.last_used: str | None = None

    def complete(self, system: str, prompt: str) -> str:
        errors = []
        for p in self.providers:
            try:
                out = p.complete(system, prompt)
                self.last_used = p.name
                return out
            except Exception as e:  # noqa: BLE001
                log.warning("provider %s failed: %s", p.name, e)
                errors.append(f"{p.name}: {e}")
        raise LLMError("; ".join(errors))


def make_llm(env: dict[str, str] | None = None) -> ChainLLM:
    env = dict(os.environ if env is None else env)
    choice = (env.get("LLM_PROVIDER") or "auto").lower()
    gemini = env.get("GEMINI_API_KEY")
    groq = env.get("GROQ_API_KEY")
    oc = OpenCodeLLM(env.get("OPENCODE_MODEL") or "opencode/deepseek-v4.1-flash-free")
    built = {
        "gemini": (lambda: GeminiLLM(gemini, env.get("GEMINI_MODEL") or "gemini-flash-latest")) if gemini else None,
        "groq": (lambda: GroqLLM(groq, env.get("GROQ_MODEL") or "llama-3.3-70b-versatile")) if groq else None,
        "opencode": (lambda: oc) if oc.available() else None,
        "fake": FakeLLM,
    }
    if choice == "auto":
        order = ["gemini", "groq", "opencode", "fake"]
    elif choice in built:
        order = [choice, "fake"] if choice != "fake" else ["fake"]
    else:
        raise ValueError(f"unknown LLM_PROVIDER {choice!r}")
    return ChainLLM([built[n]() for n in order if built[n]])
