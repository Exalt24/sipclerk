"""The local models, loaded once and shared by every call.

Speech to text is faster-whisper, speech out is Kokoro (kokoro-onnx), and the voice activity detector is
Silero (shipped inside Pipecat). All of them run on this machine; no audio leaves it. A call never
loads a model, it borrows these.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sipclerk import audio

log = logging.getLogger("sipclerk.models")
ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = Path(os.environ.get("SIPCLERK_MODELS", ROOT / "models"))
WHISPER_MODEL = os.environ.get("SIPCLERK_WHISPER", "small.en")
KOKORO_ONNX = MODEL_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODEL_DIR / "voices-v1.0.bin"
SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


def quiet_pipecat_logs(level: str = "WARNING") -> None:
    """Pipecat logs every frame at DEBUG through loguru; keep warnings and errors only."""
    import sys

    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=level)


def split_sentences(text: str) -> list[str]:
    return [p for p in SENTENCE_END.split(text.strip()) if p]


@dataclass
class Transcript:
    text: str
    no_speech_prob: float
    seconds: float  # audio length transcribed
    took: float  # wall time


@dataclass
class Models:
    whisper: object
    kokoro: object
    whisper_device: str
    tts_cache: OrderedDict = field(default_factory=OrderedDict)
    cache_hits: int = 0
    cache_misses: int = 0

    # ---- speech to text

    def _transcribe_sync(self, pcm16_16k: bytes) -> Transcript:
        t0 = time.perf_counter()
        x = np.frombuffer(pcm16_16k, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.whisper.transcribe(x, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False,
                                                  temperature=0.0)
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        nsp = max((s.no_speech_prob for s in segs), default=1.0)
        return Transcript(text, float(nsp), len(x) / 16000, time.perf_counter() - t0)

    async def transcribe(self, pcm16_16k: bytes) -> Transcript:
        return await asyncio.get_running_loop().run_in_executor(None, self._transcribe_sync, pcm16_16k)

    # ---- text to speech

    async def _sentence(self, sentence: str, voice: str):
        """Yield (8 kHz chunk, from_cache) for one sentence, caching the whole sentence once it is complete."""
        key = (voice, sentence)
        if key in self.tts_cache:
            self.tts_cache.move_to_end(key)
            self.cache_hits += 1
            yield self.tts_cache[key], True
            return
        self.cache_misses += 1
        parts = []
        async for samples, rate in self.kokoro.create_stream(sentence, voice=voice, speed=1.0, lang="en-us"):
            pcm24 = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
            chunk = audio.resample(pcm24, rate, audio.TELEPHONE_RATE)
            parts.append(chunk)
            yield chunk, False
        self.tts_cache[key] = b"".join(parts)
        while len(self.tts_cache) > 600:
            self.tts_cache.popitem(last=False)

    async def stream_synthesis(self, text: str, voice: str):
        """Yield 8 kHz PCM chunks sentence by sentence, so the first words go out before the rest is rendered.

        Fixed sentences ("Is that right?", every prompt) are cached once and reused inside any longer line.
        """
        for sentence in split_sentences(text):
            async for chunk, cached in self._sentence(sentence, voice):
                yield chunk, cached

    async def synthesize(self, text: str, voice: str) -> tuple[bytes, bool]:
        """The whole line as 8 kHz linear PCM, and whether every sentence came from the cache."""
        chunks, all_cached = [], True
        async for chunk, cached in self.stream_synthesis(text, voice):
            chunks.append(chunk)
            all_cached = all_cached and cached
        return b"".join(chunks), all_cached


def load(device: str | None = None) -> Models:
    """Load Whisper and Kokoro. Whisper goes on the GPU when there is one and falls back to int8 on the CPU."""
    from faster_whisper import WhisperModel
    from kokoro_onnx import Kokoro

    if not KOKORO_ONNX.exists():
        raise FileNotFoundError(f"{KOKORO_ONNX} is missing: run `python scripts/fetch_models.py`")
    dev = device or os.environ.get("SIPCLERK_DEVICE", "cuda")
    t0 = time.perf_counter()
    try:
        whisper = WhisperModel(WHISPER_MODEL, device=dev, compute_type="float16" if dev == "cuda" else "int8",
                               download_root=str(MODEL_DIR / "whisper"))
    except Exception as e:  # no CUDA runtime: say so and carry on with the CPU
        log.warning("whisper on %s failed (%s), using cpu int8", dev, e)
        dev = "cpu"
        whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(MODEL_DIR / "whisper"))
    kokoro = Kokoro(str(KOKORO_ONNX), str(KOKORO_VOICES))
    log.info("models loaded in %.1fs (whisper %s on %s)", time.perf_counter() - t0, WHISPER_MODEL, dev)
    return Models(whisper, kokoro, dev)


def fixed_lines(registry) -> list[tuple[str, str]]:
    """Every (voice, line) a call can say without filling in a name, a number or a date."""
    from sipclerk.intake.machine import FIXED_LINES

    out: list[tuple[str, str]] = []
    for t in registry.all:
        c = t.config
        texts = [c.greeting, c.after_hours_message, c.closing, c.transfer.announce, c.transfer.unavailable, c.guard.out_of_scope_message,
                 *(d.text for d in c.disclosures), *(q.prompt for q in c.questions), *(q.reprompt for q in c.questions if q.reprompt),
                 *FIXED_LINES]
        out += [(c.voice, x) for x in texts if x]
    return out


async def warm(models: Models, lines: list[tuple[str, str]]) -> dict:
    """Run one transcription and pre-render the fixed lines so the first caller does not pay the cold start."""
    t0 = time.perf_counter()
    await models.transcribe(bytes(32000))
    t_stt = time.perf_counter() - t0
    t1 = time.perf_counter()
    for voice, text in lines:
        async for _ in models.stream_synthesis(text, voice):
            pass
    return {"stt_warm_s": round(t_stt, 2), "tts_prerender_s": round(time.perf_counter() - t1, 2), "lines": len(lines),
            "sentences_cached": len(models.tts_cache)}
