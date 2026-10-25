"""G.711 codecs and sample-rate conversion for the telephone leg.

A SIP call from the PSTN or a firm's own PBX carries 8 kHz G.711 (PCMU or PCMA).
The speech models want 16 kHz (speech to text) and produce 24 kHz (text to speech),
so every call crosses two resamplers. They are kept here, pure and tested, because a
wrong ratio or a missing low-pass filter is a quiet way to ruin transcription.
"""
from __future__ import annotations

import audioop  # audioop-lts on Python 3.13 and later

import numpy as np
from scipy.signal import resample_poly

PCMU = "PCMU"
PCMA = "PCMA"
STATIC_PAYLOAD_TYPES = {0: PCMU, 8: PCMA}
CODEC_PAYLOAD_TYPES = {PCMU: 0, PCMA: 8}

TELEPHONE_RATE = 8000
STT_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = TELEPHONE_RATE * FRAME_MS // 1000  # 160 samples, 160 bytes of G.711


def encode(pcm16: bytes, codec: str) -> bytes:
    """Linear 16-bit PCM at 8 kHz to G.711."""
    if codec == PCMU:
        return audioop.lin2ulaw(pcm16, 2)
    if codec == PCMA:
        return audioop.lin2alaw(pcm16, 2)
    raise ValueError(f"unsupported codec {codec}")


def decode(payload: bytes, codec: str) -> bytes:
    """G.711 to linear 16-bit PCM at 8 kHz."""
    if codec == PCMU:
        return audioop.ulaw2lin(payload, 2)
    if codec == PCMA:
        return audioop.alaw2lin(payload, 2)
    raise ValueError(f"unsupported codec {codec}")


def silence_frame(codec: str) -> bytes:
    """One 20 ms frame of digital silence in the given codec."""
    return encode(bytes(FRAME_SAMPLES * 2), codec)


def resample(pcm16: bytes, rate_in: int, rate_out: int) -> bytes:
    """Polyphase resample of mono int16 audio. Filters before decimating."""
    if rate_in == rate_out or not pcm16:
        return pcm16
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    g = int(np.gcd(rate_in, rate_out))
    y = resample_poly(x, rate_out // g, rate_in // g)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


def rms(pcm16: bytes) -> float:
    if not pcm16:
        return 0.0
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def add_noise(pcm16: bytes, snr_db: float, seed: int = 7) -> bytes:
    """Add white Gaussian noise so that speech RMS over noise RMS equals snr_db."""
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    speech_rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    if speech_rms == 0.0:
        return pcm16
    noise_rms = speech_rms / (10 ** (snr_db / 20.0))
    noise = np.random.default_rng(seed).normal(0.0, noise_rms, len(x))
    return np.clip(np.rint(x + noise), -32768, 32767).astype(np.int16).tobytes()


def measured_snr_db(clean: bytes, noisy: bytes) -> float:
    """SNR of a noisy copy against its clean source, for the test that checks add_noise."""
    a = np.frombuffer(clean, dtype=np.int16).astype(np.float64)
    b = np.frombuffer(noisy, dtype=np.int16).astype(np.float64)
    n = b - a
    return 20.0 * np.log10(np.sqrt(np.mean(a * a)) / np.sqrt(np.mean(n * n)))


def frames(pcm16_8k: bytes) -> list[bytes]:
    """Split 8 kHz PCM into 20 ms frames, padding the last one with silence."""
    step = FRAME_SAMPLES * 2
    out = []
    for i in range(0, len(pcm16_8k), step):
        chunk = pcm16_8k[i:i + step]
        if len(chunk) < step:
            chunk += bytes(step - len(chunk))
        out.append(chunk)
    return out


class StreamUpsampler:
    """8 kHz to 16 kHz, one 20 ms frame at a time, without the seams a per-frame resample leaves.

    Each call resamples the new frame together with the tail of the earlier ones and keeps only the
    middle part, so the filter always has real samples on both sides. The cost is 2 ms of delay.
    Pipecat's own streaming resampler was tried first and swallowed speech that followed a long
    stretch of digital silence, so the telephone leg does not use it.
    """

    LEFT = 40
    RIGHT = 16

    def __init__(self) -> None:
        self._tail = np.zeros(self.LEFT + self.RIGHT, dtype=np.float32)

    def feed(self, pcm16_8k: bytes) -> bytes:
        x = np.frombuffer(pcm16_8k, dtype=np.int16).astype(np.float32)
        buf = np.concatenate([self._tail, x])
        y = resample_poly(buf, 2, 1)
        start = 2 * (len(self._tail) - self.RIGHT)
        out = y[start:start + 2 * len(x)]
        self._tail = buf[-(self.LEFT + self.RIGHT):]
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16).tobytes()
