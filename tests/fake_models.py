"""Stand-ins for Whisper and Kokoro so the pipeline logic can be tested quickly and on any machine.

The voice activity detector, the RTP, the SIP and the state machine are all real. Only the two heavy models are
replaced: speech to text returns the next scripted line for each utterance it is handed, and text to speech
produces a tone as long as the words would take to say.
"""
from __future__ import annotations

import numpy as np

from sipclerk import audio
from sipclerk.voice.models import Transcript, split_sentences

SECONDS_PER_WORD = 0.15


def speech_tone(text: str, rate: int = audio.TELEPHONE_RATE) -> bytes:
    secs = max(0.25, SECONDS_PER_WORD * len(text.split()))
    t = np.arange(int(rate * secs)) / rate
    return (3500 * np.sin(2 * np.pi * 330 * t)).astype(np.int16).tobytes()


class FakeModels:
    def __init__(self, script: list[str] | None = None):
        self.script = list(script or [])
        self.heard: list[float] = []
        self.said: list[str] = []
        self.tts_cache: dict = {}

    async def transcribe(self, pcm16_16k: bytes) -> Transcript:
        self.heard.append(len(pcm16_16k) / 32000)
        text = self.script.pop(0) if self.script else ""
        return Transcript(text, 0.0 if text else 1.0, len(pcm16_16k) / 32000, 0.0)

    async def stream_synthesis(self, text: str, voice: str):
        for s in split_sentences(text):
            self.said.append(s)
            yield speech_tone(s), True

    async def synthesize(self, text: str, voice: str):
        chunks = [c async for c, _ in self.stream_synthesis(text, voice)]
        return b"".join(chunks), True
