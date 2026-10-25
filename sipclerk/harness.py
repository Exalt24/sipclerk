"""Replays a spoken scenario against a running line over real SIP and RTP, and measures what the caller hears.

The caller here is a softphone: it places an INVITE, plays WAV clips as G.711 RTP, sends keypad digits as
RFC 4733 events, and listens. Latency is measured where a person would feel it: from the last RTP packet
of the caller's sentence to the first loud RTP frame of the agent's answer.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sipclerk import audio
from sipclerk.sip.phone import PhoneCall, SoftPhone, StaffEndpoint, goertzel_power, tone
from sipclerk.sip.stack import SipStack
from sipclerk.tenants import TenantRegistry
from voice_scripts.scenarios import Scenario, Step

log = logging.getLogger("sipclerk.harness")
ROOT = Path(__file__).resolve().parent.parent
WAV_DIR = ROOT / "voice_scripts" / "wav"
QUIET_BEFORE_REPLY_S = 1.2
REPLY_WAIT_S = 14.0


def wav_name(voice: str, text: str) -> str:
    import hashlib

    slug = "".join(c if c.isalnum() else "_" for c in text.lower())[:40].strip("_")
    return f"{voice}__{slug}__{hashlib.sha1(text.encode()).hexdigest()[:6]}.wav"


def read_wav_8k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
    return audio.resample(pcm, rate, audio.TELEPHONE_RATE)


def write_wav(path: Path, pcm: bytes, rate: int = audio.TELEPHONE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def make_registry(staff_ports: dict[str, int] | None = None, timeout_scale: float = 1.0, ring_timeout_s: float | None = None) -> TenantRegistry:
    """The two real tenant files, with the desk-phone port and the question timeouts adjusted for a test run.

    The adjusted copy has its own config hash; the repository files are never changed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="sipclerk-tenants-"))
    for f in (ROOT / "tenants").glob("*.yaml"):
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
        if timeout_scale != 1.0:
            for q in raw["questions"]:
                q["timeout_s"] = max(2.5, q.get("timeout_s", 8.0) * timeout_scale)
        if staff_ports and raw["tenant_id"] in staff_ports:
            raw["transfer"]["destination"] = f"sip:desk@127.0.0.1:{staff_ports[raw['tenant_id']]}"
        if ring_timeout_s:
            raw["transfer"]["ring_timeout_s"] = ring_timeout_s
        (tmp / f.name).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return TenantRegistry.from_dir(tmp)


@dataclass
class Played:
    scenario: str
    call: PhoneCall | None
    status: int
    onsets_ms: list[float] = field(default_factory=list)  # end of caller sentence to first agent audio, per answered turn
    no_response: int = 0
    barge_stop_ms: float | None = None
    heard_since_end: bytes = b""
    sip_log: list = field(default_factory=list)
    started: float = 0.0
    ended_by_agent: bool = False
    steps_run: int = 0
    staff_calls_before: int = 0
    bridge: dict = field(default_factory=dict)
    timeline: list = field(default_factory=list)  # (kind, seconds after answer, voice, text or digits): what the caller did and when


async def agent_last_active(call: PhoneCall, since: float) -> float:
    act = call.active_times(since)
    return act[-1] if act else 0.0


async def play(sc: Scenario, port: int, caller_stack: SipStack, staff: StaffEndpoint | None = None, snr_seed: int = 7, busy=None) -> Played:
    phone = SoftPhone(caller_stack)
    out = Played(sc.name, None, 0, started=time.monotonic())
    out.staff_calls_before = len(staff.calls) if staff is not None else 0
    call, status = await phone.call(f"sip:{sc.did}@127.0.0.1:{port}", from_user=sc.caller, timeout=10)
    out.call, out.status = call, status
    if call is None:
        return out
    try:
        for step in sc.steps:
            if call.ended:
                break
            await _run_step(call, step, sc, out, snr_seed, staff, busy)
            out.steps_run += 1
        # let the agent finish what it is saying or doing (a closing line, a failed transfer), unless we were checking a bridge
        end = time.monotonic() + 25
        checked_bridge = any(st.kind == "bridge_check" for st in sc.steps)
        while not call.ended and time.monotonic() < end and not checked_bridge:
            await asyncio.sleep(0.1)
        out.ended_by_agent = call.dialog.ended_by == "remote"
        out.heard_since_end = call.heard_pcm()
    finally:
        if not call.ended:
            await call.hangup()
        else:
            await call.rtp.close()
    return out


async def _agent_quiet(call: PhoneCall, busy=None, timeout: float = 40.0, first: bool = False) -> bool:
    """True once the agent has stopped talking. `busy` is the line's own answer (speaking, or still working out a reply), so a gap
    while it renders a sentence is not mistaken for the end of its turn."""
    if first:  # the agent has not spoken yet: a silent stretch now is the agent preparing its greeting, not the agent being done
        await call.wait_for_speech(30.0, since=call.started)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not await call.wait_for_quiet(QUIET_BEFORE_REPLY_S, max(0.1, end - time.monotonic())):
            return False
        if busy is None or not busy():
            return True
        await asyncio.sleep(0.05)
    return False


