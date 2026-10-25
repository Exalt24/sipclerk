"""Mutants whose source edit is DERIVED from the code's own syntax tree, never typed as an old text and a new text.

A hand written `old -> new` pair goes stale the first time someone rewords a line: it then fails to apply, or applies
somewhere else. These rows name a FUNCTION, the KIND of change (the description mutate_ast.py gives its mutation sites)
and a SNIPPET of the code at that site, and the edit is computed at run time with `Operations/tools/mutate_ast.py`
(`sites` and `apply`). A refactor moves the site; it does not break the row. If the snippet no longer exists the row
fails `mutation_check.py --check` instead of silently testing nothing.

The tool lives outside this repository: set SIPCLERK_MUTATE_AST to its path, or keep the default below.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from mutants import FAST, Mutant

DEFAULT_TOOL = Path("C:/Projects/Professional/Operations/tools/mutate_ast.py")


def _tool():
    path = Path(os.environ.get("SIPCLERK_MUTATE_AST", DEFAULT_TOOL))
    if not path.exists():
        raise FileNotFoundError(f"mutate_ast.py not found at {path}: set SIPCLERK_MUTATE_AST")
    spec = importlib.util.spec_from_file_location("mutate_ast", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class DerivedMutant(Mutant):
    function: str = ""
    kind_re: str = ""   # matches the description of one mutation site, e.g. "delete the call statement" or "negate the condition"
    snippet: str = ""   # a piece of the code at that site (the test of an `if`, the whole of a call statement)
    nth: int = 0        # which of several matching sites, when the snippet occurs more than once


def _site_text(node: ast.AST) -> str:
    if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
        return ast.unparse(node.test)
    return ast.unparse(node)


def derive(m: DerivedMutant, text: str) -> str:
    """The whole source file with exactly one mutation applied at the site `m` names."""
    tool = _tool()
    tree = ast.parse(text)
    nodes = list(ast.walk(tree))
    hits = []
    for index, _line, desc in tool.sites(tree, text, include_output=True, only_functions=[m.function]):
        if re.search(m.kind_re, desc) and m.snippet in _site_text(nodes[index // tool.SUB]):
            hits.append(index)
    if len(hits) <= m.nth:
        raise ValueError(f"{m.id}: no `{m.kind_re}` site containing {m.snippet!r} (nth {m.nth}) in {m.function}; found {len(hits)}")
    return ast.unparse(tool.apply(tree, hits[m.nth]))


def D(id: str, file: str, function: str, kind_re: str, snippet: str, desc: str, tests: list[str] | None = None, nth: int = 0) -> DerivedMutant:
    return DerivedMutant(id, file, function, kind_re, list(tests) if tests is not None else list(FAST), desc, "ast", "unit",
                         function=function, kind_re=kind_re, snippet=snippet, nth=nth)


# Fields before `function` are the Mutant ones: id, file, old, new, tests, desc, kind, layer.
STORE = ["tests/test_store.py"]
RETENTION = ["tests/test_retention.py", "tests/test_store.py", "tests/test_wiring.py"]
OUTBOX = ["tests/test_outbox_at_rest.py", "tests/test_delivery.py"]
LEG = ["tests/test_transfer_leg.py", "tests/test_sip_stack.py"]
WINDOW = ["tests/test_delivery_window.py", "tests/test_delivery.py"]
ST, SV, DE = "sipclerk/store.py", "sipclerk/server.py", "sipclerk/delivery.py"
CA, SK = "sipclerk/call.py", "sipclerk/sip/stack.py"

DERIVED_MUTANTS: list[DerivedMutant] = [
    # ---- retention: the purge itself (these two replace the typed rows store-retention and store-purge-audit)
    D("store-retention", ST, "purge", "negate the condition", "saved >= cutoff", "a call inside its retention window is deleted too", STORE),
    D("store-purge-audit", ST, "purge", "delete the call statement", "'purge'", "a purge leaves no audit line", STORE),
    # ---- one bad entry must not stop or hide in a pass over all entries
    D("store-purge-unreadable-counted", ST, "purge", "delete the call statement", "'unreadable meta", "a folder the purge cannot read is not counted", STORE),
    D("store-purge-delete-failure-counted", ST, "purge", "delete the call statement", "'delete failed", "a folder the purge cannot delete is not counted", STORE),
    D("store-purge-list-failure-counted", ST, "purge", "delete the call statement", "'list failed", "a firm whose folder cannot be listed is not counted", STORE),
    # ---- the line applies retention (F1)
    D("server-retention-at-start", SV, "start", "delete the call statement", "run_retention_pass", "retention is not applied when the line starts", RETENTION),
    D("server-retention-loop", SV, "_retention_loop", "delete the call statement", "run_retention_pass", "retention is applied once at start and never again", RETENTION),
    D("server-retention-failure-counted", SV, "run_retention_pass", "Add= -> Sub=", "'failures'", "a failed retention pass is not counted", RETENTION),
    D("server-retention-failure-logged", SV, "run_retention_pass", "delete the call statement", "log.exception", "a failed pass is not logged", RETENTION),
    # ---- the outbox is the second place caller data rests (F2)
    D("outbox-body-never-written", DE, "to_line", "delete the call statement", "pop('payload')", "the readable body of an entry is written to the file", OUTBOX),
    D("outbox-needs-a-firm", DE, "enqueue", "negate the condition", "not tenant_id", "an entry can be queued with no firm to encrypt it under", OUTBOX),
    D("outbox-body-bound-to-its-call", DE, "_aad", "return value -> None", "", "an encrypted body can be moved to another entry", OUTBOX),
    D("outbox-purge-window", DE, "purge", "LtE?|Lt -> GtE|Gt", "finished_at <", "a dead or delivered entry is purged inside its firm's retention", OUTBOX),
    D("outbox-purge-spares-waiting", DE, "purge", "negate the condition", "e.status == 'pending'", "a waiting entry is purged", OUTBOX),
    D("outbox-unopenable-dies", DE, "deliver_due", "delete the call statement", "cannot be opened", "an entry whose body cannot be opened waits for ever", OUTBOX),
    D("outbox-bad-line-counted", DE, "_load", "Add= -> Sub=", "unreadable_lines", "a line the loader skipped is not counted", OUTBOX),
    D("delivery-4xx-retried", DE, "deliver_due", "delete the call statement", "self._die(e)", "a permanent refusal is retried forever (the first _die(e) in walk order is the 4xx branch)", OUTBOX),
    # ---- a delivery is not given up inside a short window (F3)
    D("delivery-backoff-index", DE, "deliver_due", "Sub -> Add", "e.attempts - 1", "the wait after an attempt is read from the wrong step of the schedule", WINDOW),
    D("delivery-window-off-by-one", DE, "deliver_due", "GtE -> Lt", "e.attempts >= MAX_ATTEMPTS", "an entry is dead after its first attempt, or never", WINDOW),
    D("delivery-dead-is-warned", DE, "_die", "delete the call statement", "log.warning", "an entry that goes dead is not logged", WINDOW),
    D("delivery-requeue-selective", DE, "requeue", "negate the condition", "key is not None", "requeue ignores the key it was given", WINDOW),
    D("delivery-requeue-history", DE, "requeue", "delete the call statement", "history.append", "a requeued entry does not say it was requeued", WINDOW),
    D("delivery-requeue-request-consumed", DE, "_apply_requeue_request", "delete the call statement", "marker.unlink", "a requeue request is applied on every pass for ever", WINDOW),
    D("server-dead-reminder", SV, "run_retention_pass", "negate the condition", "self.outbox.dead", "the hourly pass says nothing about dead deliveries", WINDOW),
    D("script-requeue-now-or-ask", "scripts/requeue_dead.py", "main", "negate the condition", "a.now", "the script edits a running line's file instead of asking the line", WINDOW),
    # ---- a transfer leg never outlives the call that started it (F4)
    D("call-end-transfer-cancels", CA, "_end_transfer", "delete the call statement", "task.cancel()", "ending the call leaves the desk leg running", LEG),
    D("call-drop-leg-hangs-up-the-desk", CA, "_drop_leg", "negate the condition", "d_staff.terminated.is_set()", "an answered desk call is not hung up when the call ends", LEG),
    D("call-caller-bye-ends-an-unbridged-call", CA, "_watch_caller_bye", "Is -> IsNot", "self.bridged_at is None", "a caller's BYE before the bridge does not end the call", LEG),
    D("stack-cancel-is-sent", SK, "_cancel", "delete the call statement", "CANCEL", "an INVITE nobody wants any more is never cancelled", LEG),
    D("stack-cancel-crossing-2xx", SK, "_cancel", "negate the condition", "make_dialog is not None", "an answer that crosses the CANCEL is left up", LEG),
    D("stack-invite-task-ends-with-the-dialog", SK, "run", "and <-> or", "d.terminated", "the 2xx retransmit task outlives its dialog", LEG),
]
