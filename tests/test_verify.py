from agent.verify import Command, Result, build_script, compare, extract_commands, parse_output, port_of

DOC = """# Run

```bash
npm install
SHOP_PORT=8080 npm start
```

Start the server with `npm run dev`, then:

```bash
curl -X POST http://localhost:3000/api/checkout \\
  -H 'content-type: application/json' \\
  -d '{"items":[]}'
```

```json
{"not": "a command"}
```

The variable `PORT` is not a command.
"""


def test_extracts_blocks_inline_spans_and_continuations():
    cmds = extract_commands(DOC, "README.md")
    assert [c.text for c in cmds] == [
        "npm install",
        "SHOP_PORT=8080 npm start",
        "npm run dev",
        "curl -X POST http://localhost:3000/api/checkout -H 'content-type: application/json' -d '{\"items\":[]}'",
    ]
    assert [c.server for c in cmds] == [False, True, True, False]


def test_port_from_env_prefix():
    assert port_of("PORT=8080 npm run dev") == 8080
    assert port_of("npm run dev") == 3000


def test_script_waits_on_the_commands_port_and_fails_curl_on_http_errors():
    s = build_script(extract_commands(DOC))
    assert "http://127.0.0.1:8080/" in s and "fail-with-body" in s
    assert "### rc 3" in s


def test_parse_output_reads_markers():
    cmds = [Command("a.md", "npm test", False), Command("a.md", "npm start", True)]
    out = '### rc 0 0\n### rc 1 1\n### out 1 npm error Missing script: "start"\n### out 1 nothing answered\n'
    r = parse_output(out, cmds)
    assert r[0].ok and not r[1].ok and r[1].detail == 'npm error Missing script: "start"'


def res(text, ok, file="a.md"):
    return Result(Command(file, text, False), ok, "")


def test_compare_blocks_changed_failures_and_regressions_only():
    before = [res("npm start", False), res("npm test", True), res("curl old", False)]
    good = [res("npm run dev", True), res("npm test", True), res("curl old", False)]
    v = compare(before, good)
    assert v.ok and [r.cmd.text for r in v.known_failing] == ["curl old"]
    assert "2 of 3 commands pass" in v.report()
    bad = [res("PORT=1 npm start", False), res("npm test", False)]
    v = compare(before, bad)
    assert not v.ok and [r.cmd.text for r in v.blocking] == ["PORT=1 npm start", "npm test"]
