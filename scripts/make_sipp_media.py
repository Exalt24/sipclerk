"""Build the pcap SIPp plays as the caller: PCMU speech and RFC 4733 keypad events at the offsets of a reference run.

    python scripts/replay.py sipp_reference --json scratch/ref.json
    python scripts/make_sipp_media.py scratch/ref.json tools/sipp/media/call.pcap

The reference run is the reactive softphone call (it waits for the agent before each reply). SIPp cannot react, so it
replays the same clips at the same offsets, plus a small margin, and the agent's prompts are cached so their timing repeats.
"""
import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sipclerk import audio  # noqa: E402
from sipclerk.harness import WAV_DIR, read_wav_8k, wav_name  # noqa: E402
from sipclerk.sip.rtp import DTMF_EVENTS, build_packet, dtmf_payload  # noqa: E402

PCAP_MAGIC = 0xA1B2C3D4
MARGIN_S = 0.5
TAIL_S = 30.0
DTMF_PT = 101
SSRC = 0x5199A001


def ip_checksum(header: bytes) -> int:
    s = sum(struct.unpack("!10H", header))
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def frame(payload: bytes, sport: int = 6000, dport: int = 6000) -> bytes:
    udp_len = 8 + len(payload)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + udp_len, 0, 0, 64, 17, 0, bytes([10, 0, 0, 1]), bytes([10, 0, 0, 2]))
    ip = ip[:10] + struct.pack("!H", ip_checksum(ip)) + ip[12:]
    udp = struct.pack("!HHHH", sport, dport, udp_len, 0)
    eth = bytes.fromhex("020000000002020000000001") + b"\x08\x00"
    return eth + ip + udp + payload


def build(timeline: list, out: Path) -> dict:
    events = []  # (start_s, kind, data)
    end = 0.0
    for kind, t, voice, text in timeline:
        start = t + MARGIN_S
        if kind == "say":
            pcm = read_wav_8k(WAV_DIR / wav_name(voice, text))
            events.append((start, "audio", pcm))
            end = max(end, start + len(pcm) / 16000)
        elif kind == "dtmf":
            events.append((start, "dtmf", text))
            end = max(end, start + len(text) * 0.22)
    total = end + TAIL_S
    steps = int(total / 0.02)
    audio_frames: dict[int, bytes] = {}
    dtmf_at: dict[int, str] = {}
    for start, kind, data in events:
        i0 = int(start / 0.02)
        if kind == "audio":
            for k, fr in enumerate(audio.frames(data)):
                audio_frames[i0 + k] = audio.encode(fr, audio.PCMU)
        else:
            for n, d in enumerate(data):
                dtmf_at[i0 + n * 11] = d  # 220 ms per digit
    silent = audio.silence_frame(audio.PCMU)
    packets = []
    seq, ts = 1000, 160000
    i = 0
    dtmf_busy_until = -1
    while i < steps:
        t_s = i * 0.02
        if i in dtmf_at:
            event = DTMF_EVENTS.index(dtmf_at[i])
            start_ts = ts
            for n in range(8):  # 160 ms of start/continue packets
                packets.append((t_s + n * 0.02, build_packet(DTMF_PT, seq, start_ts, SSRC, dtmf_payload(event, False, (n + 1) * 160), marker=(n == 0))))
                seq += 1
            for _ in range(3):
                packets.append((t_s + 8 * 0.02, build_packet(DTMF_PT, seq, start_ts, SSRC, dtmf_payload(event, True, 8 * 160))))
                seq += 1
            dtmf_busy_until = i + 11
        if i >= dtmf_busy_until:
            packets.append((t_s, build_packet(0, seq, ts, SSRC, audio_frames.get(i, silent))))
            seq += 1
        ts += 160
        i += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        fh.write(struct.pack("<IHHiIII", PCAP_MAGIC, 2, 4, 0, 0, 65535, 1))
        for t_s, payload in packets:
            f = frame(payload)
            fh.write(struct.pack("<IIII", int(t_s), int((t_s % 1) * 1e6), len(f), len(f)))
            fh.write(f)
    return {"seconds": round(total, 1), "packets": len(packets), "dtmf_digits": sum(len(t[3]) for t in timeline if t[0] == "dtmf"),
            "clips": sum(1 for t in timeline if t[0] == "say")}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("reference_json")
    ap.add_argument("out_pcap")
    a = ap.parse_args()
    rows = json.loads(Path(a.reference_json).read_text(encoding="utf-8"))
    print(build(rows[0]["timeline"], Path(a.out_pcap)))
