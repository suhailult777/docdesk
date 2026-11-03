# DocDesk: correct it once, the docs fix themselves

DocDesk is a teammate in your team chat, connected through [Switch](https://github.com/sandbox-quantum/switch).
It answers questions from your repo and cites the file, the lines and the commit. When a teammate corrects it,
DocDesk checks the correction against the code, hands the doc gap to a coding agent in a linked room, and once
a human approves the fix it goes back to the original thread with the corrected answer. Nobody leaves the chat.

Built for the Flip the Switch hackathon, Track 01 (Best Business Workflow). Status: **Day 1 of 4**, see
[Status](#status). A GIF of the full loop will go here.

## The problem

- Teams answer the same questions in chat every week ("how do I run it?", "which env var?").
- The docs that should answer them drift from the code, and nobody notices until someone is stuck.
- When someone does notice, the fix is a chat message, not a pull request, so the docs stay wrong.

## What DocDesk does (the loop)

1. A teammate asks in `#acme-help`. DocDesk answers in the thread with citations to `file:lines@commit`.
2. Someone replies "that's outdated". DocDesk checks the claim against the code and `git log` before it believes it. *(Day 2)*
3. If the code agrees, DocDesk logs a gap in the `doc-gap-ledger` room document and hands it to `@doc-fixer`
   in the linked `#acme-docs-maint` room, with the question, the wrong answer, the correction and the evidence. *(Day 2)*
4. doc-fixer (OpenCode run by Switch Console, free model) edits the doc, runs the doc's own commands in a
   throwaway container and opens a PR. *(Day 2)*
5. The `@docs-owner` approves in chat; Switch records who approved. Nothing merges without it. *(Day 3)*
6. DocDesk re-indexes and replies in the original thread: "Fixed in PR #N, here is the corrected answer." *(Day 3)*

## How Switch is used

| Switch piece | How DocDesk uses it |
|---|---|
| Rooms | `#acme-help` (questions) and `#acme-docs-maint` (fixes), bridged to Mattermost |
| Linked rooms | `acme-help` links to `acme-docs-maint` ("doc gaps"), so gaps travel with their context |
| Custom protocol agent | `docdesk` (this repo) registers over the agent protocol: WebSocket events, HTTP ops |
| Console-run agent | `doc-fixer`, OpenCode with a free model, started by Switch Console *(Day 2)* |
| Role | `docs-owner` in `#acme-docs-maint`, so approval goes to a job, not a person's name |
| Room documents | `doc-gap-ledger` (shared memory of gaps) and `docs-style-guide` (rules the fixer follows) |
| Main agent | `!assign-agent @docdesk` in `#acme-help`, so people can ask without tagging |
| Threads | every answer goes in the question's thread; the loop closes in the same thread |
| Room template | [`switch/docdesk.group.template.yaml`](switch/docdesk.group.template.yaml) creates both rooms, the link, the role and the documents in one step |
| Approvals | permission request before merge, recorded by Switch *(Day 3)* |

**What breaks without Switch:** DocDesk would be one more bot in one channel. The handoff between two
different kinds of agent, the shared ledger both rooms can read, the role-based approval and its audit trail,
and the one-step setup all come from Switch.

## Architecture

```
   Mattermost #acme-help                      Mattermost #acme-docs-maint
   people ask, correct                        @docs-owner, doc-fixer, docdesk
          |                                              |
 +------------------- Switch server 0.31.0 (rooms, link, role, room docs, approvals) --------+
          | WebSocket events, HTTP ops                   | Console sidecar
          v                                              v
   docdesk (Python, agent/)                       doc-fixer (OpenCode via Switch Console)
   client.py  protocol client                     edits docs, runs their commands,
   rag.py     index of docs + code                opens the PR, waits for approval
   answer.py  cited answers
   llm.py     Gemini / Groq / OpenCode / offline
          |
          v
   target repo: github.com/suhailult777/acme-shop-demo
```

### The protocol, in short

- `POST /agents` once with a registration key from the Gateway, keep the returned API key in `.state/`.
- Open `GET /agents/{id}/connection/ws?connection_id=<uuid>&scope=all` with the key as a Bearer token.
- Answer every `ping` with `{"type": "pong", "cursor": N}` before anything else; handlers run as tasks so a slow
  LLM call never delays a pong.
- On a `message` with `payload.addressed` true, call `connect_to_room` then `post_message` with the `thread_id`.
- Save the cursor after every event and reconnect with `start_from` and `expected_generation`; stop on `taken_over`.

Two gotchas we hit on Switch 0.31.0:
- An agent registered with the deployment bootstrap token belongs to a separate non-admin account and refuses
  the human teammate. Use a key from the Gateway API Keys page (`make regkey` makes one).
- `update_agent_detail` with the agent's own key returns 403, although the docs say an agent can set its own
  addressing. Set "who can talk to your agent" in Switch Console or the Gateway instead.

## Run it in 10 minutes

Prerequisites: Docker with Compose, Python 3.11+, `git`, `curl`, `openssl`. Optional: the OpenCode CLI
(`npm i -g opencode-ai`) for a free LLM with no key, or a free Gemini or Groq key.

```bash
git clone https://github.com/suhailult777/docdesk && cd docdesk
make install                 # .venv with DocDesk and test deps
cp .env.example .env         # fill in later steps; leave keys empty to use the free/offline LLMs
make up                      # downloads the pinned Switch 0.31.0 stack into .switch-stack/ and starts it
make regkey                  # creates a registration key in the Gateway, saves it to .env
make setup                   # registers docdesk, creates #acme-help and #acme-docs-maint from the template
make run                     # starts the agent (leave it running)
make demo                    # in a second terminal: asks the seed questions in #acme-help as the teammate
```

Then open Mattermost at http://localhost:8065 (user `user`, password `MATTERMOST_USER_PASSWORD` in
`.switch-stack/.env`), open `acme-help` and ask your own question. Type `!assign-agent @docdesk` once to ask
without tagging.

Pinned versions: Switch server 0.31.0, Mattermost 11.9.0 (from the official compose at tag `switch-v0.31.0`),
Switch Console 0.40.1 for the fixer.

## Verification and safety

- Answers only from retrieved sources, with citations; below a confidence threshold DocDesk says
  "I don't know from the repo" instead of guessing.
- Corrections are checked against code and commit history before they become a gap. *(Day 2)*
- Doc commands run in `docker run --network none` before a human sees the fix. *(Day 2)*
- Merge only after a recorded `@docs-owner` approval. *(Day 3)*
- Secrets live in `.env` (git-ignored) or a file outside the repo (`DOCDESK_ENV_FILE`); `make scan` checks
  tracked files before every push.

## Tests

`make test` runs the unit tests with no network and no keys: the protocol client against a fake socket and
HTTP transport (pong carries the cursor, resume parameters, stop on `taken_over`, reconnect after a bad
handshake, slow handlers never block pongs), retrieval and citations, the LLM fallback chain, and the agent's
thread handling. GitHub Actions runs them on every push.

## Status

- **Day 1 (done):** Switch 0.31.0 stack, protocol client, repo index with citations, LLM layer, room template,
  DocDesk answering in `#acme-help` threads, Makefile, tests.
- **Day 2:** correction detection with evidence, ledger updates, handoff to doc-fixer, verify sandbox, PR.
- **Day 3:** approval before merge, re-index and close the loop in the thread, hardening.
- **Day 4:** video, blog post, submission.

Known limits today: retrieval is keyword based (BM25), so phrasing far from the docs' words can miss; the
OpenCode free model sometimes times out, in which case DocDesk falls back to quoting the sources.

## Prior work

All code in this repo and in [acme-shop-demo](https://github.com/suhailult777/acme-shop-demo) was written
during the hackathon, starting Nov 4, 2026. No earlier code was reused. Before the start we only read the Switch
docs and ran a throwaway protocol smoke test, which is not part of this repo.

## License and credits

MIT, see [LICENSE](LICENSE). Switch is by SandboxAQ (Apache 2.0 with Commons Clause); this repo uses it and does
not include its code. The stack script downloads the official compose file at a pinned tag.
