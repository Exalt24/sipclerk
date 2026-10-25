"""Class test: a log line is not the encrypted store. No logging call in the line interpolates what a caller said or what the agent read
back (their name, number, story): it may say how long a line was, never what it said.

Static, so it covers every call including ones no test happens to reach. The pipeline test with stand-in models checks the same thing at run time."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "sipclerk"
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
LAB_ONLY = {"harness.py"}  # the replay rig logs the line its own scripted, invented caller is about to speak; it never runs on a real call
CALLER_WORDS = {"text", "utterance", "transcript", "ack", "summary", "description", "said", "reply", "pending", "readback"}


def risky_log_calls(root: Path = SRC) -> list[str]:
    found = []
    for f in sorted(p for p in root.rglob("*.py") if p.name not in LAB_ONLY):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            fn = call.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in LEVELS and isinstance(fn.value, ast.Name) and fn.value.id == "log"):
                continue
            wrapped = {id(x) for c in ast.walk(call) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "len" for x in ast.walk(c)}
            for arg in call.args[1:]:
                for n in ast.walk(arg):
                    word = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else ""
                    if word in CALLER_WORDS and id(n) not in wrapped:
                        found.append(f"{f.name}:{call.lineno} logs `{word}`")
    return found


def test_no_logging_call_interpolates_what_a_caller_said():
    assert risky_log_calls() == []


def test_the_scan_sees_a_call_that_would_leak_and_lets_a_length_through(tmp_path):
    (tmp_path / "leak.py").write_text('import logging\nlog = logging.getLogger("x")\n\n\ndef f(text, frame):\n    log.info("heard %r", text)\n'
                                      '    log.exception("failed for %r", frame.text[:40])\n    log.info("ok %d", len(text))\n', encoding="utf-8")
    assert risky_log_calls(tmp_path) == ["leak.py:6 logs `text`", "leak.py:7 logs `text`"]
