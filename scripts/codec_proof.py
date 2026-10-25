"""Print the audio path of a call with numbers: G.711 on the wire, 8 kHz linear, 16 kHz for the models, 24 kHz in from the voice.

    python scripts/codec_proof.py            # prints the table
    python scripts/codec_proof.py --out docs/codec_proof.txt

Uses a rendered caller clip (voice_scripts/wav) and a synthetic 24 kHz tone for the voice side, so it needs no model.
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sipclerk import audio  # noqa: E402
from sipclerk.harness import WAV_DIR, read_wav_8k, wav_name  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    pcm = read_wav_8k(WAV_DIR / wav_name("am_adam", "My name is Jordan Reyes."))
    frames = audio.frames(pcm)
    rows = ["clip: 'My name is Jordan Reyes.' (invented caller), %.2f s, %d frames of 20 ms" % (len(pcm) / 16000, len(frames)), ""]
    rows.append(f"{'codec':6} {'payload type':>12} {'bytes per 20 ms packet':>24} {'packets per second':>19} {'kbit per second (payload)':>26} {'round-trip SNR dB':>18}")
    for codec in (audio.PCMU, audio.PCMA):
        enc = [audio.encode(f, codec) for f in frames]
        back = b"".join(audio.decode(e, codec) for e in enc)
        snr = audio.measured_snr_db(b"".join(frames), back)
        rows.append(f"{codec:6} {audio.CODEC_PAYLOAD_TYPES[codec]:>12} {len(enc[0]):>24} {1000 // audio.FRAME_MS:>19} {len(enc[0]) * 8 * 50 / 1000:>26.0f} {snr:>18.1f}")
    up = audio.StreamUpsampler()
    up16 = b"".join(up.feed(f) for f in frames)
    whole = audio.resample(pcm, 8000, 16000)
    lag = 2 * audio.StreamUpsampler.RIGHT
    x = np.frombuffer(up16, dtype=np.int16).astype(np.float64)[lag + 640:]
    y = np.frombuffer(whole, dtype=np.int16).astype(np.float64)[640:len(up16) // 2 - lag]
    n = min(len(x), len(y))
    err = np.sqrt(np.mean((x[:n] - y[:n]) ** 2)) / np.sqrt(np.mean(y[:n] ** 2))
    rows += ["",
             f"caller leg: RTP PCMU 8 kHz -> linear 8 kHz ({audio.FRAME_SAMPLES} samples per frame) -> StreamUpsampler -> 16 kHz ({len(up.feed(frames[0])) // 2} samples per frame) -> Silero VAD and Whisper",
             f"  streaming upsample against a whole-signal resample: relative error {err:.4f}, delay {audio.StreamUpsampler.RIGHT} input samples ({audio.StreamUpsampler.RIGHT / 8:.0f} ms)",
             f"  samples out per sample in: {len(up16) // 2} / {len(pcm) // 2} = {len(up16) / len(pcm):.1f}"]
    tone24 = (6000 * np.sin(2 * np.pi * 300 * np.arange(24000) / 24000)).astype(np.int16).tobytes()
    down = audio.resample(tone24, 24000, 8000)
    rows += [f"agent leg: Kokoro 24 kHz ({len(tone24) // 2} samples for 1 s) -> resample 3:1 -> 8 kHz ({len(down) // 2} samples) -> {len(down) // 2 // audio.FRAME_SAMPLES} frames of 20 ms -> G.711 -> RTP"]
    text = "\n".join(rows)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
