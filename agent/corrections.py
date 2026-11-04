"""Turn a teammate's correction into checked evidence, or refuse to.

DocDesk never trusts a correction on its word. When someone replies in a
thread DocDesk answered and says the answer is wrong, this module:

1. decides whether the reply is a correction (keywords, then optionally an LLM),
2. pulls out the terms it is about: the new ones the teammate says are right
   (``PORT``, ``npm run dev``, ``/api/checkout``) and the old ones that are
   now wrong (``SHOP_PORT``, ``npm start``),
3. checks the code: new terms must exist in code files, old ones must be gone
   from code, and ``git log -S`` names the commit that changed it,
4. lists the docs that still mention the old terms: those are the files to fix.

Everything here is plain git and regex, so tests need no network or model.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

CORRECTION_PATTERNS = [
    r"\boutdated\b", r"\bout of date\b", r"\bstale\b", r"\bnot anymore\b", r"\bno longer\b",
    r"\bthat'?s (?:wrong|old|not right|incorrect)\b", r"\bwrong\b", r"\bincorrect\b",
    r"\bactually\b", r"\bwe (?:now )?use\b", r"\bit'?s now\b", r"\brenamed\b", r"\bdoesn'?t work\b",
    r"\binstead of\b", r"\bdeprecated\b", r"\bremoved\b", r"\bmoved to\b",
]
# "not X", "instead of X", "no longer X", "used to be X", "X is gone" name the old term.
OLD_MARKERS = re.compile(
    r"(?:\bnot|\binstead of|\bno longer(?: uses?| reads?| supports?)?|\bused to be|\breplaced?|\bremoved|\bdropped)"
    r"\s+(`[^`]+`|[A-Z][A-Z0-9_]{2,}|/[\w/.:-]+|npm (?:run )?[\w:-]+)",
    re.I,
)

DOC_EXT = {".md", ".markdown", ".rst", ".txt"}


@dataclass
class Term:
    text: str
    kind: str  # "env", "npm", "path", "ident"

    def needle(self) -> str:
        """What to look for in code. An npm command is a script key in package.json."""
        if self.kind == "npm":
            return f'"{npm_script(self.text)}":'
        return self.text


@dataclass
class CodeRef:
    path: str
    line: int
    text: str

    def ref(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class Commit:
    sha: str
    subject: str


@dataclass
class Evidence:
    new_terms: list[Term] = field(default_factory=list)
    old_terms: list[Term] = field(default_factory=list)
    code_refs: dict[str, list[CodeRef]] = field(default_factory=dict)  # new term -> where code has it
    old_in_code: dict[str, list[CodeRef]] = field(default_factory=dict)  # old term -> still in code?
    commits: dict[str, Commit] = field(default_factory=dict)  # term -> commit that changed it
    stale_docs: dict[str, list[CodeRef]] = field(default_factory=dict)  # old term -> doc lines

    @property
    def confirmed(self) -> bool:
        has_new = any(self.code_refs.get(t.text) for t in self.new_terms)
        old_gone = all(not self.old_in_code.get(t.text) for t in self.old_terms)
        return has_new and old_gone

    @property
    def files(self) -> list[str]:
        return sorted({r.path for refs in self.stale_docs.values() for r in refs})

    def replacements(self) -> list[tuple[str, str]]:
        """Old -> new pairs of the same kind, in the order the teammate wrote them."""
        pairs = []
        news = list(self.new_terms)
        for old in self.old_terms:
            for n in news:
                if n.kind == old.kind:
                    pairs.append((old.text, n.text))
                    news.remove(n)
                    break
        return pairs

    def summary(self) -> str:
        """One line per fact, used in the reply, the ledger and the handoff."""
        out = []
        for t in self.new_terms:
            refs = self.code_refs.get(t.text) or []
            c = self.commits.get(t.text)
            if refs:
                since = f" since {c.sha} ({c.subject})" if c else ""
                out.append(f"`{refs[0].ref()}` has `{t.text}`{since}")
            else:
                out.append(f"no code file mentions `{t.text}`")
        for t in self.old_terms:
            still = self.old_in_code.get(t.text) or []
            c = self.commits.get(t.text)
            if still:
                out.append(f"`{t.text}` is still in code at `{still[0].ref()}`")
            else:
                gone = f", removed in {c.sha}" if c else ""
                out.append(f"`{t.text}` is gone from the code{gone}")
        return "; ".join(out)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def npm_script(cmd: str) -> str:
    parts = cmd.split()
    if len(parts) >= 3 and parts[1] == "run":
        return parts[2]
    return parts[1] if len(parts) > 1 else ""


def looks_like_correction(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in CORRECTION_PATTERNS)


def classify_term(raw: str) -> Term | None:
    t = raw.strip().strip("`").strip().rstrip(".,;:!?)")
    t = re.sub(r"^(?:GET|POST|PUT|PATCH|DELETE)\s+", "", t)
    if not t or len(t) < 2:
        return None
    if re.fullmatch(r"npm (?:run )?[\w:-]+", t):
        if t.split()[1] == "run" or t.split()[1] in {"start", "test"}:
            return Term(t, "npm")
        return None  # npm install etc. are not scripts
    if t.startswith("/") and re.fullmatch(r"/[\w/.:-]+", t):
        return Term(t, "path")
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t):
        return Term(t, "env")
    m = re.fullmatch(r"(?:[A-Z][A-Z0-9_]*=\S+\s+)?(npm (?:run )?[\w:-]+)", t)
    if m:
        return classify_term(m.group(1))
    if re.fullmatch(r"[A-Za-z_][\w.-]{2,}", t) and not t.islower():
        return Term(t, "ident")
    return None


IGNORED = {"GET", "POST", "PUT", "DELETE", "PATCH", "API", "JSON", "HTTP", "URL", "README", "PR", "OK"}


def extract_terms(text: str) -> list[Term]:
    """Backticked spans, ALL_CAPS names, URL paths and npm commands, de-duplicated."""
    raw: list[str] = re.findall(r"`([^`]+)`", text)
    plain = re.sub(r"`[^`]+`", " ", text)
    raw += re.findall(r"npm (?:run )?[\w:-]+", plain)
    raw += re.findall(r"(?<![\w.])/[\w-]+(?:/[\w:.-]+)+", plain)
    raw += re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", plain)
    out: list[Term] = []
    seen = set()
    for r in raw:
        t = classify_term(r)
        if t and t.text not in seen and t.text not in IGNORED:
            seen.add(t.text)
            out.append(t)
    return out


def split_terms(correction: str, wrong_answer: str = "") -> tuple[list[Term], list[Term]]:
    """New terms (claimed right) and old terms (claimed wrong).

    Old terms come from "not X" / "instead of X" in the correction, plus
    terms of the same kind that the wrong answer used and the correction
    does not repeat.
    """
    old_raw = [m.group(1) for m in OLD_MARKERS.finditer(correction)]
    old = [t for t in (classify_term(r) for r in old_raw) if t]
    old_texts = {t.text for t in old}
    new = [t for t in extract_terms(correction) if t.text not in old_texts]
    new_texts = {t.text for t in new}
    kinds = {t.kind for t in new}
    for t in extract_terms(wrong_answer):
        if t.kind in kinds and t.text not in new_texts and t.text not in old_texts:
            # A replacement for a term the teammate named: SHOP_PORT when they said PORT.
            if any(_related(t, n) for n in new):
                old.append(t)
                old_texts.add(t.text)
    return new, old


def _related(a: Term, b: Term) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "npm":
        return True
    if a.kind == "env":
        return a.text.endswith(b.text) or b.text.endswith(a.text) or a.text.split("_")[-1] == b.text.split("_")[-1]
    if a.kind == "path":
        return a.text.split("/")[1:2] == b.text.split("/")[1:2]
    return False


# ---- git -------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
    return p.stdout if p.returncode in (0, 1) else ""


def _word_regex(needle: str) -> str:
    esc = re.escape(needle).replace("\\ ", " ")
    left = r"(^|[^A-Za-z0-9_])" if re.match(r"\w", needle) else ""
    right = r"([^A-Za-z0-9_]|$)" if re.search(r"\w$", needle) else ""
    return f"{left}{esc}{right}"


def grep(repo: Path, needle: str, docs: bool) -> list[CodeRef]:
    """Whole-word matches in tracked files, docs only or code only."""
    out = _git(repo, "grep", "-n", "-I", "-E", _word_regex(needle))
    refs = []
    for line in out.splitlines():
        path, num, text = line.split(":", 2)
        is_doc = Path(path).suffix.lower() in DOC_EXT
        if is_doc == docs and path != "package-lock.json":
            refs.append(CodeRef(path, int(num), text.strip()))
    # Prefer src/ over tests for the headline reference.
    refs.sort(key=lambda r: (r.path.startswith("test"), r.path, r.line))
    return refs


def last_change(repo: Path, needle: str) -> Commit | None:
    """The newest commit that added or removed ``needle`` in code: ``git log -S``.

    ``--pickaxe-regex`` with a whole-word regex so ``PORT`` does not match
    inside ``SHOP_PORT``.
    """
    out = _git(
        repo, "log", "--pickaxe-regex", f"-S{_word_regex(needle)}", "--format=%h%x09%s", "-n", "1",
        "--", ".", ":(exclude)*.md",
    )
    line = out.strip().splitlines()[0] if out.strip() else ""
    if not line:
        return None
    sha, subject = line.split("\t", 1)
    return Commit(sha, subject)


def is_test(path: str) -> bool:
    return bool(re.search(r"(^|/)(tests?|spec|__tests__)/|\.(test|spec)\.|_test\.", path))


def gather_evidence(repo: Path, new: list[Term], old: list[Term]) -> Evidence:
    ev = Evidence(new_terms=new, old_terms=old)
    for t in new:
        ev.code_refs[t.text] = grep(repo, t.needle(), docs=False)
        c = last_change(repo, t.needle())
        if c:
            ev.commits[t.text] = c
    for t in old:
        # Tests often assert the old thing is gone (expect 404 on /api/v1), so
        # only non-test code counts as "still there".
        ev.old_in_code[t.text] = [r for r in grep(repo, t.needle(), docs=False) if not is_test(r.path)]
        ev.stale_docs[t.text] = grep(repo, t.text, docs=True)
        c = last_change(repo, t.needle())
        if c:
            ev.commits[t.text] = c
    return ev


def check_correction(repo: Path, correction: str, wrong_answer: str = "") -> Evidence:
    repo = Path(repo)
    new, old = split_terms(correction, wrong_answer)
    # "`npm start` doesn't work, we use `npm run dev`": both look new. The one
    # the code lacks but the docs still have is the old one, when a related
    # term of the same kind is in the code.
    in_code = {t.text: bool(grep(repo, t.needle(), docs=False)) for t in new}
    for t in list(new):
        if in_code[t.text]:
            continue
        if any(in_code[n.text] and _related(t, n) for n in new) and grep(repo, t.text, docs=True):
            new.remove(t)
            old.append(t)
    return gather_evidence(repo, new, old)


def reply_text(ev: Evidence, gap_id: str | None) -> str:
    """What DocDesk says in the help thread after checking."""
    if not ev.new_terms:
        return ("Thanks. I couldn't tell which command, variable or path you mean. "
                "Put it in backticks, for example: it's `PORT` now, not `SHOP_PORT`.")
    if ev.confirmed:
        head = f"You're right. {ev.summary()}."
        if ev.files:
            files = ", ".join(f"`{f}`" for f in ev.files)
            head += f"\nThe docs still say the old thing in {files}."
        if gap_id:
            head += f"\nI logged doc gap **{gap_id}** and handed it to the docs room (#acme-docs-maint) to fix."
        return head
    return (f"I checked the code and could not confirm that: {ev.summary()}. "
            "I haven't filed a doc gap. If the code is about to change, tell me and I'll check again.")
