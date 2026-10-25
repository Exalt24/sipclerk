import hashlib
import hmac
import json
import random

import pytest

from sipclerk import delivery as d
from sipclerk.store import derive_key
from sipclerk.intake.machine import IntakeMachine
from conftest import IN_HOURS

SECRET = "lab-secret"
MASTER = bytes(range(32)) * 2
TENANT = "ashgrovepell"


def keys(tenant):
    return derive_key(MASTER, tenant)


def finished_record(tenant, call_id="call-9"):
    m = IntakeMachine(tenant, call_id, "+14155550142", IN_HOURS)
    m.begin()
    for u in ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no", "I was in a car accident",
              "about two weeks ago", "A truck ran a red light and hit my car on Main Street"]:
        m.handle_text(u)
    assert m.done
    return m.record()


class Receiver:
    """A fake receiver with a scripted sequence of statuses, and a real signature check."""

    def __init__(self, statuses, kind="bridge", secret=SECRET):
        self.statuses, self.kind, self.secret = list(statuses), kind, secret
        self.calls, self.accepted_keys = [], set()

    async def __call__(self, url, body, headers):
        self.calls.append((url, headers.get("idempotency-key")))
        if self.kind == "bridge":
            ok = hmac.compare_digest(headers.get("x-hook-signature", ""), hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest())
        else:
            t, v1 = (p.split("=", 1)[1] for p in headers["x-signature"].split(","))
            ok = hmac.compare_digest(v1, hmac.new(self.secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest())
        if not ok:
            return 401, "bad signature"
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            self.accepted_keys.add(headers["idempotency-key"])
        return status, "{}"


def box(tmp_path, receiver, now):
    return d.Outbox(tmp_path / "outbox.jsonl", {"bridge": d.Target("bridge", "http://x/hook", SECRET)}, sender=receiver,
                    clock=lambda: now["t"], keys=keys)


async def test_bridge_payload_is_clio_shaped_and_signature_verifies(ashgrove, tmp_path):
    rec = finished_record(ashgrove)
    p = d.build_bridge_payload(rec, "Summary.")
    assert p["meta"]["event"] == "intake.created" and p["data"]["id"] == "call-9" and p["data"]["etag"].startswith('"')
    assert p["data"]["client"]["phone"] == "+14155550123" and p["meta"]["config_hash"] == ashgrove.config_hash
    now = {"t": 1000.0}
    r = Receiver([200])
    ob = box(tmp_path, r, now)
    ob.enqueue("bridge", rec["idempotency_key"], p, tenant_id=TENANT)
    assert await ob.deliver_due() == 1 and ob.stats() == {"pending": 0, "delivered": 1, "dead": 0}


async def test_wrong_secret_is_a_permanent_refusal_and_goes_to_dead_letters(ashgrove, tmp_path):
    now = {"t": 0.0}
    ob = box(tmp_path, Receiver([200], secret="other"), now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await ob.deliver_due()
    assert ob.stats()["dead"] == 1 and ob.dead[0].attempts == 1 and "401" in ob.dead[0].last


async def test_server_error_retries_with_backoff_and_the_call_is_untouched(tmp_path):
    now = {"t": 0.0}
    r = Receiver([500, 500, 200])
    ob = box(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    assert await ob.deliver_due() == 0 and ob.pending[0].next_at == 1.0
    assert await ob.deliver_due() == 0 and len(r.calls) == 1  # not due yet: no second attempt inside the backoff
    now["t"] = 1.0
    assert await ob.deliver_due() == 0 and ob.pending[0].next_at == 3.0
    now["t"] = 3.0
    assert await ob.deliver_due() == 1 and ob.stats()["delivered"] == 1
    assert {k for _, k in r.calls} == {"k1"}  # every attempt carried the same idempotency key


async def test_gives_up_after_the_attempt_limit(tmp_path):
    now = {"t": 0.0}
    ob = box(tmp_path, Receiver([500] * 10), now)
    ob.enqueue("bridge", "k1", {}, tenant_id=TENANT)
    for _ in range(d.MAX_ATTEMPTS + 2):
        now["t"] += 100
        await ob.deliver_due()
    assert ob.stats()["dead"] == 1 and ob.dead[0].attempts == d.MAX_ATTEMPTS


async def test_connection_errors_and_429_are_retried_but_other_4xx_are_not(tmp_path):
    now = {"t": 0.0}

    async def refused(url, body, headers):
        raise ConnectionRefusedError("down")

    ob = d.Outbox(tmp_path / "o.jsonl", {"bridge": d.Target("bridge", "http://x", SECRET)}, sender=refused, clock=lambda: now["t"], keys=keys)
    ob.enqueue("bridge", "k1", {}, tenant_id=TENANT)
    await ob.deliver_due()
    assert ob.stats()["pending"] == 1 and "ConnectionRefusedError" in ob.pending[0].last
    ob2 = box(tmp_path / "b", Receiver([429, 422]), now)
    ob2.enqueue("bridge", "a", {}, tenant_id=TENANT)
    ob2.enqueue("bridge", "b", {}, tenant_id=TENANT)
    await ob2.deliver_due()
    assert {e.key: e.status for e in ob2.entries.values()} == {"a": "pending", "b": "dead"}


async def test_enqueue_is_idempotent_per_target_and_key(tmp_path):
    now = {"t": 0.0}
    ob = box(tmp_path, Receiver([]), now)
    assert ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT) is True
    assert ob.enqueue("bridge", "k1", {"a": 2}, tenant_id=TENANT) is False
    assert len(ob.entries) == 1 and ob.entries["bridge:k1"].payload == {"a": 1}


async def test_outbox_survives_a_restart(tmp_path):
    now = {"t": 0.0}
    r = Receiver([500])
    ob = box(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await ob.deliver_due()
    again = box(tmp_path, Receiver([200]), now)
    assert again.stats()["pending"] == 1 and again.pending[0].attempts == 1
    now["t"] = 5
    assert await again.deliver_due() == 1


async def test_unknown_target_goes_dead_instead_of_looping(tmp_path):
    now = {"t": 0.0}
    ob = box(tmp_path, Receiver([]), now)
    ob.enqueue("nowhere", "k", {}, tenant_id=TENANT)
    await ob.deliver_due()
    assert ob.stats()["dead"] == 1


@pytest.mark.parametrize("seed", range(25))
async def test_random_failure_worlds_never_lose_or_double_deliver(tmp_path, seed):
    """Invariants over seeded random sequences of failures: every key is delivered at most once per success,
    nothing pending is silently dropped, and a dead letter is only ever a permanent refusal or an exhausted retry."""
    rnd = random.Random(seed)
    now = {"t": 0.0}
    statuses = [rnd.choice([200, 200, 500, 503, 429, 0, 422]) for _ in range(200)]
    r = Receiver([s for s in statuses if s != 0])
    ob = box(tmp_path / str(seed), r, now)
    keys = [f"k{i}" for i in range(rnd.randint(3, 12))]
    for step in range(60):
        if rnd.random() < 0.3 and keys:
            k = keys.pop()
            ob.enqueue("bridge", k, {"k": k}, tenant_id=TENANT)
            if rnd.random() < 0.3:
                assert ob.enqueue("bridge", k, {"k": "dup"}, tenant_id=TENANT) is False  # a duplicate enqueue must be refused
        now["t"] += rnd.choice([0.5, 1, 5, 40])
        await ob.deliver_due()
        if rnd.random() < 0.15:  # a restart in the middle
            ob = box(tmp_path / str(seed), r, now)
    for _ in range(d.MAX_ATTEMPTS + 2):
        now["t"] += 100
        await ob.deliver_due()
    for e in ob.entries.values():
        assert e.status in ("delivered", "dead"), (seed, e)
        if e.status == "dead":
            assert e.attempts >= d.MAX_ATTEMPTS or "422" in e.last or "401" in e.last
        if e.status == "delivered":
            assert e.key in r.accepted_keys
    delivered = [e.key for e in ob.entries.values() if e.status == "delivered"]
    assert len(delivered) == len(set(delivered))


def test_console_payload_matches_the_documented_ingest_contract(ashgrove):
    rec = finished_record(ashgrove)
    transcript = [{"speaker": "AGENT", "text": "May I have your first and last name?"},
                  {"speaker": "CALLER", "text": "Call me on 415-555-0123 or jo@example.com"}]
    p = d.build_console_payload(rec, transcript, 95, ashgrove.config.display_name)
    assert set(p) == {"externalId", "startedAt", "durationSec", "agent", "callerNumber", "reason", "outcome", "patientName", "transcript"}
    assert p["externalId"] == "ashgrovepell-call-9" and p["reason"] == "OTHER" and p["outcome"] == "INFO_GIVEN"
    assert p["startedAt"].endswith("Z") and "+00:00" not in p["startedAt"] and p["patientName"] == "Jordan Reyes"
    assert "555" not in json.dumps(p["transcript"]) and "example.com" not in json.dumps(p["transcript"])
    assert len(p["agent"]) <= 60


def test_console_signature_follows_the_documented_header():
    ts = 1_700_000_000
    h = d.console_signature("s", b'{"a":1}', ts)
    assert h == f"t={ts},v1=" + hmac.new(b"s", f"{ts}.".encode() + b'{"a":1}', hashlib.sha256).hexdigest()


def test_outcome_mapping_covers_every_machine_outcome(ashgrove):
    for outcome in ("transferred", "intake_complete", "intake_priority", "intake_needs_review", "declined", "declined_out_of_scope"):
        assert d.OUTCOME_FOR_CONSOLE[outcome] in ("HANDED_OFF", "INFO_GIVEN", "UNRESOLVED")
