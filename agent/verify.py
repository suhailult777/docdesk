"""Run the commands a markdown doc tells people to run, in a sandbox.

A doc fix only reaches a human if its commands work. This module:

* extracts runnable commands from markdown: fenced ``bash``/``sh`` blocks
  (joining ``\\`` continuations) and inline spans such as ``npm run dev``,
* runs them in order inside ``docker run --rm --network none`` on a copy of
  the repo, with no network, capped memory and processes, all caps dropped,
* treats server commands (``npm start``, ``npm run dev``, ``node x.js``) as
  passing only when something answers on the port the command sets
  (``PORT=8080 npm run dev`` must listen on 8080), and makes ``curl`` fail
  on HTTP errors (a 404 is a failure, not a success),
* compares before and after a fix: every changed command must pass and
  nothing that passed before may fail.

``python -m agent.verify REPO FILE...`` prints a report.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IMAGE = "docdesk-verify:node20"
FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.S | re.M)
RUNNABLE_INLINE = re.compile(
    r"`((?:[A-Z][A-Z0-9_]*=\S+\s+)*(?:npm (?:install|test|start|run [\w:-]+)|node [\w./-]+\.js))`"
)
SERVER = re.compile(r"^(?:[A-Z][A-Z0-9_]*=\S+\s+)*(?:npm (?:start|run (?:dev|serve|start))|node [\w./-]*server[\w./-]*\.js)\b")
PORT_ENV = re.compile(r"\b\w*PORT=(\d+)")
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh", ""}


@dataclass
class Command:
    file: str
    text: str
    server: bool


@dataclass
class Result:
    cmd: Command
    ok: bool
    detail: str

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        d = f": {self.detail}" if self.detail and not self.ok else ""
        return f"{mark} `{self.cmd.text}` ({self.cmd.file}){d}"


def extract_commands(markdown: str, file: str = "") -> list[Command]:
    """Commands in document order, fenced blocks and runnable inline spans."""
    found: list[tuple[int, str]] = []
    fenced_spans = []
    for m in FENCE.finditer(markdown):
        fenced_spans.append((m.start(), m.end()))
        if m.group(1).lower() not in SHELL_LANGS:
            continue
        body = re.sub(r"\\\n\s*", " ", m.group(2))
        for i, line in enumerate(body.splitlines()):
            line = line.strip()
            if line.startswith("$ "):
                line = line[2:]
            if line and not line.startswith("#"):
                found.append((m.start() + i, " ".join(line.split())))
    for m in RUNNABLE_INLINE.finditer(markdown):
        if any(a <= m.start() < b for a, b in fenced_spans):
            continue
        found.append((m.start(), m.group(1)))
    found.sort(key=lambda x: x[0])
    return [Command(file, t, bool(SERVER.match(t))) for _, t in found]


def port_of(cmd: str, default: int = 3000) -> int:
    m = PORT_ENV.search(cmd)
    return int(m.group(1)) if m else default


def build_script(cmds: list[Command], default_port: int = 3000) -> str:
    """One sh script that runs every command and prints ``### rc i code`` markers."""
    out = [
        "cp -r /src /tmp/w && cd /tmp/w || exit 99",
        "export npm_config_audit=false npm_config_fund=false npm_config_update_notifier=false",
        "export npm_config_cache=/tmp/npm-cache npm_config_offline=true",
        "printf 'fail-with-body\\nsilent\\nshow-error\\nmax-time = 10\\n' > \"$HOME/.curlrc\"",
    ]
    for i, c in enumerate(cmds):
        q = shlex.quote(c.text)
        if c.server:
            port = port_of(c.text, default_port)
            out += [
                f"[ -n \"${{PID_{port}:-}}\" ] && kill $PID_{port} 2>/dev/null && sleep 0.3",
                f"sh -c {q} > /tmp/o{i}.log 2>&1 & PID_{port}=$!",
                "rc=1; for t in $(seq 1 24); do",
                f"  curl -q -s -o /dev/null --max-time 1 http://127.0.0.1:{port}/ && rc=0 && break",
                f"  kill -0 $PID_{port} 2>/dev/null || break; sleep 0.5",
                "done",
                f"[ $rc -ne 0 ] && echo 'nothing answered on port {port}' >> /tmp/o{i}.log",
            ]
        else:
            out.append(f"timeout 30 sh -c {q} > /tmp/o{i}.log 2>&1; rc=$?")
        out.append(f"echo \"### rc {i} $rc\"; {{ grep -iE 'missing script|error:|cannot find|curl: [(]|nothing answered' /tmp/o{i}.log | head -n 2; tail -n 3 /tmp/o{i}.log; }} | sed 's/^/### out {i} /'")
    out.append("kill $(jobs -p) 2>/dev/null; exit 0")
    return "\n".join(out)


