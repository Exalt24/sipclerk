"""Our console payload against a running clinic-call-console API: created, then a redelivery (duplicate), a forged signature and a stale one.

    python scripts/console_contract.py http://127.0.0.1:18080

Uses the console's documented demo webhook secret and a made-up call. Prints one line per check and exits 1 if any is not what the
console's README and its WebhookSignature code say it must be.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from sipclerk.delivery import build_console_payload, console_signature  # noqa: E402

SECRET = "demo-only-webhook-secret-change-me"


def post(base: str, body: bytes, header: str | None) -> tuple[int, str]:
    h = {"Content-Type": "application/json"}
    if header:
        h["X-Signature"] = header
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/api/ingest/calls", data=body, headers=h, method="POST"), timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    base = sys.argv[1]
    record = {"tenant_id": "ashgrovepell", "call_id": uuid.uuid4().hex[:8], "started_at": "2026-10-26T03:00:00", "caller_number": "+14155550142",
              "outcome": "intake_complete", "fields": {"full_name": {"value": "Casey Rowan"}}}
    payload = build_console_payload(record, [{"speaker": "AGENT", "text": "May I have your first and last name?"},
                                             {"speaker": "CALLER", "text": "Casey Rowan, call me on 415-555-0123"}], 61, "Ashgrove and Pell LLP")
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = int(time.time())
    checks = [
        ("signed delivery is created", post(base, body, console_signature(SECRET, body, ts)), 201),
        ("the same call again is a duplicate", post(base, body, console_signature(SECRET, body, ts)), 200),
        ("a forged signature is refused", post(base, body, "t=%d,v1=%s" % (ts, "0" * 64)), 401),
        ("a stale timestamp is refused", post(base, body, console_signature(SECRET, body, ts - 3600)), 401),
        ("no signature is refused", post(base, body, None), 401),
    ]
    bad = 0
    for name, (status, text), want in checks:
        ok = status == want
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {name}: {status} {text[:90]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
