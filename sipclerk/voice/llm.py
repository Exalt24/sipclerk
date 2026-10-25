"""The language model's small, fenced jobs.

It never drives the call. It does four things, each with a deterministic fallback, a time limit, and a
validator that the answer must pass before the state machine sees it:

* classify what a caller said into one of the tenant's practice areas (or "other"),
* pull a name or a date out of a sentence the plain parsers could not read,
* write one short acknowledgement sentence before the closing line (screened by the output guard),
* write the staff summary after the call (screened by the output guard).

Tokens and seconds are counted per call, so a call's model cost is a measurement, not a guess.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("SIPCLERK_LLM", "qwen3.5:4b")


class LlmUnavailable(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"calls": self.calls, "failures": self.failures, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "seconds": round(self.seconds, 2)}


@dataclass
class Llm:
    base_url: str = OLLAMA_URL
    model: str = MODEL
    timeout_s: float = 4.0
    fail: bool = False  # test hook: simulate a provider outage
    usage: Usage = field(default_factory=Usage)
    _client: httpx.AsyncClient | None = None

    async def _chat(self, system: str, user: str, schema: dict | None, max_tokens: int) -> str:
        self.usage.calls += 1
        t0 = time.perf_counter()
        try:
            if self.fail:
                raise LlmUnavailable("injected provider failure")
            if self._client is None:
                self._client = httpx.AsyncClient(base_url=self.base_url)
            body = {"model": self.model, "stream": False, "think": False, "keep_alive": "30m",
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "options": {"temperature": 0, "seed": 7, "num_predict": max_tokens}}
            if schema:
                body["format"] = schema
            r = await asyncio.wait_for(self._client.post("/api/chat", json=body), self.timeout_s)
            r.raise_for_status()
            j = r.json()
            self.usage.prompt_tokens += int(j.get("prompt_eval_count", 0))
            self.usage.completion_tokens += int(j.get("eval_count", 0))
            return j["message"]["content"].strip()
        except Exception as e:
            self.usage.failures += 1
            raise LlmUnavailable(f"{type(e).__name__}: {e}") from e
        finally:
            self.usage.seconds += time.perf_counter() - t0

    async def classify_area(self, text: str, area_ids: list[str]) -> str | None:
        """One of area_ids, or "other". None when the model is down or answers outside the list."""
        schema = {"type": "object", "properties": {"area": {"type": "string", "enum": [*area_ids, "other"]}}, "required": ["area"]}
        try:
            out = await self._chat(
                "You sort a caller's description into one legal practice area for a law firm's receptionist. "
                "Answer with JSON only. Use 'other' when none of the areas fits.",
                f"Areas: {', '.join(area_ids)}.\nCaller said: {text}", schema, 40)
            area = json.loads(out).get("area")
        except (LlmUnavailable, ValueError):
            return None
        return area if area in [*area_ids, "other"] else None

    async def extract_name(self, text: str) -> str | None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        try:
            out = await self._chat("Extract the caller's own full name from what they said. JSON only. "
                                   "Use an empty string if they did not give a name.", text, schema, 40)
            return json.loads(out).get("name") or None
        except (LlmUnavailable, ValueError):
            return None

    async def extract_date(self, text: str, today_iso: str) -> str | None:
        schema = {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}
        try:
            out = await self._chat(f"Today is {today_iso}. Turn the date the caller mentions into YYYY-MM-DD. "
                                   "JSON only. Use an empty string if there is no date.", text, schema, 30)
            return json.loads(out).get("date") or None
        except (LlmUnavailable, ValueError):
            return None

    async def acknowledge(self, description: str) -> str | None:
        """One short, kind sentence. It must not give advice, assess the case or promise anything."""
        try:
            out = await self._chat(
                "You are a law firm's receptionist. In ONE short sentence under 18 words, acknowledge what the caller described "
                "with empathy. Do not give advice, do not say anything about their case, do not promise anything, do not ask a question.",
                description, None, 40)
        except LlmUnavailable:
            return None
        out = out.strip().splitlines()[0].strip() if out.strip() else ""
        return out if 3 <= len(out.split()) <= 25 else None

    async def summarize(self, facts: dict) -> str | None:
        try:
            out = await self._chat(
                "Write a two sentence summary of this law firm intake call for staff. State only the facts given. "
                "No advice, no assessment of the case, no promises.", json.dumps(facts), None, 120)
        except LlmUnavailable:
            return None
        return out or None
