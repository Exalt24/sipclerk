"""Softphone and staff endpoint, built on the same SIP stack and RTP session as the server.

These are the test and demo callers: a real INVITE with real RTP audio goes from here to the
agent over UDP. They are also what answers the transfer, standing in for a staff desk phone.
"""
from __future__ import annotations

import asyncio
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sipclerk import audio
from sipclerk.sip import message as sm
from sipclerk.sip.rtp import RtpSession
from sipclerk.sip.stack import IncomingInvite, SipStack, local_ip_for

ACTIVE_RMS = 350.0  # a 20 ms frame above this is "someone is talking" on a clean line


def tone(freq: float, seconds: float, amp: float = 8000.0, rate: int = 8000) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def goertzel_power(pcm16: bytes, freq: float, rate: int = 8000) -> float:
    """Relative power at one frequency, used to prove that a tone crossed the bridge."""
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    if len(x) == 0:
        return 0.0
    k = 2 * np.cos(2 * np.pi * freq / rate)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + k * s1 - s2
        s2, s1 = s1, s0
    return float((s1 * s1 + s2 * s2 - k * s1 * s2) / (len(x) ** 2))


@dataclass
class PhoneCall:
    dialog: object
    rtp: RtpSession
    codec: str
    answer_sdp: str
    frames: list[tuple[float, bytes]] = field(default_factory=list)  # (monotonic time, 8 kHz pcm)
    digits: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.rtp.on_frame = lambda pcm: self.frames.append((time.monotonic(), pcm))
        self.rtp.on_dtmf = self.digits.append
        self.dialog.on_dtmf = self.digits.append

    # ---- sending

    def say(self, pcm16_8k: bytes) -> float:
        """Queue audio for the line. Returns how long it will take to play, in seconds."""
        self.rtp.send_linear(pcm16_8k)
        return len(pcm16_8k) / 2 / audio.TELEPHONE_RATE

    async def say_and_wait(self, pcm16_8k: bytes) -> None:
        self.say(pcm16_8k)
        await self.wait_sent()

    async def wait_sent(self, timeout: float = 120.0) -> None:
        end = time.monotonic() + timeout
        while self.rtp.tx_backlog_frames and time.monotonic() < end:
            await asyncio.sleep(0.02)

    async def dtmf(self, digits: str) -> None:
        for d in digits:
            await self.rtp.send_dtmf(d)

    # ---- hearing

    def heard_pcm(self, since: float = 0.0) -> bytes:
        return b"".join(f for t, f in self.frames if t >= since)

    def active_times(self, since: float = 0.0) -> list[float]:
        return [t for t, f in self.frames if t >= since and audio.rms(f) > ACTIVE_RMS]

    async def wait_for_speech(self, timeout: float, since: float | None = None) -> float | None:
        """Time (monotonic) of the first loud frame after `since`, or None on timeout."""
        since = time.monotonic() if since is None else since
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            act = self.active_times(since)
            if act:
                return act[0]
            await asyncio.sleep(0.01)
        return None

    async def wait_for_quiet(self, quiet_s: float, timeout: float) -> bool:
        """True once the agent has been silent for `quiet_s`; False if it kept talking for `timeout`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            act = self.active_times(time.monotonic() - quiet_s - 0.5)
            last = act[-1] if act else 0.0
            if time.monotonic() - max(last, self.started) >= quiet_s and (self.frames or time.monotonic() - self.started > quiet_s):
                return True
            await asyncio.sleep(0.02)
        return False

    @property
    def ended(self) -> bool:
        return self.dialog.terminated.is_set()

    async def hangup(self) -> None:
        await self.dialog.bye()
        await self.rtp.close()

    def save_wav(self, path: str | Path, since: float = 0.0) -> None:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(audio.TELEPHONE_RATE)
            w.writeframes(self.heard_pcm(since))


class SoftPhone:
    """Places calls. One stack, one RTP socket per call."""

    def __init__(self, stack: SipStack, rtp_host: str = "127.0.0.1"):
        self.stack = stack
        self.rtp_host = rtp_host

    async def call(self, to_uri: str, from_user: str = "+14155550142", codecs: list[str] | None = None,
                   timeout: float = 15.0) -> tuple[PhoneCall | None, int]:
        codecs = codecs or [audio.PCMU, audio.PCMA]
        peer_host, peer_port = sm.uri_hostport(to_uri)
        adv = self.stack.advertise_host or local_ip_for(peer_host, peer_port)
        # We offer our own telephone-event payload type 101; open the socket first so the port is known.
        rtp = await RtpSession.open("0.0.0.0", 0, codecs[0], 101)
        sdp = sm.build_sdp(adv, rtp.local_port, codecs, 101)
        res = await self.stack.invite(to_uri, f"sip:{from_user}@{adv}", sdp, timeout=timeout)
        if res.dialog is None:
            await rtp.close()
            return None, res.status
        ans = sm.parse_sdp(res.answer_sdp)
        chosen = ans.codec_names[0]
        rtp.codec = chosen
        rtp.pt = audio.CODEC_PAYLOAD_TYPES[chosen]
        rtp.dtmf_pt = ans.dtmf_pt
        rtp.set_remote(ans.host, ans.port)
        rtp.start()
        return PhoneCall(res.dialog, rtp, chosen, res.answer_sdp), 200


class StaffEndpoint:
    """A desk phone that answers (or does not) when the agent transfers a call to it."""

    def __init__(self, stack: SipStack, mode: str = "answer", answer_delay: float = 0.4):
        self.stack = stack
        self.mode = mode  # "answer", "busy", "no_answer"
        self.answer_delay = answer_delay
        self.calls: list[PhoneCall] = []
        self.invites: list[IncomingInvite] = []
        self.hello_pcm: bytes = b""
        stack.on_invite = self.handle

    async def handle(self, inv: IncomingInvite) -> None:
        self.invites.append(inv)
        if self.mode == "busy":
            inv.reject(486, "Busy Here")
            return
        inv.provisional(180, "Ringing")
        if self.mode == "no_answer":
            await inv.cancelled.wait()
            return
        await asyncio.sleep(self.answer_delay)
        if inv.cancelled.is_set():
            return
        offer = sm.parse_sdp(inv.offer)
        pick = sm.negotiate(offer, [audio.PCMU, audio.PCMA])
        if pick is None:
            inv.reject(488, "Not Acceptable Here")
            return
        codec, _pt = pick
        rtp = await RtpSession.open("0.0.0.0", 0, codec, offer.dtmf_pt)
        rtp.set_remote(offer.host, offer.port)
        host = self.stack.local_host_for(inv.addr)
        d = await inv.answer(sm.build_sdp(host, rtp.local_port, [codec], offer.dtmf_pt), local_host=host)
        rtp.start()
        call = PhoneCall(d, rtp, codec, inv.offer)
        self.calls.append(call)
        if self.hello_pcm:
            call.say(self.hello_pcm)