async def _run_step(call: PhoneCall, step: Step, sc: Scenario, out: Played, seed: int, staff: StaffEndpoint | None = None, busy=None) -> None:
    if step.kind == "say":
        quiet = await _agent_quiet(call, busy, first=out.steps_run == 0)
        log.info("step say %r: agent quiet=%s at t=%.1f", step.text[:30], quiet, time.monotonic() - out.started)
        if not quiet:
            return
        pcm = _clip(step, sc, seed)
        out.timeline.append(("say", round(time.monotonic() - call.started, 2), step.voice, step.text))
        log.info("caller starts %r (%.1fs)", step.text[:30], len(pcm) / 16000)
        await call.say_and_wait(pcm)
        t_end = time.monotonic()
        log.info("caller done")
        onset = await call.wait_for_speech(REPLY_WAIT_S, since=t_end)
        if onset is None:
            out.no_response += 1
        else:
            out.onsets_ms.append((onset - t_end) * 1000)
    elif step.kind == "barge":
        # wait for the agent to start its next line, let it talk for `delay`, then talk over it
        await call.wait_for_speech(15.0)
        await asyncio.sleep(step.delay)
        t_start = time.monotonic()
        pcm = _clip(step, sc, seed)
        call.say(pcm)
        # how long until the agent goes quiet after we start talking
        quiet_for = 0.35
        while time.monotonic() - t_start < 4.0:
            act = call.active_times(t_start)
            last = act[-1] if act else 0.0
            if last and time.monotonic() - last > quiet_for and last - t_start > 0:
                out.barge_stop_ms = (last - t_start) * 1000
                break
            await asyncio.sleep(0.02)
        await call.wait_sent()
        t_end = time.monotonic()
        onset = await call.wait_for_speech(REPLY_WAIT_S, since=t_end)
        if onset is None:
            out.no_response += 1
        else:
            out.onsets_ms.append((onset - t_end) * 1000)
    elif step.kind == "dtmf":
        if not await _agent_quiet(call, busy):
            return
        out.timeline.append(("dtmf", round(time.monotonic() - call.started, 2), "", step.digits))
        await call.dtmf(step.digits)
        t_end = time.monotonic()
        onset = await call.wait_for_speech(REPLY_WAIT_S, since=t_end)
        if onset is not None:
            out.onsets_ms.append((onset - t_end) * 1000)
    elif step.kind == "bridge_check" and staff is not None:
        await _bridge_check(call, staff, out)
    elif step.kind in ("silence", "wait"):
        end = time.monotonic() + step.secs
        while time.monotonic() < end and not call.ended:
            await asyncio.sleep(0.05)


async def _bridge_check(call: PhoneCall, staff: StaffEndpoint, out: Played) -> None:
    """After a transfer: the caller sends a 1000 Hz tone, the desk phone sends 880 Hz back; each must arrive at the other end."""
    end = time.monotonic() + 25
    while len(staff.calls) <= out.staff_calls_before and time.monotonic() < end:  # a call from an earlier scenario does not count
        await asyncio.sleep(0.05)
    if len(staff.calls) <= out.staff_calls_before:
        out.bridge = {"connected": False}
        return
    sc_call = staff.calls[-1]
    await sc_call.wait_for_speech(20.0, since=sc_call.started)  # the whisper starts (rendering it takes a moment after the answer)
    await sc_call.wait_for_quiet(1.2, 20.0)  # the whisper to staff is over, the bridge is open
    t0 = time.monotonic()
    call.say(tone(1000, 0.8, amp=6000))
    await asyncio.sleep(1.2)
    sc_call.say(tone(880, 0.8, amp=6000))
    await asyncio.sleep(1.5)
    staff_heard = sc_call.heard_pcm(t0)
    caller_heard = call.heard_pcm(t0 + 1.0)
    out.bridge = {"connected": True, "staff_leg_codec": sc_call.codec,
                  "staff_heard_caller_tone": goertzel_power(staff_heard, 1000) > 20 * (goertzel_power(staff_heard, 2500) + 1e-9),
                  "caller_heard_staff_tone": goertzel_power(caller_heard, 880) > 20 * (goertzel_power(caller_heard, 2500) + 1e-9),
                  "whisper_frames": sum(1 for t, f in sc_call.frames if t < t0 and audio.rms(f) > 350)}


def _clip(step: Step, sc: Scenario, seed: int) -> bytes:
    pcm = read_wav_8k(WAV_DIR / wav_name(step.voice, step.text))
    pcm = pcm + bytes(int(audio.TELEPHONE_RATE * 0.05) * 2)
    if sc.snr_db is not None:
        speech = pcm
        pcm = audio.add_noise(speech, sc.snr_db, seed)
    return pcm


async def percentiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    v = sorted(values)

    def pct(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)

    return {"n": len(v), "p50": round(pct(0.5)), "p95": round(pct(0.95)), "min": round(v[0]), "max": round(v[-1])}
