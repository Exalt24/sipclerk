"""The outbox is the second place caller data rests, so it follows the same rules as the store: encrypted per firm, bound to its
call, gone at the firm's retention. Every test here reads the RAW bytes of the file, not the object."""
import json

import pytest

from sipclerk import delivery as d
from sipclerk.store import derive_key

MASTER = bytes(range(32)) * 2
DAY = 86400.0
NAME, PHONE, EMAIL = "Zelda Quimby", "+14155550123", "zelda.quimby@example.org"
STORY = "a forklift crushed my left foot at the Harbor Street warehouse"
PLANTED = [NAME, "Quimby", PHONE, "4155550123", EMAIL, "Harbor Street", "forklift"]


def keys(tenant):
    return derive_key(MASTER, tenant)


def record(tenant, call_id):
    fields = {"full_name": NAME, "callback_number": PHONE, "email": EMAIL, "practice_area": "personal_injury", "incident_date": "2026-10-01",
              "description": STORY}
    return {"fields": {k: {"value": v} for k, v in fields.items()}, "qualification": {"result": "qualified"}, "outcome": "intake_complete", "tasks": [],
            "tenant_id": tenant, "config_version": "1", "config_hash": "h", "call_id": call_id, "idempotency_key": f"key-{tenant}-{call_id}",
            "started_at": "2026-10-01T10:00:00Z", "caller_number": PHONE}


TRANSCRIPT = [{"speaker": "CALLER", "text": f"My name is {NAME} and {STORY}"}]


class Receiver:
    def __init__(self, status=200):
        self.status, self.bodies = status, []

    async def __call__(self, url, body, headers):
        self.bodies.append(body)
        return self.status, "{}"


def make(tmp_path, receiver, now, name="outbox.jsonl"):
    targets = {"bridge": d.Target("bridge", "http://x/hook", "s1"), "console": d.Target("console", "http://x/ingest", "s2")}
    return d.Outbox(tmp_path / name, targets, sender=receiver, clock=lambda: now["t"], keys=keys)


def queue_both(ob, tenant, call_id):
    rec = record(tenant, call_id)
    ob.enqueue("bridge", rec["idempotency_key"], d.build_bridge_payload(rec, f"{NAME} called about {STORY}."), tenant_id=tenant, call_id=call_id)
    ob.enqueue("console", rec["idempotency_key"], d.build_console_payload(rec, TRANSCRIPT, 60, "Firm"), tenant_id=tenant, call_id=call_id)
    return rec["idempotency_key"]


def raw(path):
    return path.read_bytes()


def assert_no_caller_data(path):
    blob = raw(path)
    leaked = [p for p in PLANTED if p.encode() in blob]
    assert not leaked, f"plaintext caller data in the outbox file: {leaked}"


