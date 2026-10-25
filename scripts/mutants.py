"""The mutants: one deliberate break per guard, rule and state-machine branch.

Each mutant names the file, the exact text to replace, what to replace it with, and the tests that must go red.
`scripts/mutation_check.py --check` fails if any `old` text no longer appears exactly once, so a refactor cannot
silently turn a mutant into a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FAST = ["tests/test_guard.py", "tests/test_machine.py", "tests/test_machine_property.py", "tests/test_parsers.py", "tests/test_tenants.py",
        "tests/test_store.py", "tests/test_delivery.py", "tests/test_sip_stack.py", "tests/test_audio.py", "tests/test_llm.py",
        "tests/test_processors.py", "tests/test_retention.py", "tests/test_wiring.py", "tests/test_outbox_at_rest.py",
        "tests/test_logs_carry_no_caller_words.py", "tests/test_delivery_window.py",
        "tests/test_transfer_leg.py",
        "tests/test_names_unicode.py", "tests/test_dialled_number.py",
        "tests/test_route_set.py"]


@dataclass
class Mutant:
    id: str
    file: str
    old: str
    new: str
    tests: list[str] = field(default_factory=lambda: list(FAST))
    desc: str = ""
    kind: str = "replace"  # or "empty_list": `old` is the module-level list's name
    layer: str = "unit"  # unit, pipeline, voice


def M(id, file, old, new, desc, tests=None, kind="replace", layer="unit") -> Mutant:
    return Mutant(id, file, old, new, tests if tests is not None else list(FAST), desc, kind, layer)


G, MA, PA, TE, ST, DE, SI = ("sipclerk/guard.py", "sipclerk/intake/machine.py", "sipclerk/intake/parsers.py", "sipclerk/tenants.py",
                             "sipclerk/store.py", "sipclerk/delivery.py", "sipclerk/sip/")

PIPE_FAST = ["tests/test_pipeline_fast.py"]

MUTANTS: list[Mutant] = [
    # ---- the guard, before the model
    M("guard-emergency", G, "EMERGENCY", "", "the emergency rule is deleted", kind="empty_list"),
    M("guard-existing-client", G, "EXISTING_CLIENT", "", "the existing client rule is deleted", kind="empty_list"),
    M("guard-human-request", G, "HUMAN_REQUEST", "", "the request for a person rule is deleted", kind="empty_list"),
    M("guard-legal-advice-request", G, "LEGAL_ADVICE_REQUEST", "", "the legal advice request rule is deleted", kind="empty_list"),
    M("guard-out-of-scope", G, "    if check_scope:\n        in_scope", "    if False:\n        in_scope", "the out of scope practice area check is deleted"),
    M("guard-in-scope-wins", G, "if oos and not in_scope:", "if oos:", "an in scope word no longer outranks an out of scope one"),
    M("guard-blocked-caller", G, "if number and number in tenant.guard.blocked_callers:", "if False:", "the blocked caller list is ignored"),
    # ---- the guard, after the model
    M("guard-promised-outcome", G, "PROMISED_OUTCOME", "", "the promised outcome rule is deleted", kind="empty_list"),
    M("guard-legal-advice-out", G, "LEGAL_ADVICE_OUT", "", "the agent legal advice rule is deleted", kind="empty_list"),
    M("guard-attorney-client", G, "ATTORNEY_CLIENT", "", "the implied attorney client relationship rule is deleted", kind="empty_list"),
    M("guard-unmade-commitment", G, "UNMADE_COMMITMENT", "", "the unmade commitment rule is deleted", kind="empty_list"),
    M("guard-output-off", G, "    if hits:\n        return OutputVerdict(False, hits, SAFE_REPLACEMENT)", "    if False:\n        return OutputVerdict(False, hits, SAFE_REPLACEMENT)",
      "the output guard blocks nothing"),
    M("guard-no-replacement", G, "return OutputVerdict(False, hits, SAFE_REPLACEMENT)", "return OutputVerdict(False, hits, text)",
      "a blocked line is not replaced"),
    # ---- the state machine
    M("machine-retry-limit-off-by-one", MA, "        if fs.retries > q.max_retries:\n            if q.required:\n                self._log(\"retry_limit\")",
      "        if fs.retries >= q.max_retries:\n            if q.required:\n                self._log(\"retry_limit\")", "one retry fewer than the question allows"),
    M("machine-required-never-escalates", MA, "            if q.required:\n                self._log(\"retry_limit\")", "            if False:\n                self._log(\"retry_limit\")",
      "a required field that keeps failing is skipped instead of escalated"),
    M("machine-no-readback", MA, "        if q.confirm:\n            self.pending = p.value", "        if False:\n            self.pending = p.value", "the read-back confirmation is skipped"),
    M("machine-confirm-not-recorded", MA, "fs.value, fs.confirmed, fs.source = self.pending, True, fs.source or source",
      "fs.value, fs.confirmed, fs.source = self.pending, False, fs.source or source", "a confirmed value is stored as unconfirmed"),
    M("machine-correction-ignored", MA, "        if p.remainder:  # \"no, it's Jordan Reyes\"", "        if False:  # \"no, it's Jordan Reyes\"",
      "a correction spoken with the no is thrown away"),
    M("machine-unclear-confirm-forever", MA, "            if self.unclear_confirms > MAX_UNCLEAR_CONFIRMS:\n                self.unclear_confirms = 0",
      "            if False:\n                self.unclear_confirms = 0", "an unclear yes or no is asked again forever"),
    M("machine-unclear-consent-records", MA, "            self.recording = False  # no clear yes means no recording", "            self.recording = True",
      "no clear yes to the recording question still records"),
    M("machine-consent-no-records", MA, "        if p.ok and p.value is False:\n            self.recording = False", "        if p.ok and p.value is False:\n            self.recording = True",
      "a no to recording still records"),
    M("machine-consent-yes-not-recorded", MA, "            self.recording = self.cfg.recording_enabled\n            self.state = State.ASK",
      "            self.recording = False\n            self.state = State.ASK", "a yes to recording never starts the recording"),
    M("machine-all-party-records-at-start", MA, "self.recording = False if self._consent_required else self.cfg.recording_enabled",
      "self.recording = self.cfg.recording_enabled", "an all-party-consent firm records before the caller agrees"),
    M("machine-dtmf-zero-ignored", MA, "        if digit == \"0\" and not (self.state == State.ASK and self.keypad):", "        if False and not (self.state == State.ASK and self.keypad):",
      "pressing zero does not reach a person"),
    M("machine-dtmf-zero-in-number", MA, "if digit == \"0\" and not (self.state == State.ASK and self.keypad):", "if digit == \"0\":",
      "a zero typed inside a phone number transfers the call"),
    M("machine-dtmf-logged", MA, "self._log(\"dtmf\", digit=\"0\" if digit == \"0\" else \"x\")", "self._log(\"dtmf\", digit=digit)",
      "keypad digits are written to the event log"),
    M("machine-dtmf-pound", MA, "        if digit == \"#\":\n            digits, self.keypad = self.keypad, \"\"", "        if digit == \"@\":\n            digits, self.keypad = self.keypad, \"\"",
      "pound no longer ends a keypad entry"),
    M("machine-dtmf-star", MA, "        if digit == \"*\":\n            self.keypad = \"\"", "        if digit == \"@\":\n            self.keypad = \"\"", "star no longer repeats the question"),
    M("machine-silence-limit", MA, "        if self.silences >= MAX_SILENCES:", "        if self.silences >= MAX_SILENCES + 100:", "endless silence never ends the call"),
    M("machine-silence-not-reset", MA, "        self.silences = 0\n        if self.state == State.CONSENT:\n            return self._consent_answer(p)",
      "        if self.state == State.CONSENT:\n            return self._consent_answer(p)", "an answer does not reset the silence count"),
    M("machine-existing-client-flag", MA, "(p.value is True) == (q.escalate_on == \"yes\")", "(p.value is True) != (q.escalate_on == \"yes\")",
      "the existing client answer escalates the wrong way round"),
    M("machine-advice-repeat", MA, "if self.legal_advice_hits >= 2 and self.cfg.guard.legal_advice_repeat_escalates:", "if False and self.cfg.guard.legal_advice_repeat_escalates:",
      "a repeated request for advice never reaches a person"),
    M("machine-unsupported-area", MA, "if q.type == \"enum\" and q.id == \"practice_area\" and self.cfg.area(str(p.value)) is None:",
      "if False and q.id == \"practice_area\" and self.cfg.area(str(p.value)) is None:", "an unsupported practice area is accepted"),
    M("machine-after-hours-transfer", MA, "if self.cfg.transfer.enabled and self.in_hours:", "if self.cfg.transfer.enabled:", "a transfer is attempted out of hours"),
    M("machine-qualification-order", MA, 'order = {"qualified": 0, "needs_review": 1, "priority": 2, "decline": 3}', 'order = {"qualified": 0, "needs_review": 1, "priority": 3, "decline": 2}',
      "a priority flag outranks a decline"),
    M("machine-missing-field-passes", MA, "        if v is None:  # a field the caller never gave cannot satisfy a rule\n            return False",
      "        if v is None:  # a field the caller never gave cannot satisfy a rule\n            return True", "a missing field satisfies a qualification rule"),
    M("machine-boundary-days", MA, ".days <= int(r.value)", ".days < int(r.value)", "the three year window is off by one day"),
    M("machine-key-ignores-call", MA, 'f"{self.cfg.tenant_id}|{self.call_id}|intake"', 'f"{self.cfg.tenant_id}|intake"', "the idempotency key is the same for every call"),
    M("machine-stamp-missing", MA, "            **self.tenant.stamp,\n", "", "the config version and hash are not stamped on the record"),
    M("machine-no-inactive-guard", MA, "return self.state in (State.DONE, State.TRANSFERRING)", "return False", "events after the call ends are acted on"),
    M("machine-no-submit", MA, "return [*says, Submit(self.record()), Hangup(reason)]", "return [*says, Hangup(reason)]", "a finished intake is never submitted"),
    M("machine-optional-skip", MA, "        if p.skipped:\n            fs.skipped = True", "        if False:\n            fs.skipped = True", "an optional field cannot be skipped"),
    M("machine-transfer-failed-no-task", MA, '        self.tasks.append({"kind": "callback", "reason": f"transfer failed',
      '        self.tasks.append({"kind": "noop", "reason": f"transfer failed', "a failed transfer leaves no callback task"),
    # ---- the field readers
    M("parsers-nanp", PA, '    if digits[0] in "01" or digits[3] in "01":', "    if False:", "an impossible area code is accepted"),
    M("parsers-country-code", PA, '    if len(digits) == 11 and digits.startswith("1"):', '    if False and digits.startswith("1"):', "a leading 1 is not stripped"),
    M("parsers-future-date", PA, "    if found > today:", "    if False:", "a date in the future is accepted"),
    M("parsers-too-old", PA, "    if max_age_days is not None and (today - found).days > max_age_days:", "    if False:", "an impossibly old date is accepted"),
    M("parsers-last-name", PA, "    if len(tokens) < 2:", "    if False:", "a first name alone is accepted"),
    M("parsers-name-length", PA, "    if len(tokens) > 5 or any(t.lower() in NAME_STOP for t in tokens):", "    if len(tokens) > 500:", "a sentence is accepted as a name"),
    M("parsers-skip-required", PA, "    if not question.required and is_skip(text):", "    if is_skip(text):", "a required question can be skipped"),
    M("parsers-email", PA, "    if EMAIL_RE.match(n):", "    if True:", "anything is accepted as an email address"),
    M("parsers-yesno-ambiguity", PA, "    if yes_i is not None and no_i is not None:", "    if False:", "yes and no in one answer is read as yes"),
    M("parsers-area-ambiguity", PA, "    if len(scored) > 1 and scored[0][0] == scored[1][0]:", "    if False:", "an ambiguous practice area is guessed"),
    M("parsers-min-words", PA, "    if len(words) < min_words:", "    if False:", "a one word description is accepted"),
    M("parsers-mask", PA, "    return re.sub(spoken, \"[number]\", t, flags=re.I)", "    return text", "numbers and emails are not masked for the console copy"),
    # ---- tenants
    M("tenants-duplicate-did", TE, "            if t.config.did in self._by_did:", "            if False:", "two firms can answer the same number"),
    M("tenants-extra-fields", TE, 'model_config = ConfigDict(extra="forbid")', 'model_config = ConfigDict(extra="ignore")', "a misspelt config key is ignored"),
    M("tenants-hours-end", TE, "return start <= local.time() < end", "return start <= local.time() <= end", "the office is open one minute too long"),
    M("tenants-hash-order", TE, "json.dumps(raw, sort_keys=True, separators=(\",\", \":\"), ensure_ascii=True)", "json.dumps(raw, sort_keys=False, separators=(\",\", \":\"), ensure_ascii=True)",
      "the config hash depends on key order"),
    M("tenants-rule-field", TE, "            if r.field not in ids:", "            if False:", "a rule can read a field that does not exist"),
    # ---- storage
    M("store-shared-key", ST, 'info=b"sipclerk-at-rest:" + tenant_id.encode()', 'info=b"sipclerk-at-rest"', "every firm shares one encryption key"),
    M("store-no-aad", ST, '        return f"{tenant_id}|{call_id}|{part}".encode()', '        return b""', "an encrypted file can be moved to another call"),
    M("store-tenant-check", ST, "        if actor.tenant_id != tenant_id:\n            return False", "        if False:\n            return False", "staff of one firm can read another firm's calls"),
    M("store-recording-roles", ST, "            return actor.role in cfg.recording_roles", "            return actor.role in cfg.staff_roles", "any staff role can play a recording"),
    M("store-audit-read", ST, '        self._audit(tenant_id, actor.name, actor.role, "read", call_id, part, "ok")\n', "", "an allowed read leaves no audit line"),
    M("store-audit-denied", ST, '            self._audit(tenant_id, actor.name, actor.role, "read", call_id, part, f"denied (actor tenant {actor.tenant_id})")\n', "",
      "a denied read leaves no audit line"),
    M("store-audit-chain", ST, '                prev = json.loads(lines[-1])["hash"]', "                prev = CHAIN_START", "the audit log is not chained"),
    M("store-call-id", ST, '        if not call_id.replace("-", "").replace("_", "").isalnum():', "        if False:", "a call id can walk out of its folder"),
    M("store-master-key", ST, "        if len(master_key) < 32:", "        if False:", "a short master key is accepted"),
    # ---- delivery
    M("delivery-duplicate-enqueue", DE, "        if ek in self.entries:\n            return False", "        if False:\n            return False", "a delivery can be queued twice"),
    M("delivery-5xx-not-retried", DE, "elif code in RETRYABLE or code >= 500 or code == 0:", "elif code in RETRYABLE or code == 0:", "a server error is not retried"),
    M("delivery-attempt-limit", DE, "                    if e.attempts >= MAX_ATTEMPTS:", "                    if False:", "retries never stop"),
    M("delivery-no-backoff", DE, "e.next_at = self.clock() + BACKOFF_S[min(e.attempts - 1, len(BACKOFF_S) - 1)]", "e.next_at = self.clock()", "retries hammer the receiver"),
    M("delivery-bridge-signature", DE, "    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()", "    return hmac.new(secret.encode(), raw_body + b' ', hashlib.sha256).hexdigest()",
      "the bridge signature covers the wrong bytes"),
    M("delivery-console-signature", DE, 'f"{ts}.".encode() + raw_body', "raw_body", "the console signature leaves out the timestamp"),
    M("delivery-key-header", DE, 'headers = {"content-type": "application/json", "idempotency-key": key}', 'headers = {"content-type": "application/json", "idempotency-key": str(id(raw))}',
      "a retry carries a new idempotency key"),
    M("delivery-console-mask", DE, 'mask_for_transcript(t["text"])[:2000]', 't["text"][:2000]', "the console copy of the transcript is not masked"),
    # ---- SIP and RTP
    M("sip-dtmf-dedupe", SI + "rtp.py", "        if ts == self._last_dtmf_ts:", "        if False:", "one keypad press arrives as many digits"),
    M("sip-retransmit", SI + "stack.py", '        if method != "ACK" and key in self._seen:', "        if False:", "a retransmitted INVITE starts a second call"),
    M("sip-codec-order", SI + "message.py", "    for codec in preference:", "    for codec in reversed(preference):", "the codec preference order is ignored"),
    M("sip-bye", SI + "stack.py", '            d.ended_by = "remote"\n            d.terminated.set()', '            d.ended_by = "remote"', "a BYE does not end the dialog"),
    M("sip-dedupe-seq", SI + "rtp.py", "            if gap == 0 or gap > 0x8000:\n                return  # duplicate or late: drop", "            if False:\n                return", "duplicate RTP packets are decoded twice"),
]
