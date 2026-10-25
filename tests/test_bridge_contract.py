"""Our webhook against the REAL verification code of clio-matter-bridge (imported, never copied or changed).

Skipped when node or the sibling clio-matter-bridge checkout is not there, which is the case in CI. On the lab machine it runs
tools/bridge_receiver.mjs, which calls the bridge's own handleEvent.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from conftest import IN_HOURS, ROOT
from sipclerk import delivery as d
from sipclerk.store import derive_key
from sipclerk.intake.machine import IntakeMachine

BRIDGE_DIR = Path(os.environ.get("CLIO_BRIDGE_DIR", ROOT.parent / "clio-matter-bridge"))
pytestmark = pytest.mark.skipif(not (shutil.which("node") and (BRIDGE_DIR / "src/lib/webhook.mjs").exists()),
                                reason="node or the clio-matter-bridge checkout is not available")
SECRET = "lab-bridge-secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def receiver():
    port = free_port()
    env = dict(os.environ, CLIO_BRIDGE_DIR=str(BRIDGE_DIR), BRIDGE_SECRET=SECRET, PORT=str(port))
    p = subprocess.Popen(["node", str(ROOT / "tools" / "bridge_receiver.mjs")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        p.kill()
        pytest.fail("the receiver did not start")
    yield url
    p.kill()


def record(tenant, call_id):
    m = IntakeMachine(tenant, call_id, "+14155550142", IN_HOURS)
    m.begin()
    for u in ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no", "I was in a car accident",
              "about two weeks ago", "A truck ran a red light and hit my car on Main Street"]:
        m.handle_text(u)
    return m.record()


async def deliver(url, secret, rec):
    box = d.Outbox(Path(tempfile.mkdtemp()) / "outbox.jsonl", {"bridge": d.Target("bridge", url, secret)}, sender=d.httpx_sender, keys=lambda t: derive_key(bytes(range(32)) * 2, t))
    box.enqueue("bridge", rec["idempotency_key"], d.build_bridge_payload(rec, "summary"), tenant_id="ashgrovepell")
    await box.deliver_due()
    return box


async def test_a_signed_intake_is_accepted_by_the_bridges_own_verifier_and_a_redelivery_is_a_duplicate(receiver, ashgrove):
    rec = record(ashgrove, "bridge-call-1")
    box = await deliver(receiver, SECRET, rec)
    assert box.stats() == {"pending": 0, "delivered": 1, "dead": 0} and "accepted" in box.entries[f"bridge:{rec['idempotency_key']}"].last
    again = await deliver(receiver, SECRET, rec)  # a fresh outbox, the same payload: the receiver must say duplicate
    assert again.stats()["delivered"] == 1 and "duplicate" in again.entries[f"bridge:{rec['idempotency_key']}"].last


async def test_the_wrong_secret_is_rejected_and_becomes_a_dead_letter_at_once(receiver, ashgrove):
    box = await deliver(receiver, "not-the-secret", record(ashgrove, "bridge-call-2"))
    e = next(iter(box.entries.values()))
    assert e.status == "dead" and e.attempts == 1 and "401" in e.last and "bad_signature" in e.last


async def test_a_body_changed_after_signing_is_rejected(receiver, ashgrove):
    rec = record(ashgrove, "bridge-call-3")
    payload = d.build_bridge_payload(rec, "summary")
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = d.bridge_signature(SECRET, raw)
    r = httpx.post(receiver, content=raw.replace(b"Jordan", b"Jorden"), headers={"x-hook-signature": sig})
    assert r.status_code == 401 and r.json()["reason"] == "bad_signature"
    assert httpx.post(receiver, content=raw).status_code == 401  # no signature at all
    ok = httpx.post(receiver, content=raw, headers={"x-hook-signature": sig})
    assert ok.status_code == 200 and ok.json()["result"]["stored_intake"] == "bridge-call-3"
