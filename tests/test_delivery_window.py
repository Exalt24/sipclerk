"""A delivery is not given up on inside a short window: a receiver that is down for an afternoon still gets it, a dead entry can be
sent again, and every give-up is loud. All on a fake clock; nothing waits."""
import json
import logging
import sys
from pathlib import Path

import pytest

from sipclerk import delivery as d
from sipclerk.server import ClerkServer
from sipclerk.store import TenantStore, derive_key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import requeue_dead  # noqa: E402

MASTER = bytes(range(32)) * 2
TENANT = "ashgrovepell"
HOUR = 3600.0


def keys(tenant):
    return derive_key(MASTER, tenant)


class Receiver:
    """Down (503) until `up_at`, then 200. Records every attempt's headers."""

    def __init__(self, now, up_at=float("inf"), status=503):
        self.now, self.up_at, self.status, self.attempts = now, up_at, status, []

    async def __call__(self, url, body, headers):
        self.attempts.append((self.now["t"], headers))
        return (200, "{}") if self.now["t"] >= self.up_at else (self.status, "down")


def make(tmp_path, receiver, now, kind="bridge"):
    return d.Outbox(tmp_path / "outbox.jsonl", {kind: d.Target(kind, "http://x/hook", "s")}, sender=receiver, clock=lambda: now["t"], keys=keys)


async def run_until_done(ob, now, limit=100):
    """Jump the clock to each entry's next attempt, like a line that is left running."""
    for _ in range(limit):
        waiting = ob.pending
        if not waiting:
            return
        now["t"] = max(now["t"], min(e.next_at for e in waiting))
        await ob.deliver_due()


async def test_a_receiver_down_for_ten_minutes_still_receives_the_delivery_when_it_comes_back(tmp_path):
    now = {"t": 1000.0}
    r = Receiver(now, up_at=1000.0 + 600)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    for _ in range(1500):  # the line checks every second for 25 minutes
        await ob.deliver_due()
        now["t"] += 1
    e = ob.entries["bridge:k1"]
    assert e.status == "delivered" and e.history[-1].endswith(":200") and 1 < e.attempts < d.MAX_ATTEMPTS
    assert r.attempts[-1][0] >= 1600 and len({h["idempotency-key"] for _t, h in r.attempts}) == 1


async def test_the_old_six_attempt_window_would_have_been_dead_after_31_seconds_and_this_one_is_not(tmp_path):
    now = {"t": 0.0}
    r = Receiver(now)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    for _ in range(40):
        await ob.deliver_due()
        now["t"] += 1
    assert ob.entries["bridge:k1"].status == "pending" and ob.entries["bridge:k1"].attempts >= 6


async def test_it_is_dead_only_after_the_documented_window(tmp_path):
    now = {"t": 500.0}
    r = Receiver(now)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await run_until_done(ob, now)
    e = ob.entries["bridge:k1"]
    assert e.status == "dead" and e.attempts == d.MAX_ATTEMPTS == len(r.attempts)
    assert e.finished_at == 500.0 + d.GIVE_UP_AFTER_S and 22 * HOUR < d.GIVE_UP_AFTER_S < 25 * HOUR
    assert max(d.BACKOFF_S) <= 4 * HOUR and d.BACKOFF_S == sorted(d.BACKOFF_S)
    gaps = [b[0] - a[0] for a, b in zip(r.attempts, r.attempts[1:])]
    assert gaps == d.BACKOFF_S