@pytest.mark.parametrize("tenant", ["ashgrovepell", "tidewaterquint"])
async def test_no_plaintext_caller_data_in_the_file_in_any_state(tmp_path, tenant):
    """Pending, delivered and dead, for both firms and both receivers."""
    now = {"t": 1_000_000.0}
    path = tmp_path / "outbox.jsonl"
    # pending: the receiver is down, so the entry waits with its body in the file
    down = Receiver(503)
    ob = make(tmp_path, down, now)
    queue_both(ob, tenant, "call-pending")
    await ob.deliver_due()
    assert ob.stats() == {"pending": 2, "delivered": 0, "dead": 0}
    assert down.bodies and NAME.encode() in down.bodies[0]  # the plaintext did go to the receiver: that is the delivery
    assert_no_caller_data(path)
    # dead: a permanent refusal
    ob2 = make(tmp_path / "dead", Receiver(422), now)
    queue_both(ob2, tenant, "call-dead")
    await ob2.deliver_due()
    assert ob2.stats() == {"pending": 0, "delivered": 0, "dead": 2}
    assert_no_caller_data(tmp_path / "dead" / "outbox.jsonl")
    # delivered: the body is dropped, only id, status and times stay
    ok = make(tmp_path / "ok", Receiver(200), now)
    queue_both(ok, tenant, "call-ok")
    await ok.deliver_due()
    assert ok.stats() == {"pending": 0, "delivered": 2, "dead": 0}
    assert_no_caller_data(tmp_path / "ok" / "outbox.jsonl")
    for line in (tmp_path / "ok" / "outbox.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        assert entry["sealed"] == "" and "payload" not in entry and entry["status"] == "delivered" and entry["finished_at"] == now["t"]


async def test_a_pending_entry_survives_a_restart_and_is_delivered_from_the_encrypted_file(tmp_path):
    now = {"t": 10.0}
    ob = make(tmp_path, Receiver(503), now)
    queue_both(ob, "ashgrovepell", "c1")
    await ob.deliver_due()
    again_receiver = Receiver(200)
    again = make(tmp_path, again_receiver, now)  # a new process: nothing but the file
    assert again.stats()["pending"] == 2 and all(e.payload is None for e in again.entries.values())
    now["t"] = 100.0
    assert await again.deliver_due() == 2
    assert any(NAME.encode() in b for b in again_receiver.bodies)
    assert_no_caller_data(tmp_path / "outbox.jsonl")


async def test_a_body_is_bound_to_its_firm_call_and_target_so_a_moved_body_is_never_sent(tmp_path):
    now = {"t": 10.0}
    path = tmp_path / "outbox.jsonl"
    ob = make(tmp_path, Receiver(503), now)
    queue_both(ob, "ashgrovepell", "call-a")
    queue_both(ob, "tidewaterquint", "call-b")
    await ob.deliver_due()
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    by = {(x["tenant_id"], x["target"]): x for x in lines}
    a, b = by[("ashgrovepell", "bridge")], by[("tidewaterquint", "bridge")]
    c = by[("ashgrovepell", "console")]
    c["sealed"] = a["sealed"]  # the same firm's own key opens this body, but it belongs to the bridge entry: only the binding refuses it
    a["sealed"], b["sealed"] = b["sealed"], a["sealed"]  # and swap the encrypted bodies of two firms' entries
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    receiver = Receiver(200)
    swapped = make(tmp_path, receiver, now)
    now["t"] = 1000.0
    assert await swapped.deliver_due() == 1  # only the untouched tidewater console entry opens
    assert len(receiver.bodies) == 1 and swapped.stats() == {"pending": 0, "delivered": 1, "dead": 3}
    assert all("cannot be opened" in e.last for e in swapped.dead)


async def test_dead_and_delivered_entries_are_purged_at_their_firms_retention_and_a_waiting_one_never_is(tmp_path):
    now = {"t": 5_000_000.0}
    path = tmp_path / "outbox.jsonl"
    ob = make(tmp_path, Receiver(422), now)
    queue_both(ob, "ashgrovepell", "dead-ash")       # retention 30 days
    queue_both(ob, "tidewaterquint", "dead-tide")    # retention 90 days
    await ob.deliver_due()
    ob.sender = Receiver(200)
    queue_both(ob, "ashgrovepell", "ok-ash")
    await ob.deliver_due()
    ob.sender = Receiver(503)
    queue_both(ob, "ashgrovepell", "waiting-ash")
    await ob.deliver_due()
    assert ob.stats() == {"pending": 2, "delivered": 2, "dead": 4}

    def days(tenant):
        return {"ashgrovepell": 30, "tidewaterquint": 90}.get(tenant)

    now["t"] += 29 * DAY
    assert ob.purge(days) == 0
    now["t"] += 2 * DAY  # day 31: the 30 day firm's finished entries go, the 90 day firm's stay
    assert ob.purge(days) == 4
    assert {e.tenant_id for e in ob.entries.values() if e.status != "pending"} == {"tidewaterquint"}
    assert len(ob.pending) == 2  # a waiting entry is delivered or dead within a day: purge never decides about it
    now["t"] += 60 * DAY
    assert ob.purge(days) == 2 and ob.purge(lambda t: None) == 0
    reread = make(tmp_path, Receiver(200), now)
    assert reread.stats() == {"pending": 2, "delivered": 0, "dead": 0} and path.exists()


async def test_an_old_file_that_kept_the_body_in_the_clear_is_sealed_when_it_is_read(tmp_path):
    now = {"t": 50.0}
    path = tmp_path / "outbox.jsonl"
    rec = record("tidewaterquint", "old-call")
    legacy_pending = {"key": "k-old", "target": "bridge", "payload": d.build_bridge_payload(rec, "s"), "attempts": 1, "next_at": 0.0, "status": "pending",
                      "last": "503 down", "history": ["1:503"]}
    legacy_delivered = {"key": "k-done", "target": "console", "payload": d.build_console_payload(rec, TRANSCRIPT, 5, "Firm"), "attempts": 1, "next_at": 0.0,
                        "status": "delivered", "last": "201 ok", "history": ["1:201"]}
    legacy_orphan = {"key": "k-x", "target": "bridge", "payload": {"hello": NAME}, "attempts": 0, "next_at": 0.0, "status": "pending", "last": "", "history": []}
    path.write_text("".join(json.dumps(x) + "\n" for x in (legacy_pending, legacy_delivered, legacy_orphan)), encoding="utf-8")
    receiver = Receiver(200)
    ob = make(tmp_path, receiver, now)
    assert_no_caller_data(path)  # rewritten at once, before anything was delivered
    assert ob.entries["bridge:k-old"].tenant_id == "tidewaterquint" and ob.entries["console:k-done"].status == "delivered"
    assert ob.entries["bridge:k-x"].status == "dead" and "names no firm" in ob.entries["bridge:k-x"].last
    assert await ob.deliver_due() == 1 and any(NAME.encode() in b for b in receiver.bodies)


async def test_a_line_of_the_file_that_cannot_be_read_does_not_stop_the_rest_from_loading(tmp_path):
    now = {"t": 5.0}
    path = tmp_path / "outbox.jsonl"
    ob = make(tmp_path, Receiver(503), now)
    queue_both(ob, "ashgrovepell", "c1")
    good = path.read_text(encoding="utf-8").splitlines()
    bad = ["{not json", json.dumps([1, 2]), json.dumps({"key": "k", "target": "bridge", "status": "weird"}), json.dumps({"target": "bridge"})]
    path.write_text("\n".join([bad[0], good[0], bad[1], bad[2], good[1], bad[3]]) + "\n", encoding="utf-8")
    again = make(tmp_path, Receiver(200), now)
    assert again.unreadable_lines == 4 and again.stats() == {"pending": 2, "delivered": 0, "dead": 0}


def test_an_entry_needs_a_firm_because_its_body_is_encrypted_under_that_firms_key(tmp_path):
    ob = make(tmp_path, Receiver(), {"t": 0.0})
    with pytest.raises(ValueError):
        ob.enqueue("bridge", "k", {"a": 1}, tenant_id="")
    with pytest.raises(TypeError):
        ob.enqueue("bridge", "k", {"a": 1})  # type: ignore[call-arg]


# ---- the class, enumerated: every firm x every state an entry can rest in x every piece of caller data
#
# GROUNDING (docs/DECISIONS.md): "the file holds no readable caller data in any state." and "so an operator who turns on INFO gets lengths and timings, never words."
# WHY: a firm's intake staff hands the machine to a contractor for disk backup on an ordinary day; the outbox file is in that backup. If a waiting, dead or
# delivered entry keeps a caller's name, number, email or injury story readable, the firm's client is exposed by a backup nobody thought of as a data store.
import itertools
import logging

FIRMS = ["ashgrovepell", "tidewaterquint"]
STATES = ["pending", "dead_refused", "dead_exhausted", "delivered", "requeued", "restarted_while_pending", "old_plaintext_file_read"]
# every piece of what the caller said or the line read back, as the staff would see it
PIECES = [NAME, "Zelda", "Quimby", PHONE, PHONE[1:], "4155550123", EMAIL, "zelda.quimby", "Harbor Street", "forklift", "left foot", "warehouse",
          "crushed my left foot"]
HOLDS_A_BODY = {"pending", "requeued", "restarted_while_pending", "old_plaintext_file_read", "dead_refused", "dead_exhausted"}


async def rest_in_state(base, tenant, state):
    """Put one firm's two entries in `state`, return (raw bytes of the file, text of every log record made on the way, bodies a working receiver then got)."""
    now = {"t": 5_000_000.0}
    folder = base / f"{tenant}-{state}"
    path = folder / "outbox.jsonl"
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    grab = Grab(level=logging.DEBUG)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(grab)
    root.setLevel(logging.DEBUG)
    try:
        if state == "old_plaintext_file_read":
            rec = record(tenant, "old-call")
            lines = [{"key": f"k-{t}", "target": t, "payload": p, "attempts": 1, "next_at": 0.0, "status": "pending", "last": "503 down", "history": ["1:503"],
                      "tenant_id": tenant, "call_id": "old-call"}
                     for t, p in (("bridge", d.build_bridge_payload(rec, f"{NAME} called about {STORY}.")), ("console", d.build_console_payload(rec, TRANSCRIPT, 60, "Firm")))]
            folder.mkdir(parents=True)
            path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
            ob = make(folder, Receiver(503), now)
        elif state in ("dead_refused", "requeued"):
            ob = make(folder, Receiver(422), now)
            queue_both(ob, tenant, "c1")
            await ob.deliver_due()
            if state == "requeued":
                assert ob.requeue() == 2
        elif state == "dead_exhausted":
            ob = make(folder, Receiver(503), now)
            queue_both(ob, tenant, "c1")
            for _ in range(60):
                if not ob.pending:
                    break
                now["t"] = max(now["t"], min(e.next_at for e in ob.pending))
                await ob.deliver_due()
        else:
            ob = make(folder, Receiver(200 if state == "delivered" else 503), now)
            queue_both(ob, tenant, "c1")
            await ob.deliver_due()
            if state == "restarted_while_pending":
                ob = make(folder, Receiver(503), now)  # a new process: nothing but the file
                now["t"] += 30
                await ob.deliver_due()
    finally:
        root.removeHandler(grab)
        root.setLevel(old_level)
    blob, logs, stats = raw(path), "\n".join(r.getMessage() for r in records), ob.stats()  # the file as it rests in this state, before anything is sent again
    got = Receiver(200)
    again = make(folder, got, now)
    now["t"] += 10 * DAY
    again.requeue()  # a dead entry keeps its encrypted body so staff can send it again
    await again.deliver_due()
    return blob, logs, got.bodies, stats


# CLASS-TABLE: caller data in the clear in the outbox file or the logs, in any state, for any firm
async def test_class_no_firm_in_no_state_leaves_a_piece_of_caller_data_readable_in_the_outbox_file_or_the_logs(tmp_path):
    expected_status = {"pending": "pending", "restarted_while_pending": "pending", "requeued": "pending", "old_plaintext_file_read": "pending",
                       "dead_refused": "dead", "dead_exhausted": "dead", "delivered": "delivered"}
    built = {}
    for tenant, state in itertools.product(FIRMS, STATES):
        built[(tenant, state)] = await rest_in_state(tmp_path, tenant, state)
        stats = built[(tenant, state)][3]
        assert stats[expected_status[state]] == 2 and sum(stats.values()) == 2, (tenant, state, stats)  # the state really is the state it names
    leaks = [(tenant, state, piece, where) for (tenant, state, piece) in itertools.product(FIRMS, STATES, PIECES) for where in ("file", "logs")
             if piece.encode() in (built[(tenant, state)][0] if where == "file" else built[(tenant, state)][1].encode())]
    assert not leaks, f"{len(leaks)} cells hold readable caller data: {leaks[:12]}"
    # the cells that must still work: an entry that holds a body is delivered in full when its receiver comes back, a delivered one has nothing left to send
    for tenant, state in itertools.product(FIRMS, STATES):
        bodies = built[(tenant, state)][2]
        if state in HOLDS_A_BODY:
            joined = b"".join(bodies)
            assert len(bodies) == 2 and NAME.encode() in joined and EMAIL.encode() in joined and b"forklift" in joined, (tenant, state, len(bodies))
        else:
            assert bodies == [], (tenant, state)

