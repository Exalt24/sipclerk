"""Send dead deliveries again.

    python scripts/requeue_dead.py --list                       # the dead entries: firm, target, attempts, what the receiver said last
    python scripts/requeue_dead.py                              # every dead entry gets a fresh set of attempts, starting now
    python scripts/requeue_dead.py --target bridge --key <id>   # only that receiver, only that delivery
    python scripts/requeue_dead.py --now                        # the line is stopped: change the file itself

Without --now this leaves a request file beside the outbox and the RUNNING line applies it on its next pass (a running line writes its own
copy of the outbox over the file every pass, so a change made to the file under it would be lost). A dead entry keeps its encrypted body
until its firm's retention_days, so it can be sent again until then.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sipclerk.delivery import Outbox  # noqa: E402
from sipclerk.store import derive_key  # noqa: E402


def main(argv: list[str] | None = None, keys=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--outbox", default=str(ROOT / "data" / "outbox.jsonl"))
    ap.add_argument("--target")
    ap.add_argument("--key")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--now", action="store_true", help="edit the file directly (the line must be stopped)")
    a = ap.parse_args(argv)
    path = Path(a.outbox)
    if not path.exists():
        print(f"no outbox at {path}")
        return 1
    if keys is None:
        from sipclerk.server import master_key_from_env

        master = master_key_from_env()
        keys = lambda tenant: derive_key(master, tenant)  # noqa: E731
    box = Outbox(path, {}, keys=keys)  # reading never opens a body, so no receiver is needed
    dead = [e for e in box.dead if (a.target is None or e.target == a.target) and (a.key is None or e.key == a.key)]
    if a.list or not dead:
        for e in dead:
            age_h = (time.time() - (e.finished_at or time.time())) / 3600
            print(f"{e.tenant_id:16} {e.target:8} {e.key[:12]}  {e.attempts} attempts, dead {age_h:.1f} h ago: {e.last[:60]}")
        print(f"{len(dead)} dead entr{'y' if len(dead) == 1 else 'ies'} {'match' if a.list else 'to send again'}")
        return 0
    if a.now:
        n = box.requeue(key=a.key, target=a.target)
        print(f"{n} entr{'y' if n == 1 else 'ies'} queued again in {path}")
        return 0
    marker = box._requeue_marker()
    marker.write_text(json.dumps({"key": a.key, "target": a.target}), encoding="utf-8")
    print(f"{len(dead)} dead entr{'y' if len(dead) == 1 else 'ies'} will be queued again by the running line on its next pass ({marker.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
