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


# ---- the class, enumerated: every public method of every subject against two independent searches for a caller
#
# GROUNDING (docs/DECISIONS.md): "A retention setting per tenant deletes expired calls and audits the deletion." and (README.md) "`tests/test_wiring.py` fails when
# a public method has no caller."
# WHY: an operator sets a firm's retention_days to 30; a method that is tested and promised but called by nothing means every call is kept for ever and the
# promise to the firm is silently false. The list below is read from the live classes, so a method added next month is a new row without anyone typing it.
import itertools
import re

SEARCHES = ("syntax tree", "text")


def live_public_names(cls_name: str) -> list[str]:
    """What a caller can reach on the class right now, read by introspection, never by the same helper the syntax tree search uses."""
    from sipclerk.delivery import Outbox
    from sipclerk.server import ClerkServer
    from sipclerk.store import TenantStore

    cls = {"TenantStore": TenantStore, "Outbox": Outbox, "ClerkServer": ClerkServer}[cls_name]
    return sorted(n for n, v in vars(cls).items() if not n.startswith("_") and (callable(v) or isinstance(v, (property, staticmethod, classmethod))))


def text_uses(method: str, receivers: set[str], defined_in: str, cls: str, src: Path = SRC) -> int:
    """The second search: plain text. A named holder (`store.purge`, `self.store.purge`) anywhere, or `self.` inside the class's own file but outside
    the method's own definition."""
    count = 0
    holders = "|".join(sorted(receivers))
    outside = re.compile(rf"(?<![\w])(?:\w+\.)*(?:_?(?:{holders}))\.{re.escape(method)}\b")
    inside = re.compile(rf"\bself\.{re.escape(method)}\b")
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        count += len(outside.findall(text))
        if f.name == defined_in:
            tree = ast.parse(text)
            owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
            body = ast.get_source_segment(text, owner) or ""
            own = next((m for m in owner.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == method), None)
            if own is not None:
                body = body.replace(ast.get_source_segment(text, own) or "\0", "")
            count += len(inside.findall(body))
    return count


def uses_by(search: str, method: str, cls: str) -> int:
    path, receivers = SUBJECTS[cls]
    return production_uses(method, receivers, path, cls) if search == "syntax tree" else text_uses(method, receivers, path, cls)


def test_the_enumerated_rows_cover_every_public_method_of_every_subject():
    for cls in SUBJECTS:
        path, _ = SUBJECTS[cls]
        assert live_public_names(cls) == sorted(public_methods(path, cls)), cls


# CLASS-TABLE: a public method of the store, outbox or server that nothing in the line calls
def test_class_every_public_method_of_the_store_outbox_and_server_has_a_caller_by_both_searches_or_says_why_not():
    rows = [(cls, method, search) for (cls, method), search in itertools.product([(c, m) for c in sorted(SUBJECTS) for m in live_public_names(c)], SEARCHES)]
    orphans = [(cls, method, search) for cls, method, search in rows if uses_by(search, method, cls) == 0 and (cls, method) not in NOT_RUN_BY_THE_LINE]
    assert not orphans, f"promised but called by nothing in sipclerk/ ({len(orphans)} rows): {orphans}"
    # the rows that must pass: the methods an ordinary call and the hourly pass rely on are found by both searches
    for cls, method in (("TenantStore", "save_call"), ("TenantStore", "purge"), ("Outbox", "deliver_due"), ("Outbox", "purge"), ("ClerkServer", "run_retention_pass")):
        for search in SEARCHES:
            assert uses_by(search, method, cls) >= 1, (cls, method, search)


def test_the_text_search_finds_a_caller_and_a_missing_one_in_a_small_tree(tmp_path):
    """The search itself is not vacuous: in a tree where `purge` is called and `forget` is not, it says so."""
    (tmp_path / "store.py").write_text("class TenantStore:\n    def purge(self):\n        return 1\n\n    def forget(self):\n        return 2\n", encoding="utf-8")
    (tmp_path / "server.py").write_text("def hourly(store):\n    store.purge()\n", encoding="utf-8")
    assert text_uses("purge", {"store"}, "store.py", "TenantStore", tmp_path) == 1
    assert text_uses("forget", {"store"}, "store.py", "TenantStore", tmp_path) == 0
