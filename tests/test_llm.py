"""The language model's fenced jobs, against a fake Ollama: valid answers pass, anything else becomes None."""
import asyncio
import json

import httpx
import pytest

from sipclerk.voice.llm import Llm, LlmUnavailable


def llm_with(handler, **kw) -> Llm:
    llm = Llm(timeout_s=kw.pop("timeout_s", 1.0), **kw)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama.test")
    return llm


def reply(content, prompt=40, completion=7):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["think"] is False and body["options"]["temperature"] == 0  # deterministic and no reasoning tokens
        return httpx.Response(200, json={"message": {"content": content}, "prompt_eval_count": prompt, "eval_count": completion})

    return handler


async def test_classify_returns_a_listed_area_and_counts_tokens():
    llm = llm_with(reply('{"area": "personal_injury"}'))
    assert await llm.classify_area("a truck hit me", ["personal_injury", "employment"]) == "personal_injury"
    assert llm.usage.calls == 1 and llm.usage.prompt_tokens == 40 and llm.usage.completion_tokens == 7 and llm.usage.failures == 0


async def test_classify_accepts_other_and_refuses_anything_outside_the_list():
    assert await llm_with(reply('{"area": "other"}')).classify_area("x", ["a"]) == "other"
    assert await llm_with(reply('{"area": "tax_law"}')).classify_area("x", ["a"]) is None
    assert await llm_with(reply("not json at all")).classify_area("x", ["a"]) is None


async def test_the_request_pins_the_answer_to_the_tenants_areas():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"area": "employment"}'}})

    await llm_with(handler).classify_area("fired", ["personal_injury", "employment"])
    assert seen["format"]["properties"]["area"]["enum"] == ["personal_injury", "employment", "other"]


async def test_every_job_returns_none_when_the_provider_is_down():
    llm = Llm(fail=True)
    assert await llm.classify_area("x", ["a"]) is None
    assert await llm.extract_name("x") is None
    assert await llm.extract_date("x", "2026-10-27") is None
    assert await llm.acknowledge("I was hurt") is None
    assert await llm.summarize({"a": 1}) is None
    assert llm.usage.failures == 5 and llm.usage.calls == 5


async def test_a_slow_provider_is_cut_off_at_the_time_limit():
    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    llm = llm_with(slow, timeout_s=0.2)
    assert await llm.classify_area("x", ["a"]) is None and llm.usage.failures == 1


async def test_http_errors_are_failures_not_crashes():
    llm = llm_with(lambda r: httpx.Response(500, json={"error": "boom"}))
    assert await llm.extract_name("Jordan") is None and llm.usage.failures == 1


async def test_extractors_return_none_for_an_empty_answer():
    assert await llm_with(reply('{"name": ""}')).extract_name("hello") is None
    assert await llm_with(reply('{"name": "Jordan Reyes"}')).extract_name("it is jordan") == "Jordan Reyes"
    assert await llm_with(reply('{"date": ""}')).extract_date("x", "2026-10-27") is None
    assert await llm_with(reply('{"date": "2026-10-13"}')).extract_date("two weeks ago", "2026-10-27") == "2026-10-13"


async def test_acknowledgement_is_one_short_sentence_or_nothing():
    assert await llm_with(reply("I am sorry to hear that.\nSecond line.")).acknowledge("hurt") == "I am sorry to hear that."
    assert await llm_with(reply("Sorry.")).acknowledge("hurt") is None  # too short to be a sentence
    assert await llm_with(reply(" ".join(["word"] * 40))).acknowledge("hurt") is None  # too long
    assert await llm_with(reply("")).acknowledge("hurt") is None


async def test_summary_comes_back_as_text():
    assert await llm_with(reply("Jordan Reyes called about a car accident.")).summarize({"x": 1}) == "Jordan Reyes called about a car accident."
    assert await llm_with(reply("")).summarize({"x": 1}) is None


async def test_llm_unavailable_is_the_only_exception_type_that_escapes_chat():
    llm = Llm(fail=True)
    with pytest.raises(LlmUnavailable):
        await llm._chat("s", "u", None, 10)
