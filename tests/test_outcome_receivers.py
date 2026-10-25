"""Every outcome that stores a callback task reaches the same receivers, by one code path.

A caller who hung up mid intake, or while the desk phone was ringing, got a stored callback task and a console delivery and nothing for the case-management
bridge, because the bridge entry was queued only when the machine submitted a record, and a hang up never submits. `delivery.queue_call` is now the one place
a finished call is queued: the console gets every call, the bridge gets every call that carries a task."""
import ast
import asyncio
import time
import types
from pathlib import Path

import pytest

from conftest import AFTER_HOURS, IN_HOURS
from sipclerk import delivery
from sipclerk.intake.machine import IntakeMachine
from sipclerk.server import ClerkServer
from sipclerk.store import Actor, TenantStore
from test_pipeline_fast import ASH, _press_zero, agent_done, finished, place, rig

SRC = Path(__file__).resolve().parent.parent / "sipclerk"
AREA = {"ashgrovepell": "a car accident", "tidewaterquint": "a custody case"}


def to_question(tenant, qid, now=IN_HOURS):
    """The machine waiting at question `qid`, every earlier question answered the way an ordinary caller does (the same drive for either firm)."""
    m = IntakeMachine(tenant, "c1", "+14155550142", now)
    m.begin()
    if m.state.value == "consent":
        m.handle_text("yes")
    while m.question is not None and m.question.id != qid:
        q = m.question
        answers = {"name": ["Jordan Reyes", "yes"], "phone": ["four one five five five five zero one two three", "yes"], "yesno": ["no"], "email": ["skip"],
                   "date": ["two weeks ago", "yes"]}.get(q.type, [])
        if q.id == "practice_area":
            answers = [AREA[tenant.config.tenant_id]]
        elif q.id == "state":
            answers = ["Florida", "yes"]
        for a in answers:
            m.handle_text(a)
    assert m.question is not None and m.question.id == qid, m.outcome
    return m


def with_result(tenant, result):
    m = to_question(tenant, "description")
    m.evaluate_qualification = lambda: {"result": result, "rules": []}
    m.handle_text("A truck ran a red light and hit my car on Main Street")
    return m


def silent(tenant):
    m = to_question(tenant, "full_name")
    for _ in range(3):
        m.on_silence_timeout()
    return m


def transferring(tenant, ok=None, now=IN_HOURS):
    m = to_question(tenant, "full_name", now)
    m.handle_text("I want to talk to a person")
    if ok is not None:
        m.on_transfer_result(ok, "no answer")
    return m


def hung_up(m):
    m.on_caller_hangup()
    return m


def no_ack(tenant):
    m = IntakeMachine(tenant, "c1", "+14155550142", IN_HOURS)  # the line answered and no ACK came: the machine never began
    m.on_no_ack()
    return m


def declined_by_word(tenant):
    m = to_question(tenant, "practice_area")
    m.handle_text("I need a lawyer for a DUI")
    return m


# every way a call can end, each reached the way a call really reaches it: (a label, the outcome it must be, how to get a finished machine)
CASES = [
    ("a complete intake", "intake_complete", lambda t: with_result(t, "qualified")),
    ("a priority intake", "intake_priority", lambda t: with_result(t, "priority")),
    ("an intake for review", "intake_needs_review", lambda t: with_result(t, "needs_review")),
    ("a declined intake", "declined", lambda t: with_result(t, "decline")),
    ("a matter the firm does not take", "declined_out_of_scope", declined_by_word),
    ("three silences", "abandoned_silent", silent),
    ("a request for a person after hours", "callback_requested", lambda t: transferring(t, now=AFTER_HOURS)),
    ("a transfer nobody answered", "callback_after_failed_transfer", lambda t: transferring(t, ok=False)),
    ("a transfer that connected", "transferred", lambda t: transferring(t, ok=True)),
    ("an ACK that never came", "dropped_no_ack", no_ack),
    ("a hang up mid intake", "caller_hung_up", lambda t: hung_up(to_question(t, "practice_area"))),
    ("a hang up while the desk phone rings", "caller_hung_up", lambda t: hung_up(transferring(t))),
]


