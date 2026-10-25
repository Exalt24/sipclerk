"""RTP over UDP (RFC 3550) with G.711 audio and RFC 4733 DTMF events.

One RtpSession per call leg. It sends a steady 20 ms stream (queued speech, or silence, so a NAT
mapping never goes cold), decodes what arrives into 20 ms linear frames for the speech pipeline,
and turns telephone-event packets into digit callbacks.

Symmetric RTP: the first packet that arrives fixes where we send, so a caller behind NAT (or a
Docker bridge) that advertised an unreachable address in its SDP still hears us.
"""
from __future__ import annotations

import asyncio
import random
import struct
import time
from collections import deque
from collections.abc import Callable

from sipclerk import audio

RTP_VERSION = 2
DTMF_EVENTS = "0123456789*#ABCD"


def build_packet(pt: int, seq: int, ts: int, ssrc: int, payload: bytes, marker: bool = False) -> bytes:
    b0 = RTP_VERSION << 6
    b1 = (0x80 if marker else 0) | (pt & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


def parse_packet(data: bytes) -> tuple[int, int, int, int, bool, bytes] | None:
    """(pt, seq, ts, ssrc, marker, payload), or None for something that is not RTP."""
    if len(data) < 12 or data[0] >> 6 != RTP_VERSION:
        return None
    cc = data[0] & 0x0F
    has_ext = bool(data[0] & 0x10)
    marker = bool(data[1] & 0x80)
    pt = data[1] & 0x7F
    seq, ts, ssrc = struct.unpack("!HII", data[2:12])
    off = 12 + 4 * cc
    if has_ext:
        if len(data) < off + 4:
            return None
        off += 4 + 4 * struct.unpack("!H", data[off + 2:off + 4])[0]
    payload = data[off:]
    if data[0] & 0x20 and payload:  # padding
        payload = payload[: -payload[-1]]
    return pt, seq, ts, ssrc, marker, payload


def dtmf_payload(event: int, end: bool, duration: int, volume: int = 10) -> bytes:
    return struct.pack("!BBH", event, (0x80 if end else 0) | (volume & 0x3F), duration)


class RtpSession(asyncio.DatagramProtocol):
    def __init__(self, codec: str, dtmf_pt: int | None, *, send_pt: int | None = None, latch: bool = True):
        self.codec = codec
        self.pt = send_pt if send_pt is not None else audio.CODEC_PAYLOAD_TYPES[codec]
        self.dtmf_pt = dtmf_pt
        self.latch = latch
        self.remote: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.rx_frames: deque[bytes] = deque()  # decoded 8 kHz linear, 20 ms each
        self.on_dtmf: Callable[[str], None] | None = None
        self.on_frame: Callable[[bytes], None] | None = None
        self.packets_in = 0
        self.packets_out = 0
        self.bytes_in = 0
        self._tx: deque[bytes] = deque()  # encoded G.711 frames waiting to go out
        self._partial = b""  # the tail of the last chunk that does not fill a 20 ms frame yet
        self.on_tx: Callable[[bytes], None] | None = None  # every payload sent, for the call recording
        self._last_seq: int | None = None
        self._last_dtmf_ts: int | None = None
        self._ticker: asyncio.Task | None = None
        self._closed = False
        self._marker_next = True
        self.bridge_to: RtpSession | None = None
        self.local_port = 0
        self.tx_active_frames = 0  # frames of real audio sent (not silence)

    # ---- socket

    @classmethod
    async def open(cls, host: str, port: int, codec: str, dtmf_pt: int | None, **kw) -> RtpSession:
        loop = asyncio.get_running_loop()
        session = cls(codec, dtmf_pt, **kw)
        transport, _ = await loop.create_datagram_endpoint(lambda: session, local_addr=(host, port))
        session.transport = transport
        session.local_port = transport.get_extra_info("sockname")[1]
        return session

    def connection_made(self, transport) -> None:  # noqa: D401
        self.transport = transport

    def set_remote(self, host: str, port: int) -> None:
        self.remote = (host, port)

    def start(self) -> None:
        if self._ticker is None:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())

    async def close(self) -> None:
        self._closed = True
        if self._ticker:
            self._ticker.cancel()
            try:
                await self._ticker
            except (asyncio.CancelledError, Exception):
                pass
        if self.transport:
            self.transport.close()

    # ---- receive

    def datagram_received(self, data: bytes, addr) -> None:
        pkt = parse_packet(data)
        if pkt is None:
            return
        pt, seq, ts, _ssrc, _marker, payload = pkt
        if self.latch and self.remote != addr and self.packets_in < 5:
            self.remote = addr  # symmetric RTP: answer where the audio came from
        self.packets_in += 1
        self.bytes_in += len(payload)
        if self.dtmf_pt is not None and pt == self.dtmf_pt:
            self._handle_dtmf(ts, payload)
            return
        if pt not in audio.STATIC_PAYLOAD_TYPES:
            return
        codec = audio.STATIC_PAYLOAD_TYPES[pt]
        if self._last_seq is not None:
            gap = (seq - self._last_seq) & 0xFFFF
            if gap == 0 or gap > 0x8000:
                return  # duplicate or late: drop
            if 1 < gap <= 10:  # short loss: keep the timeline with silence
                for _ in range(gap - 1):
                    self._push_rx(bytes(audio.FRAME_SAMPLES * 2))
        self._last_seq = seq
        pcm = audio.decode(payload, codec)
        if self.bridge_to is not None:
            self.bridge_to.send_linear(pcm)
        self._push_rx(pcm)

    def _push_rx(self, pcm: bytes) -> None:
        self.rx_frames.append(pcm)
        while len(self.rx_frames) > 50:  # cap the backlog at one second
            self.rx_frames.popleft()
        if self.on_frame:
            self.on_frame(pcm)

    def _handle_dtmf(self, ts: int, payload: bytes) -> None:
        if len(payload) < 4:
            return
        event = payload[0]
        if event >= len(DTMF_EVENTS):
            return
        if ts == self._last_dtmf_ts:
            return  # packets of the same event share a timestamp: one digit
        self._last_dtmf_ts = ts  # the first packet of an event is the digit; the rest are repeats
        if self.on_dtmf:
            self.on_dtmf(DTMF_EVENTS[event])

    # ---- send

    def send_linear(self, pcm16_8k: bytes) -> None:
        """Queue 8 kHz linear audio to be sent as 20 ms G.711 frames. A short tail waits for the next chunk."""
        data = self._partial + pcm16_8k
        step = audio.FRAME_SAMPLES * 2
        full = len(data) - len(data) % step
        for i in range(0, full, step):
            self._tx.append(audio.encode(data[i:i + step], self.codec))
        self._partial = data[full:]

    def clear_tx(self) -> int:
        """Drop queued audio (barge-in). Returns how many frames were dropped."""
        n = len(self._tx)
        self._tx.clear()
        self._partial = b""
        return n

    @property
    def tx_backlog_frames(self) -> int:
        return len(self._tx) + (1 if self._partial else 0)

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        step = audio.FRAME_MS / 1000
        next_at = loop.time()
        silence = audio.silence_frame(self.codec)
        while not self._closed:
            next_at += step
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:  # fell far behind (a stalled loop): do not burst, resync
                next_at = loop.time()
            if self.remote is None or self.transport is None:
                continue
            if not self._tx and self._partial:  # flush a short tail once the queue has run dry
                self._tx.append(audio.encode(self._partial + bytes(audio.FRAME_SAMPLES * 2 - len(self._partial)), self.codec))
                self._partial = b""
            if self._tx:
                payload = self._tx.popleft()
                self.tx_active_frames += 1
                marker, self._marker_next = self._marker_next, False
            else:
                payload = silence
                self._marker_next = True
                marker = False
            self._send(self.pt, payload, marker)
            if self.on_tx:
                self.on_tx(payload)
            self.ts += audio.FRAME_SAMPLES

    def _send(self, pt: int, payload: bytes, marker: bool = False, ts: int | None = None) -> None:
        if self.remote is None or self.transport is None or self.transport.is_closing():
            return
        self.transport.sendto(build_packet(pt, self.seq, self.ts if ts is None else ts, self.ssrc, payload, marker), self.remote)
        self.seq = (self.seq + 1) & 0xFFFF
        self.packets_out += 1

    async def send_dtmf(self, digit: str, duration_ms: int = 160, gap_ms: int = 60) -> None:
        """Send one digit as RFC 4733 events: start, updates every 20 ms, then three end packets."""
        if self.dtmf_pt is None:
            raise RuntimeError("peer did not offer telephone-event")
        event = DTMF_EVENTS.index(digit)
        start_ts = self.ts
        steps = max(1, duration_ms // audio.FRAME_MS)
        for i in range(steps):
            self._send(self.dtmf_pt, dtmf_payload(event, False, (i + 1) * audio.FRAME_SAMPLES), marker=(i == 0), ts=start_ts)
            await asyncio.sleep(audio.FRAME_MS / 1000)
        for _ in range(3):
            self._send(self.dtmf_pt, dtmf_payload(event, True, steps * audio.FRAME_SAMPLES), ts=start_ts)
        await asyncio.sleep(gap_ms / 1000)

    def stats(self) -> dict:
        return {"codec": self.codec, "packets_in": self.packets_in, "packets_out": self.packets_out,
                "bytes_in": self.bytes_in, "remote": self.remote}


async def wait_for(predicate: Callable[[], bool], timeout: float, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()
