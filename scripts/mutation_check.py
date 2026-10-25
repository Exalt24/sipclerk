"""Break one guard, rule or branch at a time and require a real test failure for each.

    python scripts/mutation_check.py --check              # every mutant's text still appears exactly once
    python scripts/mutation_check.py                      # the unit layer (fast, no audio)
    python scripts/mutation_check.py --layer pipeline     # the line with real SIP, RTP and VAD, stand-in speech models
    python scripts/mutation_check.py --layer all --report docs/mutation_report.json
    python scripts/mutation_check.py --only guard-emergency
    python scripts/mutation_check.py --in-place           # mutate this working tree instead of a throwaway copy of it

By default the run happens in a COPY of the repository in a temporary folder, so the working tree is never edited. Rows
in mutants_derived.py get their edit from Operations/tools/mutate_ast.py (the code's own syntax tree), not from typed text.

A mutant counts as killed only when pytest ran and reported at least one failed test. A crash or a collection
error is reported as ERROR and does not count. The source file is always restored, even on Ctrl-C.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from mutants import MUTANTS as UNIT_BASE, Mutant  # noqa: E402
from mutants_derived import DERIVED_MUTANTS, derive  # noqa: E402
from mutants_found import FOUND_MUTANTS  # noqa: E402
from mutants_pipeline import PIPELINE_MUTANTS  # noqa: E402

MUTANTS = [*UNIT_BASE, *FOUND_MUTANTS, *DERIVED_MUTANTS]


def apply(m: Mutant, text: str) -> str:
    if m.kind == "ast":
        return derive(m, text)
    if m.kind == "empty_list":
        pattern = re.compile(rf"^{m.old} = \[.*?^\]\n", re.S | re.M)
        if len(pattern.findall(text)) != 1:
            raise ValueError(f"{m.id}: list {m.old} not found exactly once")
        return pattern.sub(f"{m.old} = []\n", text, count=1)
    if text.count(m.old) != 1:
        raise ValueError(f"{m.id}: expected the text exactly once, found {text.count(m.old)}")
    return text.replace(m.old, m.new, 1)


def run_pytest(args: list[str], timeout: int) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="mut-pyc-"), PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", *args, "-x", "-q", "--tb=no", "-rf", "-p", "no:cacheprovider", "-m", "not voice"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")


def killed_by(output: str) -> list[str]:
    return re.findall(r"^FAILED (\S+)", output, re.M)


HEAVY = {".git", "models", "scratch", "demo", "__pycache__", ".pytest_cache", ".secrets", "tools", "docs"}


def run_in_copy(argv: list[str]) -> int:
    """Copy the repository to a temporary folder and run this script there with --in-place."""
    tmp = Path(tempfile.mkdtemp(prefix="sipclerk-mut-"))
    try:
        shutil.copytree(ROOT, tmp / "repo", ignore=lambda d, names: [n for n in names if n in HEAVY or n.endswith((".mp4", ".gif"))])
        args = list(argv)
        if "--report" in args:  # the report belongs to the real repository, not the copy
            i = args.index("--report")
            args[i + 1] = str((ROOT / args[i + 1]).resolve())
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(tmp / "repo" / "scripts" / "mutation_check.py"), "--in-place", *args], cwd=tmp / "repo", env=env).returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    argv = sys.argv[1:]
    if "--in-place" not in argv and "--check" not in argv:
        return run_in_copy(argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--layer", choices=["unit", "pipeline", "all"], default="unit")
    ap.add_argument("--only")
    ap.add_argument("--report")
    a = ap.parse_args()
    pool = [m for m in MUTANTS if m.layer == "unit"] if a.layer == "unit" else [m for m in PIPELINE_MUTANTS] if a.layer == "pipeline" else [*MUTANTS, *PIPELINE_MUTANTS]
    if a.only:
        pool = [m for m in pool if m.id in a.only.split(",")]
    # every mutant must still apply
    bad = []
    for m in pool:
        try:
            apply(m, (ROOT / m.file).read_text(encoding="utf-8"))
        except (ValueError, FileNotFoundError) as e:
            bad.append(str(e))
    if bad:
        print("\n".join(bad))
        return 2
    print(f"{len(pool)} mutants apply cleanly")
    if a.check:
        return 0
    # the baseline must be green, or a red run proves nothing
    results = []
    survivors = []
    baselines: dict[tuple, bool] = {}
    t_start = time.time()
    for m in pool:
        key = tuple(m.tests)
        if key not in baselines:
            code, out = run_pytest(list(m.tests), 900)
            baselines[key] = code == 0
            print(f"baseline for {m.layer} tests: {'green' if code == 0 else 'RED'}")
            if code != 0:
                print(out[-1500:])
                return 3
        path = ROOT / m.file
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(apply(m, original), encoding="utf-8", newline="")
            t0 = time.time()
            code, out = run_pytest(list(m.tests), 600 if m.layer == "pipeline" else 300)
        finally:
            path.write_text(original, encoding="utf-8", newline="")
        names = killed_by(out)
        status = "KILLED" if code == 1 and names else "SURVIVED" if code == 0 else f"ERROR({code})"
        results.append({"id": m.id, "file": m.file, "desc": m.desc, "layer": m.layer, "status": status, "killed_by": names[:2],
                        "seconds": round(time.time() - t0, 1)})
        if status != "KILLED":
            survivors.append(m.id)
        print(f"{status:9} {m.id:34} {names[0] if names else ''}"[:170])
    killed = sum(1 for r in results if r["status"] == "KILLED")
    print(f"\n{killed} of {len(results)} mutants killed in {time.time() - t_start:.0f}s; not killed: {survivors or 'none'}")
    if a.report:
        Path(ROOT / a.report).write_text(json.dumps({"killed": killed, "total": len(results), "results": results}, indent=2), encoding="utf-8")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