def docker_cmd() -> list[str]:
    return shlex.split(os.environ.get("DOCDESK_DOCKER") or "docker")


def run_in_sandbox(repo: Path, cmds: list[Command], image: str = IMAGE, timeout: int = 240) -> list[Result]:
    if not cmds:
        return []
    script = build_script(cmds)
    argv = docker_cmd() + [
        "run", "--rm", "--network", "none", "--memory", "512m", "--pids-limit", "256",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{Path(repo).resolve()}:/src:ro", image, "sh", "-c", script,
    ]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [Result(c, False, "sandbox timed out") for c in cmds]
    results = parse_output(p.stdout + p.stderr, cmds)
    if p.returncode != 0 and not any("### rc" in ln for ln in p.stdout.splitlines()):
        err = (p.stderr.strip().splitlines() or ["unknown error"])[-1][:160]
        results = [Result(r.cmd, False, f"sandbox error: {err}") for r in results]
    return results


def parse_output(out: str, cmds: list[Command]) -> list[Result]:
    rcs: dict[int, int] = {}
    tails: dict[int, list[str]] = {}
    for line in out.splitlines():
        m = re.match(r"### rc (\d+) (\d+)", line)
        if m:
            rcs[int(m.group(1))] = int(m.group(2))
            continue
        m = re.match(r"### out (\d+) (.*)", line)
        if m:
            tails.setdefault(int(m.group(1)), []).append(m.group(2).strip())
    results = []
    for i, c in enumerate(cmds):
        rc = rcs.get(i)
        tail = [t for t in tails.get(i, []) if t]
        loud = [t for t in tail if re.search(r"(?i)error|missing|not found|cannot|refused", t)]
        detail = (loud or tail or [""])[0][:160]
        if rc is None:
            results.append(Result(c, False, "did not run (sandbox error)"))
        else:
            results.append(Result(c, rc == 0, detail if rc else ""))
    return results


def verify_files(repo: Path, files: list[str]) -> list[Result]:
    """Each file runs in its own fresh sandbox, like a new reader following it."""
    results: list[Result] = []
    for f in files:
        path = Path(repo) / f
        if path.exists():
            results += run_in_sandbox(repo, extract_commands(path.read_text(), f))
    return results


@dataclass
class Verdict:
    ok: bool
    blocking: list[Result]
    known_failing: list[Result]
    after: list[Result]

    def report(self) -> str:
        n_ok = sum(r.ok for r in self.after)
        lines = [f"Sandbox (docker, --network none): {n_ok} of {len(self.after)} commands pass after the fix."]
        lines += [f"- {r.line()}" for r in self.after]
        if self.known_failing:
            lines.append("Failing before and after, untouched by this fix (another gap): "
                         + ", ".join(f"`{r.cmd.text}`" for r in self.known_failing))
        return "\n".join(lines)


def compare(before: list[Result], after: list[Result]) -> Verdict:
    passed_before = {(r.cmd.file, r.cmd.text) for r in before if r.ok}
    seen_before = {(r.cmd.file, r.cmd.text) for r in before}
    blocking, known = [], []
    for r in after:
        k = (r.cmd.file, r.cmd.text)
        if r.ok:
            continue
        if k not in seen_before or k in passed_before:
            blocking.append(r)  # a changed command that fails, or a regression
        else:
            known.append(r)
    return Verdict(not blocking and bool(after), blocking, known, after)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a doc's commands in a network-less sandbox")
    ap.add_argument("repo")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    results = verify_files(Path(a.repo), a.files)
    for r in results:
        print(r.line())
    return 0 if results and all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
