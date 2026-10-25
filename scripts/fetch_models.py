"""Download the local models: Kokoro (Apache-2.0) and faster-whisper small.en (MIT) into ./models.

    python scripts/fetch_models.py

Silero VAD ships inside the pipecat-ai package. The language model is whatever Ollama already has
(`SIPCLERK_LLM`, default qwen3.5:4b); pull it with `ollama pull qwen3.5:4b`.
"""
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sipclerk.voice.models import KOKORO_ONNX, KOKORO_VOICES, MODEL_DIR, WHISPER_MODEL  # noqa: E402

BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def main() -> int:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for path in (KOKORO_ONNX, KOKORO_VOICES):
        if path.exists():
            print("have", path.name)
            continue
        print("fetching", path.name)
        urllib.request.urlretrieve(BASE + path.name, path)
    from faster_whisper import WhisperModel

    WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(MODEL_DIR / "whisper"))
    print("whisper", WHISPER_MODEL, "ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
