"""Random walks through the intake machine: many seeded calls, every event kind mixed, invariants checked after each step.

The example tests in test_machine.py pin known paths. This file looks for the paths nobody wrote down: a keypad digit
in the middle of a read-back, silence right after a guard hit, a transfer result that arrives twice, in either firm,
in and out of hours.
"""
import json
import random
from datetime import datetime

import pytest

from conftest import AFTER_HOURS, IN_HOURS
from sipclerk import guard
from sipclerk.intake.machine import Hangup, IntakeMachine, Say, StartTransfer, State, Submit

UTTERANCES = [
    "yes", "no", "yeah that's right", "nope", "Jordan Reyes", "my name is Dana Whitfield", "Jordan", "banana",
    "four one five five five five oh one two three", "415-555-0123", "four one five", "I was in a car accident",
    "my boss fired me", "about two weeks ago", "yesterday", "next year", "March 3rd 2026", "in 1960", "skip", "I would rather not",
    "do I have a good case", "should I sue", "I'm already a client", "I want to talk to a person", "this is an emergency",
    "I need a lawyer for a DUI", "we live in Florida", "Georgia", "a truck hit my car and I hurt my neck badly",
    "no it's Jordan Reyes", "wait", "", "uh", "my husband is bleeding", "what is the statute of limitations",
]
DIGITS = list("0123456789#*")


def step(m: IntakeMachine, rnd: random.Random):
    r = rnd.random()
    if r < 0.55:
        return "text", m.handle_text(rnd.choice(UTTERANCES))
    if r < 0.72:
        return "dtmf", m.on_dtmf(rnd.choice(DIGITS))
    if r < 0.86:
        return "silence", m.on_silence_timeout()
    if r < 0.93 and m.state == State.TRANSFERRING:
        return "transfer", m.on_transfer_result(rnd.random() < 0.5, "x")
    return "text", m.handle_text(rnd.choice(UTTERANCES))


@pytest.mark.parametrize("seed", range(120))
def test_invariants_hold_over_a_random_call(registry, seed):
    rnd = random.Random(seed)
    tenant = registry.all[seed % 2]
    now: datetime = IN_HOURS if seed % 3 else AFTER_HOURS
    m = IntakeMachine(tenant, f"call-{seed}", "+14155550142", now)
    spoken = [a.text for a in m.begin() if isinstance(a, Say)]
    submits = hangups = 0
    was_done = False
    for _ in range(80):
        before_done = m.done
        kind, acts = step(m, rnd)
        if before_done:
            assert acts == [], (seed, kind, "an event after the call was over produced actions")
        for a in acts:
            if isinstance(a, Say):
                spoken.append(a.text)
            elif isinstance(a, Submit):
                submits += 1
                json.dumps(a.record)  # the record is always serialisable
            elif isinstance(a, Hangup):
                hangups += 1
                assert submits == 1, (seed, "a hang-up must follow exactly one submit")
            elif isinstance(a, StartTransfer):
                assert m.in_hours and tenant.config.transfer.enabled, (seed, "a transfer was started out of hours")
        assert submits <= 1 and hangups <= 1, (seed, submits, hangups)
        for q in tenant.config.questions:
            fs = m.fields[q.id]
            assert fs.retries <= q.max_retries + 1, (seed, q.id, fs.retries)
            if q.confirm and fs.value is not None:
                assert fs.confirmed or fs.source == "keypad" and fs.confirmed, (seed, q.id, "a confirm field was stored unconfirmed")
        assert m.silences <= 3
        if m.done:
            was_done = True
        if was_done and kind == "transfer":
            break
    assert all(guard.check_output(t).ok for t in spoken), (seed, [t for t in spoken if not guard.check_output(t).ok])
    blob = json.dumps(m.record())
    assert m.idempotency_key() == IntakeMachine(tenant, f"call-{seed}", "x", now).idempotency_key()
    for e in m.events:
        assert e.get("digit") in (None, "x", "0")
    assert blob


@pytest.mark.parametrize("tenant_index", [0, 1])
def test_endless_silence_always_ends_the_call(registry, tenant_index):
    m = IntakeMachine(registry.all[tenant_index], "c", "+1", IN_HOURS)
    m.begin()
    acts = []
    for _ in range(3):
        acts += m.on_silence_timeout()
    assert m.done and sum(isinstance(a, Hangup) for a in acts) == 1 and sum(isinstance(a, Submit) for a in acts) == 1


def test_any_text_after_the_end_is_ignored_in_both_firms(registry):
    for t in registry.all:
        m = IntakeMachine(t, "c", "+1", IN_HOURS)
        m.begin()
        for _ in range(3):
            m.on_silence_timeout()
        assert m.done
        for u in UTTERANCES:
            assert m.handle_text(u) == []
        assert m.on_dtmf("0") == [] and m.on_silence_timeout() == []


@pytest.mark.parametrize("seed", range(40))
def test_every_record_a_random_call_can_produce_can_be_delivered(registry, seed):
    """The first voice run found a record with no qualification (a caller who went silent) that the delivery builders could not read."""
    from sipclerk import delivery

    rnd = random.Random(1000 + seed)
    tenant = registry.all[seed % 2]
    m = IntakeMachine(tenant, f"d-{seed}", "+14155550142", IN_HOURS if seed % 4 else AFTER_HOURS)
    m.begin()
    for _ in range(60):
        step(m, rnd)
        if m.done:
            break
    rec = m.record()
    bridge = delivery.build_bridge_payload(rec, "")
    console = delivery.build_console_payload(rec, [{"speaker": "AGENT", "text": "hello"}], 12, tenant.config.display_name)
    json.dumps(bridge)
    assert bridge["meta"]["event"] == ("intake.created" if rec["qualification"] else "intake.incomplete")
    assert console["outcome"] in ("HANDED_OFF", "INFO_GIVEN", "UNRESOLVED") and console["transcript"]
