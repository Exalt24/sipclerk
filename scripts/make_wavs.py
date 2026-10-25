"""Render every caller line in voice_scripts/scenarios.py to an 8 kHz WAV with the local Kokoro model.

    python scripts/make_wavs.py            # only the clips that are missing
    python scripts/make_wavs.py --force    # all of them

The caller voices are not the agent's voices. The clips are what the regression suite replays over RTP.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from sipclerk import audio  # noqa: E402
from sipclerk.harness import WAV_DIR, wav_name, write_wav  # noqa: E402
from sipclerk.voice.models import KOKORO_ONNX, KOKORO_VOICES  # noqa: E402
from voice_scripts.scenarios import all_texts  # noqa: E402


async def main(force: bool) -> int:
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(KOKORO_ONNX), str(KOKORO_VOICES))
    made = 0
    for voice, text in all_texts():
        path = WAV_DIR / wav_name(voice, text)
        if path.exists() and not force:
            continue
        chunks = []
        async for samples, rate in kokoro.create_stream(text, voice=voice, speed=1.0, lang="en-us"):
            chunks.append(audio.resample((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes(), rate, audio.TELEPHONE_RATE))
        write_wav(path, b"".join(chunks))
        made += 1
    wanted = {wav_name(v, t) for v, t in all_texts()}
    for old in WAV_DIR.glob("*.wav"):
        if old.name not in wanted:
            old.unlink()  # a clip no scenario speaks any more
    print(f"{made} clip(s) written, {len(all_texts())} lines in the scripts, folder {WAV_DIR}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().force)))
