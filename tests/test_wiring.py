"""Class test: a behaviour the README or DECISIONS.md promises must be run by the line, not only exist as a tested method.

For the store, the outbox and the server, every public method has a call from production code (sipclerk/ outside its own
definition), or is listed here with the reason it is an interface for something outside this lab."""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "sipclerk"

# class -> (file, names a caller holds it under)
SUBJECTS = {
    "TenantStore": ("store.py", {"store"}),
    "Outbox": ("delivery.py", {"outbox"}),
    "ClerkServer": ("server.py", {"server"}),
}
NOT_RUN_BY_THE_LINE = {
    ("TenantStore", "read"): "the staff read interface: the line writes calls and never reads one back, and this lab has no staff screen",
    ("Outbox", "pending"): "the undelivered entries, until the server's status view reads them",
    ("Outbox", "dead"): "the dead letters, until the server's status view reads them",
    ("Outbox", "stats"): "the delivery counts, until the server's status view reads them",
}


def public_methods(path: str, cls: str) -> list[str]:
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_")]


def production_uses(method: str, receivers: set[str], defined_in: str, cls: str) -> int:
    count = 0
    for f in SRC.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        skip: set[int] = set()
        if f.name == defined_in:
            for c in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls):
                for m in c.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == method:
                        skip |= {id(x) for x in ast.walk(m)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == method and id(n) not in skip:
                holder = ast.unparse(n.value).split(".")[-1]
                if holder in receivers or (holder == "self" and f.name == defined_in):
                    count += 1
    return count


@pytest.mark.parametrize("cls", sorted(SUBJECTS))
def test_every_public_method_is_run_by_the_line_or_says_why_not(cls):
    path, receivers = SUBJECTS[cls]
    orphans = [m for m in public_methods(path, cls) if production_uses(m, receivers, path, cls) == 0 and (cls, m) not in NOT_RUN_BY_THE_LINE]
    assert not orphans, f"{cls}: promised but nothing in sipclerk/ calls {orphans}; run it from the line or list it with a reason"


def test_the_allowlist_has_no_stale_entries():
    for (cls, method), why in NOT_RUN_BY_THE_LINE.items():
        path, _ = SUBJECTS[cls]
        assert method in public_methods(path, cls) and len(why) > 20, (cls, method)


def test_the_scanner_would_have_found_the_missing_purge_call():
    """The check itself: before the retention loop existed, purge had no caller and this scan returned zero."""
    assert production_uses("purge", {"store"}, "store.py", "TenantStore") >= 1
    assert production_uses("no_such_method_anywhere", {"store"}, "store.py", "TenantStore") == 0
