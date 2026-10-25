"""Signed, idempotent delivery of a finished intake to other systems, through a retrying outbox.

Two receivers, each used the way its own repository documents it:

* clio-matter-bridge verifies `x-hook-signature: HMAC-SHA256(secret, raw body)` and dedupes on
  `meta.event` + `data.id` + `data.etag` (its webhook.mjs). Our payload is shaped like a Clio delivery.
* clinic-call-console takes `POST /api/ingest/calls` with `X-Signature: t=<unix>,v1=HMAC(secret, "t.body")`
  and dedupes on `externalId` (its WebhookSignature and IngestController).

A failed delivery never touches the call: the entry waits in the outbox and is retried with backoff.
The same idempotency key is sent every time, so a retry after a half-finished attempt cannot create a
second record. A permanent refusal (a 4xx other than 408 and 429) goes to the dead letters at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sipclerk.intake.parsers import mask_for_transcript

Sender = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, str]]]

BACKOFF_S = [1, 2, 4, 8, 16, 32]
MAX_ATTEMPTS = 6
RETRYABLE = {408, 425, 429}


def bridge_signature(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def console_signature(secret: str, raw_body: bytes, ts: int) -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def build_bridge_payload(record: dict, summary: str) -> dict:
    """Shaped like a Clio webhook delivery: event under meta.event, record under data with an etag."""
    f = {k: v["value"] for k, v in record["fields"].items()}
    qualification = record["qualification"].get("result", "not_evaluated")  # a call that ended early was never qualified
    etag = hashlib.sha256(json.dumps({"fields": f, "q": record["qualification"], "o": record["outcome"]}, sort_keys=True).encode()).hexdigest()[:16]
    return {
        "meta": {"event": "intake.created" if record["qualification"] else "intake.incomplete", "tenant_id": record["tenant_id"], "config_version": record["config_version"],
                 "config_hash": record["config_hash"]},
        "data": {
            "id": record["call_id"], "etag": f'"{etag}"', "idempotency_key": record["idempotency_key"],
            "client": {"name": f.get("full_name"), "phone": f.get("callback_number"), "email": f.get("email")},
            "matter": {"practice_area": f.get("practice_area"), "incident_date": f.get("incident_date"),
                       "description": f.get("description"), "qualification": qualification},
            "follow_up_tasks": record["tasks"],
            "summary": summary,
        },
    }


OUTCOME_FOR_CONSOLE = {"transferred": "HANDED_OFF", "intake_complete": "INFO_GIVEN", "intake_priority": "INFO_GIVEN",
                       "intake_needs_review": "INFO_GIVEN", "declined": "UNRESOLVED", "declined_out_of_scope": "UNRESOLVED"}


def build_console_payload(record: dict, transcript: list[dict], duration_s: int, display_name: str) -> dict:
    """The console's documented ingest body. It masks identifiers itself on arrival; we also mask digits and emails in the lines."""
    name = record["fields"].get("full_name", {}).get("value") or "Unknown caller"
    return {
        "externalId": f'{record["tenant_id"]}-{record["call_id"]}'[:80],
        "startedAt": record["started_at"].replace("+00:00", "Z") if record["started_at"].endswith("Z") else record["started_at"] + "Z",
        "durationSec": max(0, min(7200, int(duration_s))),
        "agent": f"{display_name} intake"[:60],
        "callerNumber": record["caller_number"] or "unknown",
        "reason": "OTHER",  # the console's reasons are clinic ones (APPOINTMENT, INSURANCE, ...); a law intake maps to OTHER
        "outcome": OUTCOME_FOR_CONSOLE.get(record["outcome"], "UNRESOLVED"),
        "patientName": name[:80],
        "transcript": [{"speaker": t["speaker"], "text": mask_for_transcript(t["text"])[:2000]} for t in transcript[:200]],
    }


@dataclass
class Target:
    name: str
    url: str
    secret: str

    def request(self, payload: dict, key: str, now: float) -> tuple[bytes, dict[str, str]]:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"content-type": "application/json", "idempotency-key": key}
        if self.name == "bridge":
            headers["x-hook-signature"] = bridge_signature(self.secret, raw)
        else:
            headers["x-signature"] = console_signature(self.secret, raw, int(now))
        return raw, headers


@dataclass
class OutboxEntry:
    key: str
    target: str
    payload: dict
    attempts: int = 0
    next_at: float = 0.0
    status: str = "pending"  # pending, delivered, dead
    last: str = ""
    history: list[str] = field(default_factory=list)


async def httpx_sender(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    import httpx

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, content=body, headers=headers)
        return r.status_code, r.text[:300]


class Outbox:
    def __init__(self, path: Path, targets: dict[str, Target], sender: Sender = httpx_sender,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.targets = targets
        self.sender = sender
        self.clock = clock
        self.entries: dict[str, OutboxEntry] = {}
        self._lock = asyncio.Lock()
        if self.path.exists():
            for line in filter(str.strip, self.path.read_text(encoding="utf-8").splitlines()):
                e = OutboxEntry(**json.loads(line))
                self.entries[f"{e.target}:{e.key}"] = e

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e.__dict__) + "\n" for e in self.entries.values()), encoding="utf-8")
        tmp.replace(self.path)

    def enqueue(self, target: str, key: str, payload: dict) -> bool:
        """Queue a delivery. The same (target, key) twice is one delivery. Returns False for the duplicate."""
        ek = f"{target}:{key}"
        if ek in self.entries:
            return False
        self.entries[ek] = OutboxEntry(key=key, target=target, payload=payload, next_at=self.clock())
        self._persist()
        return True

    @property
    def pending(self) -> list[OutboxEntry]:
        return [e for e in self.entries.values() if e.status == "pending"]

    @property
    def dead(self) -> list[OutboxEntry]:
        return [e for e in self.entries.values() if e.status == "dead"]

    async def deliver_due(self) -> int:
        """Try every entry that is due. Returns how many were delivered in this pass."""
        delivered = 0
        async with self._lock:
            for e in list(self.entries.values()):
                if e.status != "pending" or e.next_at > self.clock():
                    continue
                t = self.targets.get(e.target)
                if t is None:
                    e.status, e.last = "dead", "no such target"
                    continue
                raw, headers = t.request(e.payload, e.key, self.clock())
                e.attempts += 1
                try:
                    code, text = await self.sender(t.url, raw, headers)
                except Exception as exc:  # connection refused, timeout, DNS: all retryable
                    code, text = 0, f"{type(exc).__name__}"
                e.last = f"{code} {text[:80]}"
                e.history.append(f"{e.attempts}:{code}")
                if 200 <= code < 300:
                    e.status = "delivered"
                    delivered += 1
                elif code in RETRYABLE or code >= 500 or code == 0:
                    if e.attempts >= MAX_ATTEMPTS:
                        e.status = "dead"
                    else:
                        e.next_at = self.clock() + BACKOFF_S[min(e.attempts - 1, len(BACKOFF_S) - 1)]
                else:  # a 4xx that will not get better by asking again
                    e.status = "dead"
            self._persist()
        return delivered

    async def run(self, stop: asyncio.Event, every: float = 0.5) -> None:
        while not stop.is_set():
            await self.deliver_due()
            try:
                await asyncio.wait_for(stop.wait(), every)
            except asyncio.TimeoutError:
                pass

    def stats(self) -> dict:
        s = {"pending": 0, "delivered": 0, "dead": 0}
        for e in self.entries.values():
            s[e.status] += 1
        return s
