"""A SIPp container places a real INVITE with RTP audio at the line, and the line answers it with the real pipeline.

    docker build -t sipclerk-sipp tools/sipp
    python scripts/replay.py sipp_reference --json scratch/ref.json
    python scripts/make_sipp_media.py scratch/ref.json tools/sipp/media/call.pcap
    python scripts/sipp_demo.py --out scratch/sipp_run.json

SIPp is not our code: it is the Debian `sip-tester` package. It offers PCMA first, PCMU second and telephone-event; the line answers
with its own preference (PCMU), so the run shows a negotiation, not a default. The caller speech and the keypad digits come from the
pcap built from a reference run. SIPp cannot react to the agent, so the run reproduces the reference only if the timing holds, and the
output says whether it did.
"""
import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sipclerk.labrun import LabRig  # noqa: E402
from sipclerk.voice import models as vm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PORT = 5070
DID = "+13055550102"


async def main(out: Path) -> int:
    logging.basicConfig(level=logging.WARNING)
    vm.quiet_pipecat_logs()
    models = vm.load()
    logs = ROOT / "scratch" / "sipp_logs"
    logs.mkdir(parents=True, exist_ok=True)
    for f in logs.glob("*"):
        f.unlink()
    async with LabRig(models, host="0.0.0.0", port=PORT, rtp_host="0.0.0.0", timeout_scale=1.0) as rig:
        print("warm", await rig.warm())
        cmd = ["docker", "run", "--rm", "-v", f"{ROOT / 'tools' / 'sipp'}:/work", "-v", f"{logs}:/logs", "sipclerk-sipp",
               "-sf", "/work/uac_intake.xml", "-s", DID, f"host.docker.internal:{PORT}", "-m", "1", "-nostdin", "-trace_msg",
               "-message_file", "/logs/messages.log", "-trace_err", "-error_file", "/logs/errors.log", "-timeout", "230s"]
        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        proc = await loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=290))
        took = time.monotonic() - t0
        end = loop.time() + 20
        while not rig.results and loop.time() < end:
            await asyncio.sleep(0.2)
        res = rig.results[0] if rig.results else None
        report = {"sipp_exit": proc.returncode, "sipp_seconds": round(took, 1), "sipp_stdout_tail": proc.stdout[-1500:],
                  "server_sip_log": rig.server.stack.log, "server_counters": rig.server.counters}
        if res:
            r = res.record
            report.update({"tenant_id": r["tenant_id"], "config_version": r["config_version"], "config_hash": r["config_hash"], "codec": r["codec"], "rtp": r["rtp"], "outcome": r["outcome"], "ended_by": r["ended_by"], "duration_s": r["duration_s"],
                           "fields": {k: v["value"] for k, v in r["fields"].items()}, "field_sources": {k: v["source"] for k, v in r["fields"].items()},
                           "transcript": res.transcript, "usage": r["usage"], "events": [(e["kind"], e.get("reason", "")) for e in r["events"]]})
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in ("server_sip_log", "sipp_stdout_tail", "transcript", "usage")}, default=str)[:1800])
        for t in report.get("transcript", []):
            print(f"   {t['speaker']:6} {t['text']}")
        print("sipp exit", proc.returncode)
    return 0 if res and proc.returncode == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/sipp_run.json")
    sys.exit(asyncio.run(main(Path(ap.parse_args().out))))
