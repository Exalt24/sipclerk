"""A script that writes a report to a path the caller names must create the folder: the docs send a stranger to `scratch/`, which is ignored by git and so missing in a fresh clone, and the mutation check ran for 45 minutes and then died on the write."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# CLASS-TABLE: every script with an output path argument against the line that makes its folder
WRITERS = {
    "scripts/mutation_check.py": "(ROOT / a.report).parent.mkdir(",
    "scripts/codec_proof.py": "Path(a.out).parent.mkdir(",
    "scripts/sipp_demo.py": "out.parent.mkdir(",
    "scripts/replay.py": "Path(out).parent.mkdir(",
}


def test_class_every_script_with_an_output_path_makes_the_folder():
    bad = []
    for script, needle in WRITERS.items():
        text = (ROOT / script).read_text(encoding="utf-8")
        if needle not in text:
            bad.append(f"{script}: no `{needle}` before the write")
    assert not bad, "\n".join(bad)


def test_the_mutation_check_writes_its_report_into_a_folder_that_does_not_exist(tmp_path):
    report = tmp_path / "not" / "yet" / "report.json"
    p = subprocess.run([sys.executable, "-B", "scripts/mutation_check.py", "--only", "parsers-numeric-date-two-digit-year", "--report", str(report)],
                       cwd=ROOT, capture_output=True, encoding="utf-8", timeout=600)
    assert p.returncode == 0, p.stdout + p.stderr
    assert report.exists()
