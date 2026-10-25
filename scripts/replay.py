"""Replay one or more spoken scenarios through the real line and print what happened.

    python scripts/replay.py normal_intake
    python scripts/replay.py normal_intake barge_in --json out.json
    python scripts/replay.py normal_intake --deliver      # post to real receivers: BRIDGE_URL and CONSOLE_URL in the environment
    python scripts/replay.py --list

With --deliver the finished intake goes over real HTTP to the two receivers (clio-matter-bridge's verification code through
tools/bridge_receiver.mjs, and the clinic-call-console API), signed with their documented secrets, and the outbox state is printed.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sipclerk.delivery import httpx_sender  # noqa: E402
from sipclerk.harness import percentiles  # noqa: E402
from sipclerk.labrun import LabRig  # noqa: E402
from sipclerk.voice import models as vm  # noqa: E402
from voice_scripts.scenarios import BY_NAME, SCENARIOS  # noqa: E402


async def main(names: list[str], out: str | None, deliver: bool) -> int:
    logging.basicConfig(level=logging.INFO if "--debug" in sys.argv else logging.WARNING, format="%(relativeCreated)7d %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    vm.quiet_pipecat_logs()
    models = vm.load()
    rows = []
    kw = {}
    if deliver:
        kw = {"sender": httpx_sender, "bridge_url": os.environ["BRIDGE_URL"], "console_url": os.environ["CONSOLE_URL"]}
    async with LabRig(models, **kw) as rig:
        print("warm", await rig.warm())
        for n in names:
            sc = BY_NAME[n]
            played, result = await rig.run(sc)
            rec = result.record if result else {}
            row = {"scenario": n, "sip_status": played.status, "outcome": rec.get("outcome"), "ended_by": rec.get("ended_by"),
                   "codec": rec.get("codec"), "usage": rec.get("usage"), "duration_s": rec.get("duration_s"), "onsets_ms": [round(x) for x in played.onsets_ms],
                   "no_response": played.no_response, "barge_stop_ms": played.barge_stop_ms, "timeline": played.timeline,
                   "fields": {k: v["value"] for k, v in rec.get("fields", {}).items()},
                   "transcript": result.transcript if result else []}
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "transcript"}))
            for t in row["transcript"]:
                print(f"   {t['speaker']:6} {t['text']}")
        print("latency", await percentiles([x for r in rows for x in r["onsets_ms"]]))
        if deliver:
            end = asyncio.get_running_loop().time() + 20
            while rig.outbox.pending and asyncio.get_running_loop().time() < end:
                await asyncio.sleep(0.5)
            for e in rig.outbox.entries.values():
                print(f"delivery {e.target:8} {e.status:10} attempts={e.attempts} last={e.last}")
            rows[-1]["deliveries"] = [{"target": e.target, "status": e.status, "attempts": e.attempts, "last": e.last} for e in rig.outbox.entries.values()]
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--deliver", action="store_true")
    a = ap.parse_args()
    if a.list:
        for s in SCENARIOS:
            print(f"{s.name:28} {s.note}")
        sys.exit(0)
    sys.exit(asyncio.run(main(a.names, a.json, a.deliver)))
