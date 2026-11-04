import json

import httpx

from agent.llm import OPENROUTER_FREE_MODELS, ChainLLM, FakeLLM, LLMError, OpenRouterLLM, make_llm


def client(responses, seen):
    it = iter(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((dict(req.headers), json.loads(req.content)))
        return next(it)

    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(text="PORT [1]", model="google/gemma-4-31b-it:free"):
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": text}}]})


def test_sends_free_models_fallback_and_key_in_header():
    seen = []
    llm = OpenRouterLLM("k-test", http=client([ok()], seen))
    assert llm.complete("sys", "q") == "PORT [1]"
    headers, body = seen[0]
    assert headers["authorization"] == "Bearer k-test"
    assert body["model"] == OPENROUTER_FREE_MODELS[0]
    assert body["models"] == OPENROUTER_FREE_MODELS[:3] and all(m.endswith(":free") or m == "openrouter/free"
                                                                for m in body["models"])


def test_retries_on_429_then_succeeds():
    seen, slept = [], []
    llm = OpenRouterLLM("k", http=client([httpx.Response(429, headers={"retry-after": "1"}), ok()], seen),
                        sleep=slept.append)
    assert llm.complete("s", "q") == "PORT [1]" and slept == [1.0] and len(seen) == 2


def test_gives_up_after_retries_and_chain_falls_back():
    seen = []
    llm = OpenRouterLLM("k", retries=1, http=client([httpx.Response(429)] * 2, seen), sleep=lambda s: None)
    chain = ChainLLM([llm, FakeLLM()])
    out = chain.complete("s", "Sources:\n[1] a.md:1-2\nRun `npm run dev`.\n\nQuestion: how to run\n")
    assert chain.last_used == "fake" and "npm run dev" in out


def test_empty_answer_is_an_error():
    llm = OpenRouterLLM("k", http=client([ok(text="")], []))
    try:
        llm.complete("s", "q")
        raise AssertionError("expected LLMError")
    except LLMError:
        pass


def test_env_picks_model_preset_and_order():
    c = make_llm({"OPENROUTER_API_KEY": "k", "OPENROUTER_PRESET": "docdesk", "LLM_PROVIDER": "auto"})
    assert c.providers[0].name == "openrouter" and c.providers[0].model == "@preset/docdesk"
    assert "models" not in c.providers[0].body("s", "q")  # a preset carries its own fallbacks
    c = make_llm({"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "cohere/north-mini-code:free",
                  "OPENROUTER_PRESET": "docdesk"})
    assert c.providers[0].model == "cohere/north-mini-code:free"
    assert c.providers[0].models[0] == "cohere/north-mini-code:free" and len(c.providers[0].models) == 3
    assert make_llm({"LLM_PROVIDER": "auto"}).providers[0].name != "openrouter"  # no key, not used
