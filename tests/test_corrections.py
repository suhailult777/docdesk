from agent.corrections import (
    check_correction, extract_terms, looks_like_correction, reply_text, split_terms,
)


def test_detects_corrections_and_ignores_questions():
    assert looks_like_correction("That's outdated, it reads `PORT` now")
    assert looks_like_correction("actually we use npm run dev")
    assert not looks_like_correction("thanks! and how do I run the tests?")


def test_extracts_env_npm_and_paths():
    terms = {t.text: t.kind for t in extract_terms("use `npm run dev`, PORT and POST `/api/checkout`")}
    assert terms == {"npm run dev": "npm", "PORT": "env", "/api/checkout": "path"}


def test_old_term_from_not_and_from_wrong_answer():
    new, old = split_terms("it's `PORT` now, not `SHOP_PORT`")
    assert [t.text for t in new] == ["PORT"] and [t.text for t in old] == ["SHOP_PORT"]
    new, old = split_terms("outdated, the server reads `PORT`", "Set `SHOP_PORT` to change it")
    assert [t.text for t in old] == ["SHOP_PORT"]


def test_confirms_env_rename_with_commit(shop):
    ev = check_correction(shop, "That's outdated, the server reads `PORT` now, not `SHOP_PORT`.")
    assert ev.confirmed
    assert ev.code_refs["PORT"][0].ref() == "src/server.js:1"
    assert ev.commits["PORT"].subject == "read PORT instead of SHOP_PORT"
    assert ev.files == ["README.md"]
    assert ev.replacements() == [("SHOP_PORT", "PORT")]
    text = reply_text(ev, "G-1")
    assert text.startswith("You're right.") and "G-1" in text and "`README.md`" in text


def test_npm_script_rename_infers_the_old_command(shop):
    ev = check_correction(shop, "actually `npm start` doesn't work, we use `npm run dev`")
    assert ev.confirmed
    assert [t.text for t in ev.old_terms] == ["npm start"]
    assert ev.commits["npm run dev"].subject == "rename start script to dev"
    assert ev.replacements() == [("npm start", "npm run dev")]


def test_old_route_only_in_tests_still_counts_as_gone(shop):
    ev = check_correction(shop, "wrong, it's POST `/api/checkout` now instead of `/api/v1/orders`")
    assert ev.confirmed and ev.files == ["docs/api.md"]


def test_refuses_a_correction_the_code_does_not_back(shop):
    ev = check_correction(shop, "that's wrong, it's `APP_PORT` now, not `SHOP_PORT`")
    assert not ev.confirmed
    assert "could not confirm" in reply_text(ev, None)


def test_asks_for_specifics_when_no_terms(shop):
    ev = check_correction(shop, "that's outdated")
    assert "backticks" in reply_text(ev, None)


def test_new_npm_script_finds_the_removed_one_in_docs(shop):
    ev = check_correction(shop, "outdated: we use `npm run dev` and the server reads `PORT` now, not `SHOP_PORT`")
    assert ev.confirmed
    assert ev.replacements() == [("SHOP_PORT", "PORT"), ("npm start", "npm run dev")]
    assert ev.files == ["README.md"]
