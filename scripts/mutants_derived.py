"""Mutants whose source edit is DERIVED from the code's own syntax tree, never typed as an old text and a new text.

A hand written `old -> new` pair goes stale the first time someone rewords a line: it then fails to apply, or applies
somewhere else. These rows name a FUNCTION, the KIND of change (the description mutate_ast.py gives its mutation sites)
and a SNIPPET of the code at that site, and the edit is computed at run time with `scripts/mutate_ast.py`
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

from mutants import FAST, PIPE_FAST, Mutant

DEFAULT_TOOL = Path(__file__).resolve().parent / "mutate_ast.py"


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


def D(id: str, file: str, function: str, kind_re: str, snippet: str, desc: str, tests: list[str] | None = None, nth: int = 0, layer: str = "unit") -> DerivedMutant:
    return DerivedMutant(id, file, function, kind_re, list(tests) if tests is not None else list(FAST), desc, "ast", layer,
                         function=function, kind_re=kind_re, snippet=snippet, nth=nth)


def PD(id: str, file: str, function: str, kind_re: str, snippet: str, desc: str, k: str) -> DerivedMutant:
    """A derived mutant checked through the whole line (real SIP, RTP and voice activity detection, stand-in speech models)."""
    return D(id, file, function, kind_re, snippet, desc, [*PIPE_FAST, "tests/test_processors.py", "-k", k], layer="pipeline")


# Fields before `function` are the Mutant ones: id, file, old, new, tests, desc, kind, layer.
STORE = ["tests/test_store.py"]
RETENTION = ["tests/test_retention.py", "tests/test_store.py", "tests/test_wiring.py"]
OUTBOX = ["tests/test_outbox_at_rest.py", "tests/test_delivery.py"]
REPORT = ["tests/test_emergency_report.py", "tests/test_emergency_tense.py", "tests/test_guard.py"]
SCOPE = ["tests/test_scope_confirmed_area.py", "tests/test_machine.py", "tests/test_guard.py"]
STOPWORDS = ["tests/test_names_stopwords.py", "tests/test_names_unicode.py", "tests/test_parsers.py"]
CALLERS = ["tests/test_caller_number.py", "tests/test_anonymous_callers.py", "tests/test_guard.py", "tests/test_sip_stack.py"]
RECEIVERS = ["tests/test_outcome_receivers.py", "tests/test_delivery.py", "tests/test_wiring.py", "tests/test_bridge_contract.py"]
ACKWIN = ["tests/test_ack_window.py"]
TENSE = ["tests/test_emergency_tense.py", "tests/test_guard.py", "tests/test_machine.py"]
ANON = ["tests/test_anonymous_callers.py"]
ROUTE = ["tests/test_route_set.py", "tests/test_sip_stack.py"]
DIAL = ["tests/test_dialled_number.py", "tests/test_sip_stack.py", "tests/test_tenants.py"]
NAMES = ["tests/test_names_unicode.py", "tests/test_parsers.py", "tests/test_machine.py"]
LEG = ["tests/test_transfer_leg.py", "tests/test_sip_stack.py"]
WINDOW = ["tests/test_delivery_window.py", "tests/test_delivery.py"]
ST, SV, DE = "sipclerk/store.py", "sipclerk/server.py", "sipclerk/delivery.py"
CA, SK = "sipclerk/call.py", "sipclerk/sip/stack.py"
PA = "sipclerk/intake/parsers.py"
MS, TN = "sipclerk/sip/message.py", "sipclerk/tenants.py"
GD = "sipclerk/guard.py"

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
    # ---- names are read as written in any language (F5)
    D("parsers-squash-keeps-letters", PA, "_squash", "and <-> or", "in keep", "a letter of any language, a combining mark or an apostrophe is turned into a space", NAMES),
    D("parsers-squash-digits-flag", PA, "_squash", "and <-> or", "digits or", "the digits flag no longer decides whether a digit stays", NAMES),
    D("parse-name-digits-are-not-letters", PA, "parse_name", "False -> True", "False", "a digit inside a name is kept as part of the name", NAMES),
    # ---- which firm a call is for is read from what was dialled, in every form, and never guessed (F6, F11)
    D("dial-plus-sign", MS, "_number_from", "negate the condition", "n.startswith('+')", "a number written with a plus sign is read as a national one", DIAL),
    D("dial-00-prefix", MS, "_number_from", "negate the condition", "digits.startswith('00')", "a number written with the 00 prefix is not read as international", DIAL),
    D("dial-eleven-digits", MS, "_number_from", "negate the condition", "len(digits) >= 11", "eleven digits with no plus are not read as an international number", DIAL),
    D("dial-candidates-distinct", MS, "dialled_numbers", "negate the condition", "candidate and candidate not in out", "the dialled number candidates are collected wrongly", DIAL),
    D("tenants-resolve-international", TN, "resolve", "negate the condition", "c.startswith('+')", "an international number is looked up as if it were national", DIAL),
    D("tenants-resolve-national-ten-digits", TN, "resolve", "negate the condition", "fullmatch", "a national number is never matched", DIAL),
    D("tenants-resolve-national-unique", TN, "resolve", "Eq -> NotEq", "len(hits) == 1", "a national number two firms share loads one of them", DIAL),
    D("stack-400-needs-the-five-headers", SK, "_refuse_unreadable_invite", "Lt -> GtE", "len(found) < 5", "an unparseable INVITE gets no 400, or a 400 that is not a valid response", DIAL),
    D("stack-400-only-for-an-invite", SK, "_refuse_unreadable_invite", "negate the condition", "re.match", "a request that is not an INVITE gets a 400 or an INVITE gets none", DIAL),
    # ---- a dialog follows the route set of the proxies on its path (F7)
    D("message-record-route-echo", MS, "make_response", "negate the condition", "'INVITE'", "the 200 OK does not echo the proxies' Record-Route", ROUTE),
    D("message-record-route-not-on-refusals", MS, "make_response", "Lt -> GtE", "101", "a refusal echoes Record-Route as if it made a dialog", ROUTE),
    D("message-loose-router-test", MS, "is_loose_route", "return value -> None", "", "no router is ever loose", ROUTE),
    D("message-list-header-quoted-comma", MS, "split_list_header", "and <-> or", "depth == 0", "a comma inside quotes or brackets splits an entry in two", ROUTE),
    D("stack-no-route-set-no-route", SK, "request_target", "negate the condition", "not self.route_set", "a dialog without a route set still builds routes", ROUTE),
    D("stack-loose-router-first", SK, "request_target", "negate the condition", "is_loose_route", "a loose router first is treated as strict and the other way round", ROUTE),
    D("stack-next-hop-is-the-first-route", SK, "next_hop", "negate the condition", "_ip_literal(host)", "a request is sent to where the INVITE came from, not to the first route", ROUTE),
    # ---- a withheld caller id is nobody in particular (F10)
    D("server-withheld-caller-is-not-a-duplicate", SV, "_already_on_this_line", "negate the condition", "caller_is_withheld", "a second anonymous caller is turned away as a duplicate", ANON),
    D("server-duplicate-needs-the-same-firm", SV, "_already_on_this_line", "Eq -> NotEq", "s.cfg.tenant_id == tenant_id", "the same number on the other firm's line counts as a duplicate", ANON),
    D("message-withheld-placeholder-numbers", MS, "caller_is_withheld", "and <-> or", "WITHHELD_NUMBERS", "the keypad placeholder for anonymous counts as a real number", ANON),
    D("message-withheld-zeros", MS, "caller_is_withheld", "Eq -> NotEq", "set(digits)", "a caller id of only zeros counts as a real number", ANON),
    # ---- the emergency rule fires on a danger happening now, not on an account of one (F12)
    D("guard-emergency-account-dropped", GD, "_emergency_now", "negate the condition", "_is_account", "the emergency rule fires only on accounts of the past", TENSE),
    D("guard-emergency-present-verb-is-live", GD, "_is_account", "negate the condition", "(?:is|are)", "a hit that says `is bleeding` is dropped when the clause mentions the past", TENSE),
    D("guard-emergency-now-before-the-hit", GD, "_is_account", "negate the condition", "_NOW_BEFORE_HIT", "`he's unconscious after the fall` is dropped as an account", TENSE),
    D("guard-emergency-past-clause", GD, "_is_account", "return value -> None", "", "no clause is ever an account of the past", TENSE),
    # ---- the emergency rule fires on a danger now, not on an injury or a deadline being reported (output round 2, F1 and F2)
    D("guard-report-is-checked", GD, "_emergency_now", "remove `not`", "_is_report", "a report of an injury is taken for an alarm and an alarm is dropped as a report", REPORT),
    D("guard-report-body-part", GD, "_is_report", "negate the condition", "_BODY_PART_BEFORE", "`my back is hurt` is an alarm and `my husband is hurt` is a report", REPORT),
    D("guard-report-hedge", GD, "_is_report", "return value -> None", "_HEDGE_BEFORE", "`kind of an emergency` is an alarm", REPORT),
    D("guard-report-asks-for-help", GD, "_is_report", "remove `not`", "_HELP_NOW", "`my son is hurt, please help` is dropped as a report", REPORT),
    D("guard-report-badly-hurt", GD, "_is_report", "remove `not`", "(?:badly|seriously)", "`my husband is badly hurt` is dropped as a report", REPORT),
    # ---- a practice area the caller confirmed in scope is not overturned by a word in the description (output round 2, F8)
    D("machine-confirmed-area-wins", "sipclerk/intake/machine.py", "screen", "remove `not`", "_area_in_scope", "a word in the description turns away a caller whose area the firm takes", SCOPE),
    D("machine-area-must-be-one-the-firm-takes", "sipclerk/intake/machine.py", "_area_in_scope", "IsNot -> Is", "self.cfg.area", "an area the firm does not list counts as confirmed in scope", SCOPE),
    D("machine-unknown-area-keeps-the-check", "sipclerk/intake/machine.py", "_area_in_scope", "IsNot -> Is", "fs.value is not None", "a skipped area counts as confirmed and the word check is dropped", SCOPE),
    # ---- a word that is also a filler is not a veto on a real name (output round 2, F7)
    D("parse-name-ambiguous-words-are-listed", PA, "parse_name", "In -> NotIn", "NAME_AMBIGUOUS", "An, To, Case and Hurt are not read as parts of a name", STOPWORDS),
    D("parse-name-ambiguous-needs-a-capital", PA, "parse_name", "remove `not`", "named or all", "a lower case an, to, case or hurt is read as part of a name, or a capitalised one is not", STOPWORDS),
    D("parse-name-lone-ambiguous-word", PA, "parse_name", "Eq -> NotEq", "len(ambiguous) == len(tokens)", "`to` or `an` on its own is taken for a name", STOPWORDS),
    D("proc-model-name-is-a-name", "sipclerk/voice/processors.py", "_interpret", "True -> False", "True", "the model's extracted name is rejected by the capital letter check", STOPWORDS, nth=1),
    # ---- who is calling is read one way: the stored number, the block list and the one call per number rule (output round 2, F5 and F6)
    D("guard-blocked-caller", GD, "check_caller", "negate the condition", "match_number", "the blocked caller list is ignored", CALLERS),
    D("caller-number-reads-uris", MS, "caller_number", "negate the condition", "re.search", "a From header with sip, sips or tel in it is read as a bare number", CALLERS),
    D("caller-number-uri-form", MS, "caller_number", "return value -> None", "number_in_uri", "a caller id written as a URI reads as nobody", CALLERS),
    D("caller-number-bare-form", MS, "caller_number", "return value -> None", "_number_from", "a number written without a scheme, as a firm writes its block list, reads as nobody", CALLERS),
    D("match-number-needs-the-input", MS, "match_number", "remove `not`", "not probe", "an empty caller matches an empty entry", CALLERS),
    D("match-number-national-suffix", MS, "match_number", "negate the condition", "fullmatch", "a ten digit caller is never matched to an international number that ends with it", CALLERS),
    D("match-number-national-unique", MS, "match_number", "Eq -> NotEq", "len(hits) == 1", "a ten digit number two countries share is taken for one of them", CALLERS),
    D("server-duplicate-uses-match-number", SV, "_already_on_this_line", "return value -> None", "match_number", "a caller already in a call is never recognised", CALLERS),
    D("server-duplicate-ignores-withheld-open-calls", SV, "_already_on_this_line", "remove `not`", "caller_is_withheld(c)", "a withheld open call is compared with the new caller", CALLERS),
    # ---- every outcome that stores a callback task reaches the same receivers by one code path (output round 2, F4)
    D("queue-call-bridge-needs-a-task", DE, "queue_call", "and <-> or", "record['tasks']", "a call that carries no task is queued for the case-management bridge", RECEIVERS),
    D("queue-call-console-gets-every-call", DE, "queue_call", "negate the condition", "call_console", "a call is not queued for the console", RECEIVERS),
    D("queue-call-only-names-a-receiver", DE, "queue_call", "In -> NotIn", "only in (None, 'bridge')", "the early bridge entry is queued for no receiver or for both", RECEIVERS),
    D("server-file-call-queues-the-receivers", SV, "file_call", "delete the call statement", "queue_call", "a finished call is stored and never queued for a receiver", RECEIVERS),
    D("machine-hangup-leaves-a-task", "sipclerk/intake/machine.py", "on_caller_hangup", "delete the call statement", "self.tasks.append", "a caller who hangs up mid intake leaves no callback task", RECEIVERS),
    D("machine-hangup-keeps-an-outcome", "sipclerk/intake/machine.py", "on_caller_hangup", "remove `not`", "not self.outcome", "a hang up overwrites the outcome a call already has", RECEIVERS),
]


PIPELINE_DERIVED: list[DerivedMutant] = [
    PD("pipe-duplicate-call", SV, "on_invite", "negate the condition", "_already_on_this_line", "the same caller can hold two calls on one line", "same_caller"),
    PD("pipe-anonymous-callers", SV, "_already_on_this_line", "negate the condition", "caller_is_withheld", "a second anonymous caller is turned away as a duplicate", "anonymous"),
    PD("pipe-caller-hangs-up-during-the-transfer", CA, "_watch_caller_bye", "Is -> IsNot", "self.bridged_at is None", "a caller who hangs up while the desk phone rings or hears the whisper does not end the call",
       "desk_invite_and_still or hangs_up_the_desk"),
    D("pipe-bridge-delivery", DE, "queue_call", "negate the condition", "case_management", "a finished intake is not sent to the case-management receiver",
      ["tests/test_pipeline_fast.py", "-k", "full_intake or saved_call"], layer="pipeline"),
    D("pipe-console-delivery", DE, "queue_call", "negate the condition", "call_console", "the masked call is not sent to the console",
      ["tests/test_pipeline_fast.py", "-k", "full_intake or saved_call"], layer="pipeline"),
    D("pipe-hangup-reaches-the-bridge", CA, "_finalize", "delete the call statement", "on_caller_hangup", "a caller who hangs up mid intake leaves no outcome, no task and nothing for the bridge",
      ["tests/test_outcome_receivers.py", "-k", "reaches_the_bridge"], layer="pipeline"),
    # ---- the line waits for the ACK as long as the stack takes one, and a call whose ACK never comes is ended and recorded (output round 2, F3); real time, so the pipeline layer
    D("pipe-ack-window-is-64-t1", SK, "_retransmit_until_ack", "Mult -> FloorDiv", "64 * t1", "the ACK is waited for far longer or far shorter than 64 * T1", ACKWIN, nth=1, layer="pipeline"),
    D("pipe-ack-2xx-is-resent", SK, "_retransmit_until_ack", "delete the call statement", "self.stack.send", "the 2xx is not resent while the ACK is awaited", ACKWIN, layer="pipeline"),
    D("pipe-ack-stops-the-resend", SK, "_retransmit_until_ack", "negate the condition", "done", "the 2xx is resent and the call ended after the ACK came", ACKWIN, layer="pipeline"),
    D("pipe-ack-lost-is-told", SK, "_retransmit_until_ack", "delete the call statement", "self.ack_lost.set()", "the line is never told the ACK was lost", ACKWIN, layer="pipeline"),
    D("pipe-ack-answer-from-the-stack", SK, "wait_for_ack", "negate the condition", "self.ack_lost.is_set()", "a lost ACK is reported as a caller who hung up", ACKWIN, layer="pipeline"),
    D("pipe-ack-line-waits-for-the-stack", SV, "_take_call", "negate the condition", "got != 'acked'", "a call whose ACK came is dropped, or one whose ACK did not is started", ACKWIN, layer="pipeline"),
    D("pipe-ack-lost-is-recorded", SV, "_take_call", "negate the condition", "got == 'lost'", "a call whose ACK never came leaves no record", ACKWIN, layer="pipeline"),
    D("pipe-ack-record-has-the-cause", SV, "_record_unacknowledged_call", "delete the call statement", "m.on_no_ack()", "the record of a lost ACK does not name the cause", ACKWIN, layer="pipeline"),
    D("pipe-ack-record-is-filed", SV, "_record_unacknowledged_call", "delete the call statement", "self.file_call", "the record of a lost ACK is built and never stored or queued", ACKWIN, layer="pipeline"),
    D("pipe-ack-record-leaves-a-task", "sipclerk/intake/machine.py", "on_no_ack", "delete the call statement", "self.tasks.append", "a call whose ACK never came leaves no callback task", ACKWIN, layer="pipeline"),
    D("pipe-ack-unconfirmed-call-is-on-the-line", SV, "_already_on_this_line", "Add= -> Sub=", "open_here", "a caller answered and not yet confirmed can be answered a second time", ACKWIN, layer="pipeline"),
    D("pipe-ack-unconfirmed-call-is-for-its-firm", SV, "_already_on_this_line", "Eq -> NotEq", "t == tenant_id", "an unconfirmed call blocks the other firm's line or none", ACKWIN, layer="pipeline"),
    D("pipe-ack-unconfirmed-call-is-forgotten", SV, "on_invite", "delete the call statement", "_answering.pop", "a call that ended without a session still blocks its number", ACKWIN, layer="pipeline"),
    D("pipe-ack-stopped-line-starts-no-call", SV, "_take_call", "negate the condition", "self._closing", "a line that was stopped while a call awaited its ACK starts the call afterwards", ACKWIN, layer="pipeline"),
    D("pipe-ack-early-hangup-starts-the-pipeline-first", CA, "run", "Lt -> GtE", "time.monotonic() < deadline", "a call that ends before its pipeline has started cancels nothing, and the process hangs at the end of the loop",
      ["tests/test_ack_window.py", "-k", "instant"], layer="pipeline"),
    D("pipe-dialled-shapes", TN, "resolve", "negate the condition", "fullmatch", "a call dialled as ten digits is turned away",
      ["tests/test_dialled_number.py", "-k", "line_answers_each_shape"], layer="pipeline"),
]
