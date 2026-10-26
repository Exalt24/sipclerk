"""Every mutant must still apply to the code it breaks: a typed anchor that stops matching (or matches twice) after a code change makes `scripts/mutation_check.py` exit before it runs anything, and the documented command then fails for a reader. The last parse_date change did exactly that to two date rows and nothing but a manual run noticed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mutation_check  # noqa: E402
from mutants import MUTANTS as UNIT_BASE  # noqa: E402
from mutants_derived import DERIVED_MUTANTS, PIPELINE_DERIVED  # noqa: E402
from mutants_found import FOUND_MUTANTS  # noqa: E402
from mutants_pipeline import PIPELINE_MUTANTS  # noqa: E402

# CLASS-TABLE: every mutant of every layer against the current text of the file it breaks
LAYERS = {
    "unit": [*UNIT_BASE, *FOUND_MUTANTS, *DERIVED_MUTANTS],
    "pipeline": [*PIPELINE_MUTANTS, *PIPELINE_DERIVED],
}


def test_class_every_mutant_still_applies_exactly_once():
    bad = []
    for layer, pool in LAYERS.items():
        for m in pool:
            try:
                mutation_check.apply(m, (ROOT / m.file).read_text(encoding="utf-8"))
            except (ValueError, FileNotFoundError) as e:
                bad.append(f"{layer}: {e}")
    assert not bad, "\n".join(bad)
    assert sum(len(p) for p in LAYERS.values()) >= 234
