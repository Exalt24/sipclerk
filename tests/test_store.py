import json
import os
from datetime import datetime, timedelta

import pytest

from sipclerk import store as st
from sipclerk.store import AccessDenied, Actor, TenantStore

MASTER = bytes(range(32)) * 2
NOW = datetime(2026, 10, 27, 12, 0)
RECORD = {"outcome": "intake_complete", "fields": {"full_name": {"value": "Jordan Reyes"}}}
TRANSCRIPT = [{"speaker": "CALLER", "text": "My name is Jordan Reyes"}]


@pytest.fixture
def store(tmp_path, registry):
    return TenantStore(tmp_path, registry, MASTER, clock=lambda: NOW)


def put(store, tenant="ashgrovepell", call="call-1", recording=b"\x01\x00" * 800):
    return store.save_call(tenant, call, RECORD, TRANSCRIPT, "Summary for staff.", recording)


def test_nothing_readable_is_on_disk(store, tmp_path):
    put(store)
    blob = b"".join(p.read_bytes() for p in (tmp_path / "tenants").rglob("*.enc"))
    assert b"Jordan" not in blob and b"RIFF" not in blob and b"Summary" not in blob


def test_staff_can_read_text_parts_and_attorney_can_play_the_recording(store):
    put(store)
    staff = Actor("sam", "ashgrovepell", "intake_staff")
    lawyer = Actor("lee", "ashgrovepell", "attorney")
    assert store.read(staff, "ashgrovepell", "call-1", "record")["fields"]["full_name"]["value"] == "Jordan Reyes"
    assert store.read(staff, "ashgrovepell", "call-1", "transcript") == TRANSCRIPT
    assert store.read(staff, "ashgrovepell", "call-1", "summary") == "Summary for staff."
    assert store.read(lawyer, "ashgrovepell", "call-1", "recording")[:4] == b"RIFF"


def test_least_privilege_staff_cannot_play_recordings(store):
    put(store)
    with pytest.raises(AccessDenied):
        store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", "call-1", "recording")


def test_another_firms_staff_cannot_read_even_with_a_matching_role(store):
    put(store)
    spy = Actor("eve", "tidewaterquint", "attorney")
    for part in st.PARTS:
        with pytest.raises(AccessDenied):
            store.read(spy, "ashgrovepell", "call-1", part)


def test_every_read_leaves_one_audit_line_and_denials_are_audited_too(store):
    put(store)
    store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", "call-1", "record")
    with pytest.raises(AccessDenied):
        store.read(Actor("eve", "tidewaterquint", "attorney"), "ashgrovepell", "call-1", "record")
    lines = store.audit_lines("ashgrovepell")
    reads = [x for x in lines if x["action"] == "read"]
    assert [x["outcome"][:6] for x in reads] == ["ok", "denied"] and [x["actor"] for x in reads] == ["sam", "eve"]
    assert store.verify_audit("ashgrovepell")


def test_an_edited_or_removed_audit_line_is_detected(store):
    put(store)
    for i in range(3):
        store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", "call-1", "summary")
    path = store._audit_path("ashgrovepell")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    edited = json.loads(lines[2])
    edited["actor"] = "nobody"
    path.write_text("\n".join([lines[0], lines[1], json.dumps(edited), lines[3]]) + "\n", encoding="utf-8")
    assert not store.verify_audit("ashgrovepell")
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")
    assert not store.verify_audit("ashgrovepell")
    path.write_text(original, encoding="utf-8")
    assert store.verify_audit("ashgrovepell")


def test_a_file_moved_to_another_call_or_tenant_will_not_decrypt(store, tmp_path):
    put(store, call="call-1")
    put(store, call="call-2")
    src = tmp_path / "tenants/ashgrovepell/calls/call-1/summary.enc"
    dst = tmp_path / "tenants/ashgrovepell/calls/call-2/summary.enc"
    dst.write_bytes(src.read_bytes())
    with pytest.raises(Exception):
        store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", "call-2", "summary")
    key_b = st.derive_key(MASTER, "tidewaterquint")
    with pytest.raises(Exception):
        st.decrypt(key_b, src.read_bytes(), b"ashgrovepell|call-1|summary")


def test_keys_differ_per_tenant_and_master_must_be_long_enough(registry, tmp_path):
    assert st.derive_key(MASTER, "a") != st.derive_key(MASTER, "b") and len(st.derive_key(MASTER, "a")) == 32
    with pytest.raises(ValueError):
        TenantStore(tmp_path, registry, b"short")


def test_retention_deletes_only_expired_calls_per_tenant_setting(registry, tmp_path):
    clock = {"now": NOW}
    s = TenantStore(tmp_path, registry, MASTER, clock=lambda: clock["now"])
    put(s, "ashgrovepell", "old")     # retention 30 days
    put(s, "tidewaterquint", "old")   # retention 90 days
    clock["now"] = NOW + timedelta(days=20)
    put(s, "ashgrovepell", "new")
    assert s.purge(NOW + timedelta(days=31)) == [("ashgrovepell", "old")]
    assert s.list_calls("ashgrovepell") == ["new"] and s.list_calls("tidewaterquint") == ["old"]
    assert s.purge(NOW + timedelta(days=91)) == [("ashgrovepell", "new"), ("tidewaterquint", "old")]
    assert s.list_calls("tidewaterquint") == []
    assert any(x["action"] == "purge" for x in s.audit_lines("ashgrovepell")) and s.verify_audit("ashgrovepell")


def test_bad_inputs_are_refused(store):
    with pytest.raises(KeyError):
        store.save_call("nope", "c", RECORD, TRANSCRIPT, "s", None)
    with pytest.raises(ValueError):
        store.save_call("ashgrovepell", "../escape", RECORD, TRANSCRIPT, "s", None)
    with pytest.raises(ValueError):
        store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", "c", "everything")


def test_no_recording_part_when_the_caller_did_not_consent(store):
    assert put(store, recording=None) == ["record", "summary", "transcript"]
    with pytest.raises(FileNotFoundError):
        store.read(Actor("lee", "ashgrovepell", "attorney"), "ashgrovepell", "call-1", "recording")
    assert os.path.exists(store._call_dir("ashgrovepell", "call-1") / "meta.json")
