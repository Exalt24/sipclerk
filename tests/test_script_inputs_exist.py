"""A documented command must not crash on a file that is not in the repository: `scripts/codec_proof.py` named a clip the scenarios stopped rendering when their callers were renamed, and the first check of the audio claim ended in a traceback. And an `--only` list with an id that is not in the pool must fail, not print `0 of 0 mutants killed` and exit 0."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from sipclerk.harness import WAV_DIR, wav_name  # noqa: E402
import codec_proof  # noqa: E402
from mutants_pipeline import PIPELINE_MUTANTS  # noqa: E402

# CLASS-TABLE: every voice and every clip text a script names, against the files in voice_scripts/wav
CLIPS = {"scripts/codec_proof.py": [("am_adam", codec_proof.CLIP)]}


def test_class_every_clip_a_script_names_is_in_the_repository():
    missing = []
    for script, clips in CLIPS.items():
        for voice, text in clips:
            if not (WAV_DIR / wav_name(voice, text)).exists():
                missing.append(f"{script}: {voice} {text!r}")
    assert not missing, missing


def test_the_codec_proof_runs_on_a_fresh_checkout():
    p = subprocess.run([sys.executable, "-B", "scripts/codec_proof.py"], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "SNR" in p.stdout or "round-trip" in p.stdout


@pytest.mark.parametrize("only", ["not-a-real-id", "guard-emergency,not-a-real-id", "pipe-not-in-the-unit-layer"])
def test_an_unknown_mutant_id_is_an_error_not_a_pass(only):
    p = subprocess.run([sys.executable, "-B", "scripts/mutation_check.py", "--check", "--only", only], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "no such mutant id" in p.stdout


def test_a_row_of_another_layer_is_an_error_not_a_pass():
    p = subprocess.run([sys.executable, "-B", "scripts/mutation_check.py", "--check", "--only", PIPELINE_MUTANTS[0].id], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    assert p.returncode == 2 and "add --layer all" in p.stdout, p.stdout + p.stderr
