"""Read back, from a running clinic-call-console API, the call this line delivered, as a reviewer would see it (masked).

    python scripts/console_check.py http://127.0.0.1:18080

Signs in with the console's public demo reviewer account (the data is invented), lists calls, and prints the ones this line sent (the agent
name ends in "intake"): the caller label, the outcome and how many personal details the console masked. The transcript
shown is the masked copy; a reviewer cannot reveal the original.
"""
import json
import sys
import urllib.request


def call(base: str, path: str, token: str | None = None, body: dict | None = None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None, method="POST" if body else "GET",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main() -> int:
    base = sys.argv[1]
    token = call(base, "/api/auth/login", body={"username": "reviewer@demo.test", "password": "reviewer-demo-pass"})["token"]
    page = call(base, "/api/calls?size=100", token)
    rows = page["content"] if isinstance(page, dict) else page
    mine = [r for r in rows if r.get("agent", "").endswith("intake")]
    print(f"{len(rows)} calls listed, {len(mine)} from this line")
    for r in mine[:3]:
        detail = call(base, f"/api/calls/{r['id']}", token)
        print(json.dumps({k: r[k] for k in ("agent", "callerLabel", "reason", "outcome", "phiCount")}))
        for line in detail["transcript"][:40]:
            print(f"  {line['speaker']:6} {line['text']}")
    return 0 if mine else 1


if __name__ == "__main__":
    sys.exit(main())