def outcomes_the_machine_can_end_in() -> set[str]:
    """Every string the machine assigns to its outcome, read from its source, so a new outcome cannot be added without a case here."""
    tree = ast.parse((SRC / "intake" / "machine.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(ast.unparse(t) == "self.outcome" for t in n.targets):
            if isinstance(n.value, ast.Constant):
                found.add(n.value.value)
            for d in (x for x in ast.walk(n.value) if isinstance(x, ast.Dict)):  # `{"decline": "declined", ...}[result]`: the values are outcomes, the keys are not
                found |= {v.value for v in d.values if isinstance(v, ast.Constant)}
    return found - {""}


def test_every_outcome_the_machine_can_end_in_has_a_case():
    assert outcomes_the_machine_can_end_in() == {outcome for _, outcome, _ in CASES}


def lab(tmp_path, registry, now):
    store = TenantStore(tmp_path, registry, bytes(range(32)) * 2, clock=lambda: now)

    async def sender(url, body, headers):
        return 200, "{}"

    outbox = delivery.Outbox(tmp_path / "outbox.jsonl", {"bridge": delivery.Target("bridge", "http://b/", "s"), "console": delivery.Target("console", "http://c/", "s")},
                             sender=sender, keys=store.key_for)
    server = ClerkServer.__new__(ClerkServer)
    server.store, server.outbox = store, outbox
    return server, store, outbox


def config(tenant, bridge=True, console=True):
    c = tenant.config
    return types.SimpleNamespace(tenant_id=c.tenant_id, display_name=c.display_name, integrations=types.SimpleNamespace(case_management=bridge, call_console=console))


@pytest.mark.parametrize("tenant_id", ["ashgrovepell", "tidewaterquint"])
@pytest.mark.parametrize("label,outcome,drive", CASES, ids=[c[0] for c in CASES])
async def test_class_a_stored_task_has_an_outbox_entry_for_each_receiver(registry, tmp_path, label, outcome, drive, tenant_id):
    tenant = registry.by_id(tenant_id)
    m = drive(tenant)
    assert m.outcome == outcome, (label, m.outcome)
    record = m.record()
    server, store, outbox = lab(tmp_path, registry, IN_HOURS)
    server.file_call(config(tenant), record, [{"speaker": "CALLER", "text": "hello"}], "summary", None, 30)
    key = record["idempotency_key"]
    stored = store.read(Actor("sam", tenant_id, tenant.config.staff_roles[0]), tenant_id, record["call_id"], "record")
    assert stored["tasks"] == record["tasks"] and stored["outcome"] == outcome
    entries = {e.target for e in outbox.entries.values() if e.key == key}
    assert bool(record["tasks"]) == (outcome != "transferred"), (label, record["tasks"])  # every ending but a connected transfer leaves staff something to do
    if record["tasks"]:
        assert entries == {"bridge", "console"}, (label, entries)
        bridge = next(e for e in outbox.entries.values() if e.target == "bridge" and e.key == key).payload
        assert bridge["data"]["follow_up_tasks"] == record["tasks"]
        assert bridge["meta"]["event"] == ("intake.created" if record["qualification"] else "intake.incomplete")
    else:
        assert entries == {"console"}, (label, entries)  # a call that went to a person carries no task and is not a matter for the bridge
    await asyncio.sleep(0.05)


@pytest.mark.parametrize("bridge,console", [(True, False), (False, True), (False, False)])
async def test_a_receiver_a_firm_turned_off_is_not_queued(registry, tmp_path, ashgrove, bridge, console):
    record = hung_up(to_question(ashgrove, "practice_area")).record()
    server, _, outbox = lab(tmp_path, registry, IN_HOURS)
    server.file_call(config(ashgrove, bridge, console), record, [], "s", None, 5)
    assert {e.target for e in outbox.entries.values()} == ({"bridge"} if bridge else set()) | ({"console"} if console else set())
    await asyncio.sleep(0.05)


def test_a_hang_up_keeps_the_outcome_a_call_already_has_and_adds_no_second_task(ashgrove):
    for label, outcome, drive in CASES:
        if outcome == "caller_hung_up":
            continue
        m = drive(ashgrove)
        before = (m.outcome, list(m.tasks))
        m.on_caller_hangup()  # the caller's BYE after the call was already over, or after the bridge
        assert (m.outcome, m.tasks) == before, label
    m = to_question(ashgrove, "practice_area")
    m.on_caller_hangup()
    m.on_caller_hangup()
    assert m.outcome == "caller_hung_up" and len(m.tasks) == 1


async def test_queueing_a_call_twice_is_one_delivery_each(registry, tmp_path, ashgrove):
    record = with_result(ashgrove, "qualified").record()
    server, _, outbox = lab(tmp_path, registry, IN_HOURS)
    first = delivery.queue_call(outbox, config(ashgrove), record, [], 0, only="bridge")
    again = delivery.queue_call(outbox, config(ashgrove), record, [], 0)
    third = delivery.queue_call(outbox, config(ashgrove), record, [], 0)
    assert first == ["bridge"] and again == ["console"] and third == [] and len(outbox.entries) == 2


# ---- the same through a real call

async def test_a_caller_who_hangs_up_mid_intake_reaches_the_bridge_and_the_console():
    async with rig(["yes"]) as r:
        call, _ = await place(r, ASH, "+14155550161")
        await agent_done(call)
        await call.hangup()
        res = await finished(r, "+14155550161")
        rec = res.record
        assert rec["outcome"] == "caller_hung_up" and rec["tasks"] == [{"kind": "callback", "reason": "caller hung up before the intake finished"}]
        assert sorted(e.target for e in r.outbox.entries.values() if e.key == rec["idempotency_key"]) == ["bridge", "console"]
        end = time.monotonic() + 10
        while r.outbox.stats()["delivered"] < 2 and time.monotonic() < end:
            await asyncio.sleep(0.1)
        bridge = next(b for url, _h, b in r.stub.received if url == r.bridge_url)
        assert bridge["meta"]["event"] == "intake.incomplete" and bridge["data"]["follow_up_tasks"] == rec["tasks"]


async def test_a_transfer_ended_by_the_callers_hang_up_reaches_the_bridge_and_the_console():
    async with rig([]) as r:
        r.staff["tidewaterquint"].answer_delay = 3.0
        call, staff = await _press_zero(r, "+17865550162")
        end = time.monotonic() + 10
        while not staff.invites and time.monotonic() < end:
            await asyncio.sleep(0.05)
        assert staff.invites, "the desk phone never rang"
        await call.hangup()
        res = await finished(r, "+17865550162")
        rec = res.record
        assert rec["outcome"] == "caller_hung_up" and any(t["kind"] == "callback" for t in rec["tasks"])
        assert sorted(e.target for e in r.outbox.entries.values() if e.key == rec["idempotency_key"]) == ["bridge", "console"]
        await asyncio.sleep(3.3)