@pytest.mark.parametrize("outage_s", [0, 30, 600, HOUR, 6 * HOUR, 22 * HOUR, d.GIVE_UP_AFTER_S, d.GIVE_UP_AFTER_S + 1, 30 * HOUR])
async def test_class_a_delivery_is_made_whenever_the_receiver_returns_inside_the_window_and_dead_only_beyond_it(tmp_path, outage_s):
    now = {"t": 10_000.0}
    r = Receiver(now, up_at=10_000.0 + outage_s)
    ob = make(tmp_path / str(int(outage_s)), r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await run_until_done(ob, now)
    assert ob.entries["bridge:k1"].status == ("delivered" if outage_s <= d.GIVE_UP_AFTER_S else "dead")


async def test_every_attempt_carries_the_same_key_and_a_fresh_signature_timestamp(tmp_path):
    now = {"t": 1_700_000_000.0}
    r = Receiver(now, up_at=1_700_000_000.0 + 4000)
    ob = make(tmp_path, r, now, kind="console")
    ob.enqueue("console", "k1", {"a": 1}, tenant_id=TENANT)
    await run_until_done(ob, now)
    stamps = [int(h["x-signature"].split(",")[0].split("=")[1]) for _t, h in r.attempts]
    assert len(r.attempts) >= 8 and len(set(stamps)) == len(stamps), "a retry reused an old timestamp"
    assert stamps == [int(t) for t, _h in r.attempts]  # each one is the time of that attempt
    assert {h["idempotency-key"] for _t, h in r.attempts} == {"k1"}


async def test_a_pending_entry_survives_a_restart_in_the_middle_of_the_window(tmp_path):
    now = {"t": 0.0}
    r = Receiver(now, up_at=3 * HOUR)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    for _ in range(12):
        now["t"] = max(now["t"], ob.entries["bridge:k1"].next_at)
        await ob.deliver_due()
    before = ob.entries["bridge:k1"]
    assert before.status == "pending" and before.attempts >= 10
    again = make(tmp_path, r, now)  # the process restarted
    e = again.entries["bridge:k1"]
    assert (e.attempts, e.next_at, e.status) == (before.attempts, before.next_at, "pending")
    await run_until_done(again, now)
    assert again.entries["bridge:k1"].status == "delivered"


async def test_a_dead_entry_is_loud_counted_and_can_be_requeued(tmp_path, caplog):
    now = {"t": 0.0}
    r = Receiver(now, up_at=40 * HOUR)  # back long after the window closed
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    ob.enqueue("bridge", "k2", {"a": 2}, tenant_id=TENANT)
    with caplog.at_level(logging.WARNING, logger="sipclerk.delivery"):
        await run_until_done(ob, now)
    assert ob.stats() == {"pending": 0, "delivered": 0, "dead": 2}
    assert sum("is dead after" in x.getMessage() and x.levelno == logging.WARNING for x in caplog.records) == 2
    now["t"] = 41 * HOUR  # the receiver is back
    assert ob.requeue(key="k1") == 1 and ob.stats() == {"pending": 1, "delivered": 0, "dead": 1}
    await ob.deliver_due()
    e = ob.entries["bridge:k1"]
    assert e.status == "delivered" and e.attempts == 1 and "requeued" in e.history and e.payload is None and e.sealed == ""
    assert ob.requeue(target="console") == 0 and ob.requeue() == 1  # selective, then everything that is left
    await ob.deliver_due()
    assert ob.stats() == {"pending": 0, "delivered": 2, "dead": 0}


async def test_a_permanent_refusal_that_was_fixed_can_be_requeued_too(tmp_path):
    now = {"t": 0.0}
    r = Receiver(now, status=422)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await ob.deliver_due()
    assert ob.stats()["dead"] == 1 and r.attempts[0][0] == 0.0
    r.up_at = 0.0  # the receiver's fix is deployed
    assert ob.requeue() == 1
    await ob.deliver_due()
    assert ob.stats() == {"pending": 0, "delivered": 1, "dead": 0}


async def test_requeue_survives_a_restart_and_leaves_delivered_entries_alone(tmp_path):
    now = {"t": 0.0}
    r = Receiver(now)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await run_until_done(ob, now)
    ob.requeue()
    again = make(tmp_path, r, now)
    assert again.stats() == {"pending": 1, "delivered": 0, "dead": 0} and again.entries["bridge:k1"].attempts == 0
    r.up_at = 0.0
    await again.deliver_due()
    assert again.requeue() == 0  # a delivered entry is not requeued


async def test_the_script_asks_the_running_line_to_requeue_and_can_edit_a_stopped_files(tmp_path, capsys):
    now = {"t": 0.0}
    r = Receiver(now)
    ob = make(tmp_path, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await run_until_done(ob, now)
    path = str(tmp_path / "outbox.jsonl")
    assert requeue_dead.main(["--outbox", path, "--list"], keys=keys) == 0
    assert "ashgrovepell" in capsys.readouterr().out and ob.stats()["dead"] == 1
    # the line is running: the script leaves a request, the line's next pass applies it, and its own file is not overwritten blind
    assert requeue_dead.main(["--outbox", path], keys=keys) == 0
    assert (tmp_path / "outbox.jsonl.requeue").exists() and ob.stats()["dead"] == 1
    r.up_at = 0.0
    await ob.deliver_due()
    assert not (tmp_path / "outbox.jsonl.requeue").exists() and ob.stats() == {"pending": 0, "delivered": 1, "dead": 0}
    # the line is stopped: --now changes the file itself
    ob.enqueue("bridge", "k2", {"a": 2}, tenant_id=TENANT)
    r.up_at = float("inf")
    await run_until_done(ob, now)
    assert requeue_dead.main(["--outbox", path, "--now"], keys=keys) == 0
    assert make(tmp_path, r, now).stats()["pending"] == 1
    assert requeue_dead.main(["--outbox", str(tmp_path / "nothing.jsonl")], keys=keys) == 1


async def test_the_dead_count_is_in_the_servers_status_and_the_hourly_pass_says_so(tmp_path, registry, caplog):
    now = {"t": 0.0}
    r = Receiver(now)
    store = TenantStore(tmp_path, registry, MASTER)
    ob = d.Outbox(tmp_path / "outbox.jsonl", {"bridge": d.Target("bridge", "http://x", "s")}, sender=r, clock=lambda: now["t"], keys=store.key_for)
    server = ClerkServer(registry=registry, models=None, store=store, outbox=ob, host="127.0.0.1", port=0)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    assert server.status()["deliveries"] == {"pending": 1, "delivered": 0, "dead": 0, "waiting": 1}
    await run_until_done(ob, now)
    assert server.status()["deliveries"]["dead"] == 1
    with caplog.at_level(logging.WARNING, logger="sipclerk.server"):
        server.run_retention_pass()
    assert any("dead delivery" in x.getMessage() and "requeue_dead" in x.getMessage() for x in caplog.records)
    json.dumps(server.status())  # it is plain data: a health endpoint can send it as it is


# ---- the class, enumerated: every outage an ordinary day has against the schedule's own attempt positions
#
# GROUNDING (docs/PROOF.md): "a receiver down for ten minutes still gets the delivery, dead only after the 23 hour schedule" and (README.md) "retries last about 23 hours".
# WHY: the firm's case management bridge restarts for an update during the afternoon or its host has a bad night; the delivery of a finished intake is the
# only copy of the caller's details outside the encrypted store. A retry window of half a minute drops that intake silently, the firm never calls the person back.
import itertools

# outages an ordinary week has (the receiver is down this long after the entry was queued), each shorter than the promised day
ORDINARY_OUTAGES_S = [("a restart", 30), ("a ten minute deploy", 600), ("an hour", HOUR), ("an afternoon", 5 * HOUR), ("a night", 12 * HOUR), ("most of a day", 20 * HOUR),
                      ("just under the day", 22 * HOUR)]
EDGES_S = [-1, 0, 1]  # the receiver is back a second early, exactly on the second, a second late
LONG_OUTAGES_S = [("a day and a half", 36 * HOUR), ("two days", 48 * HOUR)]
SCHEDULE_AT = [sum(d.BACKOFF_S[:i]) for i in range(d.MAX_ATTEMPTS)]  # when attempt 1, 2, 3 ... is made, counted from the queueing


async def one_delivery(tmp_path, outage_s, tag):
    now = {"t": 10_000.0}
    r = Receiver(now, up_at=10_000.0 + outage_s)
    ob = make(tmp_path / tag, r, now)
    ob.enqueue("bridge", "k1", {"a": 1}, tenant_id=TENANT)
    await run_until_done(ob, now)
    return ob, r, ob.entries["bridge:k1"]


# CLASS-TABLE: a delivery given up inside a short retry window, for every outage and every position in the schedule
async def test_class_a_receiver_that_returns_after_any_ordinary_outage_still_gets_the_delivery_once_and_one_that_never_does_is_dead_and_loud(tmp_path, caplog):
    cells = list(itertools.product(ORDINARY_OUTAGES_S, EDGES_S))
    failed = []
    for ((label, base), edge), n in zip(cells, itertools.count()):
        outage = base + edge
        ob, r, e = await one_delivery(tmp_path, outage, f"o{n}")
        keys = {h["idempotency-key"] for _t, h in r.attempts}
        if not (e.status == "delivered" and keys == {"k1"} and len(r.attempts) == e.attempts and r.attempts[-1][0] >= 10_000.0 + outage and ob.stats()["dead"] == 0):
            failed.append((label, outage, e.status, e.attempts))
    assert not failed, f"{len(failed)} of {len(cells)} outages lost the delivery: {failed}"
    # the rows that must be refused: no receiver within the schedule is a dead letter, said once at WARNING, counted, and sent again by requeue
    for (label, base), n in zip(LONG_OUTAGES_S, itertools.count(100)):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="sipclerk.delivery"):
            ob, r, e = await one_delivery(tmp_path, base, f"long{n}")
        assert e.status == "dead" and e.attempts == len(r.attempts) and ob.stats() == {"pending": 0, "delivered": 0, "dead": 1}, label
        assert sum("is dead after" in x.getMessage() and x.levelno == logging.WARNING for x in caplog.records) == 1, label
        assert ob.requeue() == 1 and ob.stats()["pending"] == 1, label


async def test_class_every_position_in_the_schedule_delivers_when_the_receiver_returns_at_it_and_only_the_second_after_the_last_is_dead(tmp_path):
    """The schedule's own attempt times, a second before, on and a second after each: delivered while an attempt is still to come, dead after the last."""
    wrong = []
    for (i, at), edge in itertools.product(enumerate(SCHEDULE_AT), EDGES_S):
        outage = at + edge
        if outage < 0:
            continue
        _ob, _r, e = await one_delivery(tmp_path, outage, f"p{i}_{edge}")
        want = "delivered" if outage <= SCHEDULE_AT[-1] else "dead"
        if e.status != want:
            wrong.append((i, edge, outage, e.status, want))
    assert not wrong, wrong
