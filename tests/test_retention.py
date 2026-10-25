"""Retention is applied by the running line, not only offered as a method: at start, then on an interval, with a fake clock."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import pytest

from sipclerk import server as srv
from sipclerk.delivery import Outbox
from sipclerk.server import ClerkServer
from sipclerk.store import TenantStore

MASTER = bytes(range(32)) * 2
T0 = datetime(2026, 10, 1, 12, 0)
RECORD = {"outcome": "intake_complete", "fields": {"full_name": {"value": "Jordan Reyes"}}}


def build(tmp_path, registry, clock, **kw):
    store = TenantStore(tmp_path, registry, MASTER, clock=lambda: clock["now"])
    async def sender(url, body, headers):
        return 200, "{}"
    outbox = Outbox(tmp_path / "outbox.jsonl", {}, sender=sender)
    server = ClerkServer(registry=registry, models=None, store=store, outbox=outbox, host="127.0.0.1", port=0,
                         now_fn=lambda: clock["now"], **kw)
    return server, store


def put(store, tenant, call):
    store.save_call(tenant, call, RECORD, [], "summary", None)


async def until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def test_a_call_older_than_its_firms_retention_is_gone_after_the_loop_ran_and_each_firm_keeps_its_own_window(tmp_path, registry):
    clock = {"now": T0}
    server, store = build(tmp_path, registry, clock, retention_every_s=0.05)
    put(store, "ashgrovepell", "old")      # retention 30 days
    put(store, "tidewaterquint", "old")    # retention 90 days
    clock["now"] = T0 + timedelta(days=20)
    put(store, "ashgrovepell", "newer")
    await server.start()
    try:
        clock["now"] = T0 + timedelta(days=45)  # ash "old" is 45 days old, "newer" 25; tide "old" is 45 days old
        assert await until(lambda: store.list_calls("ashgrovepell") == ["newer"])
        assert store.list_calls("tidewaterquint") == ["old"]  # the other firm's 90 days still hold
        clock["now"] = T0 + timedelta(days=100)
        assert await until(lambda: store.list_calls("ashgrovepell") == [] and store.list_calls("tidewaterquint") == [])
    finally:
        await server.stop()
    assert any(x["action"] == "purge" and x["call_id"] == "old" for x in store.audit_lines("ashgrovepell"))
    assert any(x["action"] == "purge" for x in store.audit_lines("tidewaterquint")) and store.verify_audit("tidewaterquint")
    assert server.retention["passes"] >= 2 and server.retention["calls_purged"] == 3


async def test_the_first_pass_runs_at_start_with_the_default_interval(tmp_path, registry):
    clock = {"now": T0}
    server, store = build(tmp_path, registry, clock)  # default interval: nothing would happen for an hour
    put(store, "ashgrovepell", "expired")
    clock["now"] = T0 + timedelta(days=31)  # the line was down for a month
    put(store, "ashgrovepell", "fresh")
    await server.start()
    try:
        assert store.list_calls("ashgrovepell") == ["fresh"]  # already true when start() returns: no waiting for an interval
    finally:
        await server.stop()


def test_the_default_interval_is_one_hour_and_a_nonsense_interval_is_refused(tmp_path, registry):
    assert srv.RETENTION_EVERY_S == 3600.0
    server, _ = build(tmp_path, registry, {"now": T0})
    assert server.retention_every_s == 3600.0
    for bad in (0, -5):
        with pytest.raises(ValueError):
            build(tmp_path, registry, {"now": T0}, retention_every_s=bad)


async def test_a_failing_pass_is_logged_and_the_next_interval_tries_again(tmp_path, registry, caplog, monkeypatch):
    clock = {"now": T0}
    server, store = build(tmp_path, registry, clock, retention_every_s=0.05)
    put(store, "ashgrovepell", "expired")
    real = store.purge
    attempts = []

    def flaky(now=None):
        attempts.append(1)
        if len(attempts) <= 2:
            raise OSError("the disk was unplugged")
        return real(now)

    monkeypatch.setattr(store, "purge", flaky)
    clock["now"] = T0 + timedelta(days=40)
    with caplog.at_level(logging.ERROR, logger="sipclerk.server"):
        await server.start()
        try:
            assert await until(lambda: store.list_calls("ashgrovepell") == [])
        finally:
            await server.stop()
    assert server.retention["failures"] == 2 and server.retention["passes"] >= 1
    assert sum("retention pass failed" in r.getMessage() for r in caplog.records) == 2


async def test_a_half_written_folder_in_the_store_does_not_stop_the_loop_purging_the_rest(tmp_path, registry):
    clock = {"now": T0}
    server, store = build(tmp_path, registry, clock, retention_every_s=0.05)
    put(store, "ashgrovepell", "a-half")
    put(store, "ashgrovepell", "b-old")
    put(store, "tidewaterquint", "old")
    (store._call_dir("ashgrovepell", "a-half") / "meta.json").unlink()
    clock["now"] = T0 + timedelta(days=100)
    await server.start()
    try:
        assert await until(lambda: store.list_calls("ashgrovepell") == ["a-half"] and store.list_calls("tidewaterquint") == [])
    finally:
        await server.stop()
    assert server.retention["failures"] == 0 and server.retention["folders_skipped"] == 1


async def test_stop_ends_the_retention_task_and_a_broken_audit_chain_is_reported(tmp_path, registry):
    clock = {"now": T0}
    server, store = build(tmp_path, registry, clock, retention_every_s=0.05)
    put(store, "ashgrovepell", "c1")
    path = store._audit_path("ashgrovepell")
    path.write_text(path.read_text(encoding="utf-8").replace('"actor": "system"', '"actor": "mallory"'), encoding="utf-8")
    await server.start()
    try:
        assert await until(lambda: server.retention["audit_broken"] == ["ashgrovepell"])
    finally:
        await server.stop()
    assert server._retention_task.done()
