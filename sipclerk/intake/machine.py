"""The intake state machine: what happens next is decided here, in code, never by a prompt.

It is a pure object. It is fed events (a parsed answer, a keypad digit, a silence timeout, a guard
verdict, a transfer result) and returns actions (say this, start the transfer, submit the record,
hang up). It never touches audio, a network or a model, so every branch is a unit test and the same
call can be replayed from its events.

Per field: ASK, then optionally CONFIRM (read back and wait for a yes). A failed answer counts a
retry; past the question's limit a required field escalates to a person and an optional one is
skipped. A silence past the question's timeout re-prompts, and past the call's silence limit ends the
call with a callback task, so nobody is stranded on a dead line.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from sipclerk import guard
from sipclerk.intake import parsers
from sipclerk.intake.parsers import Parsed
from sipclerk.tenants import LoadedTenant, Question

# Fixed lines the machine and the pipeline can say, so the speech cache can be filled before the first call.
FIXED_LINES = [
    "If anyone is in immediate danger, please hang up and call 911 now.",
    "I cannot give legal advice, and an attorney is the right person for that question.",
    "I cannot give legal advice or say how a matter would turn out. An attorney will look at your details.",
    "Is that right?",
    "Please say yes or no.",
    "Sorry, was that right?",
    "Thanks.",
    "Sorry, I did not catch that.",
    "Sorry about that.",
    "That is fine, I will not record this call.",
    "I will not record this call.",
    "Sorry, is it all right if the firm records this call?",
    "Is it all right if the firm records this call? Please say yes or no.",
    "I am not hearing anything, so I will end the call. Please ring again whenever you are ready.",
    "Are you still there?",
    "I will make sure someone calls you back.",
    "Thank you. I also need your last name.",
    "That date has not happened yet. Roughly when did it take place?",
    "That was more digits than a phone number. Please say it again, area code first.",
    "Please say the rest of the number.",
    "Of course, take your time.",
    "I want to be careful here. I cannot give legal advice. An attorney will review your details.",
]
import re

SAME_NUMBER = re.compile(r"\b(?:this|same|that) (?:number|one|phone)\b|\bcalling from\b|\bsame as (?:this|the one)\b")
MAX_SILENCES = 3  # consecutive silent prompts before the call is ended
MAX_UNCLEAR_CONFIRMS = 1


class State(str, Enum):
    CONSENT = "consent"
    ASK = "ask"
    CONFIRM = "confirm"
    TRANSFERRING = "transferring"
    DONE = "done"


@dataclass
class Say:
    text: str
    kind: str = "prompt"  # prompt, disclosure, ack, closing, notice


@dataclass
class StartTransfer:
    reason: str


@dataclass
class Hangup:
    reason: str


@dataclass
class Submit:
    record: dict


Action = Say | StartTransfer | Hangup | Submit


@dataclass
class FieldState:
    value: object = None
    confirmed: bool = False
    source: str = ""  # spoken, keypad
    retries: int = 0
    skipped: bool = False
    partial: str = ""  # phone digits heard so far when the caller paused between groups
    partial_rounds: int = 0


class IntakeMachine:
    def __init__(self, tenant: LoadedTenant, call_id: str, caller_number: str, now_utc: datetime):
        self.tenant = tenant
        self.cfg = tenant.config
        self.call_id = call_id
        self.caller_number = caller_number
        self.started_at = now_utc
        self.today: date = self.cfg.hours.local(now_utc).date()
        self.in_hours = self.cfg.is_open(now_utc)
        self.state = State.CONSENT
        self.index = 0
        self.fields: dict[str, FieldState] = {q.id: FieldState() for q in self.cfg.questions}
        self.pending: object = None  # value waiting for a yes
        self.keypad = ""
        self.silences = 0
        self.unclear_confirms = 0
        self.recording = False if self._consent_required else self.cfg.recording_enabled
        self.consent_asked = False
        self.escalation: str = ""
        self.outcome: str = ""
        self.tasks: list[dict] = []
        self.events: list[dict] = []  # guard and state events, for the call record
        self.legal_advice_hits = 0
        self.qualification: dict = {}
        self.transfer: dict = {"attempted": False}
        self.submitted = False

    # ------------------------------------------------------------------ helpers

    @property
    def _consent_required(self) -> bool:
        return any(d.ask_consent for d in self.cfg.disclosures) and self.cfg.jurisdiction.recording == "all_party_consent"

    @property
    def question(self) -> Question | None:
        return self.cfg.questions[self.index] if self.index < len(self.cfg.questions) else None

    @property
    def current_timeout_s(self) -> float:
        q = self.question
        if self.state == State.CONSENT:
            return q.timeout_s if q else 8.0  # the recording question waits as long as the first intake question does
        return q.timeout_s if (q and self.state in (State.ASK, State.CONFIRM)) else 8.0

    DEFAULT_ENDPOINT_S = 0.5
    ENDPOINT_BY_TYPE = {"text": 1.2, "phone": 1.0}  # people pause inside a story and between groups of digits

    @property
    def endpoint_s(self) -> float:
        """How much silence ends the caller's turn right now: longer for a story or a number, short for a yes."""
        q = self.question
        if q is None or self.state != State.ASK:
            return self.DEFAULT_ENDPOINT_S
        return q.endpointing_s or self.ENDPOINT_BY_TYPE.get(q.type, self.DEFAULT_ENDPOINT_S)

    @property
    def done(self) -> bool:
        return self.state == State.DONE

    @property
    def _inactive(self) -> bool:
        """Once the call is over, or while a transfer is being set up, nothing the caller does changes the intake."""
        return self.state in (State.DONE, State.TRANSFERRING)

    def _log(self, kind: str, **kw) -> None:
        self.events.append({"kind": kind, "state": self.state.value, "field": self.question.id if self.question else "", **kw})

    # ------------------------------------------------------------------ start

    def begin(self) -> list[Action]:
        acts: list[Action] = [Say(self.cfg.greeting, "disclosure")]
        for d in self.cfg.disclosures:
            if d.ask_consent and not self._consent_required:
                # a notice jurisdiction: the disclosure is given, and the caller is not asked
                acts.append(Say(d.text.replace("Is that all right with you?", "").strip(), "disclosure"))
            elif d.ask_consent:
                acts.append(Say(d.text, "disclosure"))
                self.consent_asked = True
            else:
                acts.append(Say(d.text, "disclosure"))
        if not self.in_hours:
            acts.append(Say(self.cfg.after_hours_message, "notice"))
            self._log("after_hours")
        if self.consent_asked:
            self.state = State.CONSENT
            return acts
        self.state = State.ASK
        acts.append(Say(self.question.prompt))
        return acts

    # ------------------------------------------------------------------ screening (guard first)

    def screen(self, text: str) -> guard.InputVerdict:
        """The guard's verdict on what the caller said, before any model sees it."""
        if self.state == State.CONSENT:
            return guard.check_input(text, self.cfg)
        q = self.question
        scope = q is not None and (q.id == "practice_area" or (q.id == "description" and not self._area_in_scope()))
        asked = self.fields.get("existing_client")
        return guard.check_input(text, self.cfg, check_scope=scope, not_a_client=asked is not None and asked.value is False)

    def _area_in_scope(self) -> bool:
        """True once the caller's practice area is known and the firm takes it: the description is then the story of that matter, and a word in it
        (a DUI charge after the crash that is the case) does not turn the caller away."""
        fs = self.fields.get("practice_area")
        return fs is not None and fs.value is not None and self.cfg.area(str(fs.value)) is not None

    def parse(self, text: str) -> Parsed:
        """The deterministic reading of what the caller said, for whatever the machine is waiting on."""
        if self.state in (State.CONSENT, State.CONFIRM):
            return parsers.parse_yesno(text)
        q = self.question
        if q is None:
            return Parsed(False, reason="no_question")
        if q.type == "phone" and SAME_NUMBER.search(parsers._norm(text)):
            # `this number is fine`, `the number I'm calling from`: the caller ID, offered back for the read-back like any other number.
            own = parsers.parse_phone(self.caller_number or "")
            if own.ok:
                return own
        return parsers.parse_for(q, text, today=self.today, areas=self.cfg.practice_areas)

    def handle_text(self, text: str) -> list[Action]:
        """Guard, then read, then act: the whole turn with no model. The voice pipeline adds a model only on the read step."""
        if self._inactive:
            return []
        v = self.screen(text)
        if v.action != "allow":
            return self.on_guard(v)
        return self.on_parsed(self.parse(text))

    def on_guard(self, v: guard.InputVerdict) -> list[Action]:
        if self._inactive:
            return []
        self._log("guard", rule=v.rule, action=v.action, matched=v.matched)
        if v.action == "emergency":
            return [Say("If anyone is in immediate danger, please hang up and call 911 now.", "notice")] + self._escalate("emergency")
        if v.action in ("escalate",):
            return self._escalate(v.rule)
        if v.action == "out_of_scope":
            self.outcome = "declined_out_of_scope"
            self.tasks.append({"kind": "review_declined_caller", "reason": "caller described a matter the firm does not take"})
            return self._finish([Say(self.cfg.guard.out_of_scope_message, "notice")], "out_of_scope")
        if v.action == "deflect":
            self.legal_advice_hits += 1
            if self.legal_advice_hits >= 2 and self.cfg.guard.legal_advice_repeat_escalates:
                return [Say("I cannot give legal advice, and an attorney is the right person for that question.", "notice")] + \
                    self._escalate("legal_advice_repeat")
            return [Say("I cannot give legal advice or say how a matter would turn out. An attorney will look at your details.", "notice")] + \
                self._reask()
        return []

    # ------------------------------------------------------------------ answers

    def on_parsed(self, p: Parsed, source: str = "spoken") -> list[Action]:
        """The reading of the caller's last answer for the current question (or a yes/no for a confirm)."""
        if self._inactive:
            return []
        self.silences = 0
        if self.state == State.CONSENT:
            return self._consent_answer(p)
        if self.state == State.CONFIRM:
            return self._confirm_answer(p, source)
        if self.state != State.ASK or self.question is None:
            return []
        q = self.question
        fs = self.fields[q.id]
        if p.skipped:
            fs.skipped = True
            self._log("skipped")
            return self._advance()
        if q.type == "name":
            p = self._name_partials(fs, p)
        if q.type == "phone" and source == "spoken":
            p, ask_more = self._phone_partials(fs, p)
            if ask_more:
                return ask_more
        if not p.ok:
            return self._failed(q, fs, p)
        if q.type == "enum" and q.id == "practice_area" and self.cfg.area(str(p.value)) is None:
            return self._decline_area()
        if q.type == "yesno" and q.escalate_on and (p.value is True) == (q.escalate_on == "yes"):
            fs.value, fs.source = p.value, source
            return self._escalate(q.id)
        if q.confirm:
            self.pending = p.value
            fs.source = source
            self.state = State.CONFIRM
            return [Say(f"I have {parsers.readback(q, p.value)}. Is that right?")]
        fs.value, fs.source, fs.confirmed = p.value, source, False
        return self._advance()

    def _name_partials(self, fs: FieldState, p: Parsed) -> Parsed:
        """`Jordan`, then `Reyes`: the first name said alone is kept (as the phone digits are), and the next single name is its last name, so the caller
        is not asked for the same thing until the retry limit sends them to a person."""
        if p.reason == "need_last_name" and p.value:
            if fs.partial and str(p.value).lower() != fs.partial.lower():  # the same word again is the first name repeated, not a last name
                combined = f"{fs.partial} {p.value}"
                fs.partial = ""
                return Parsed(True, combined)
            fs.partial = str(p.value)
            return p
        if p.ok:
            fs.partial = ""
        return p

    MAX_PARTIAL_ROUNDS = 3

    def _phone_partials(self, fs: FieldState, p: Parsed) -> tuple[Parsed, list[Action] | None]:
        """Callers pause between digit groups, and each pause can end a turn. Keep the digits heard and ask only for the rest."""
        if p.ok:
            fs.partial, fs.partial_rounds = "", 0
            return p, None
        digits = p.value if p.reason == "too_short" and isinstance(p.value, str) else ""
        if not digits:
            return p, None  # nothing usable was said: an ordinary failed answer
        combined = fs.partial + digits
        if len(combined) >= 10:
            merged = parsers.validate_phone_digits(combined)  # a caller who starts the whole number over is already ok above
            fs.partial, fs.partial_rounds = "", 0
            return merged, None
        fs.partial_rounds += 1
        if fs.partial_rounds > self.MAX_PARTIAL_ROUNDS:
            fs.partial, fs.partial_rounds = "", 0
            return p, None
        fs.partial = combined
        self._log("phone_partial", heard=len(combined))
        return p, [Say(f"I have {parsers.speak_partial(combined)} so far. Please say the rest of the number.")]

    def _failed(self, q: Question, fs: FieldState, p: Parsed) -> list[Action]:
        fs.retries += 1
        self._log("invalid_answer", reason=p.reason, retries=fs.retries)
        if fs.retries > q.max_retries:
            if q.required:
                self._log("retry_limit")
                return self._escalate("retry_limit:" + q.id)
            fs.skipped = True
            return self._advance()
        return [Say(self._reprompt_text(q, p))]

    def _reprompt_text(self, q: Question, p: Parsed) -> str:
        if p.reason == "too_short" and q.type == "phone":
            return f"I only caught {p.detail} digits. Please say the whole number, or use your keypad."
        if p.reason == "too_long" and q.type == "phone":
            return "That was more digits than a phone number. Please say it again, area code first."
        if p.reason == "need_last_name":
            return "Thank you. I also need your last name."  # also in FIXED_LINES
        if p.reason == "in_the_future":
            return "That date has not happened yet. Roughly when did it take place?"
        if q.reprompt:
            return "Sorry, I did not catch that. " + q.reprompt
        return "Sorry, I did not catch that. " + q.prompt

    def _confirm_answer(self, p: Parsed, source: str) -> list[Action]:
        q = self.question
        fs = self.fields[q.id]
        if not p.ok or not isinstance(p.value, bool):
            self.unclear_confirms += 1
            if self.unclear_confirms > MAX_UNCLEAR_CONFIRMS:
                self.unclear_confirms = 0
                return self._rejected_confirm(q, fs)
            return [Say(f"Sorry, was that right? I have {parsers.readback(q, self.pending)}. Please say yes or no.")]
        self.unclear_confirms = 0
        if p.value is True:
            fs.value, fs.confirmed, fs.source = self.pending, True, fs.source or source
            self.pending = None
            return self._advance()
        if p.remainder:  # "no, it's Jordan Reyes": take the correction and read it back again
            corrected = parsers.parse_for(q, p.remainder, today=self.today, areas=self.cfg.practice_areas)
            if corrected.ok:
                self.pending = corrected.value
                self._log("caller_correction")
                return [Say(f"Thanks. I have {parsers.readback(q, corrected.value)}. Is that right?")]
        return self._rejected_confirm(q, fs)

    def _rejected_confirm(self, q: Question, fs: FieldState) -> list[Action]:
        fs.retries += 1
        self.pending = None
        self.state = State.ASK
        self._log("confirm_rejected", retries=fs.retries)
        if fs.retries > q.max_retries:
            if q.required:
                return self._escalate("retry_limit:" + q.id)
            fs.skipped = True
            return self._advance()
        return [Say("Sorry about that. " + q.prompt)]

    def _consent_answer(self, p: Parsed) -> list[Action]:
        if p.ok and p.value is True:
            self.recording = self.cfg.recording_enabled
            self.state = State.ASK
            self._log("recording_consent", granted=True)
            return [Say(self.question.prompt)]
        if p.ok and p.value is False:
            self.recording = False
            self.state = State.ASK
            self._log("recording_consent", granted=False)
            return [Say("That is fine, I will not record this call."), Say(self.question.prompt)]
        self.unclear_confirms += 1
        if self.unclear_confirms > MAX_UNCLEAR_CONFIRMS:
            self.recording = False  # no clear yes means no recording
            self.state = State.ASK
            self._log("recording_consent", granted=False, unclear=True)
            return [Say("I will not record this call."), Say(self.question.prompt)]
        return [Say("Sorry, is it all right if the firm records this call? Please say yes or no.")]

    def on_dtmf(self, digit: str) -> list[Action]:
        """A keypad digit. Zero is always a person; star repeats; digits fill a phone field; pound ends the entry."""
        if self._inactive:
            return []
        self._log("dtmf", digit="0" if digit == "0" else "x")  # a digit may be part of a phone number: never logged
        if digit == "0" and not (self.state == State.ASK and self.keypad):
            return self._escalate("dtmf_zero")
        if digit == "*":
            self.keypad = ""
            return self._reask()
        if self.state != State.ASK or self.question is None or not self.question.allow_dtmf:
            return []
        if digit == "#":
            digits, self.keypad = self.keypad, ""
            if not digits:
                return []
            return self.on_parsed(parsers.validate_phone_digits(digits), source="keypad")
        if digit.isdigit():
            self.keypad += digit
            # A US number is ten digits, and no area code starts with 1, so ten digits not starting with 1 are complete and a caller who forgets pound
            # is not left waiting; a number that starts with 1 is the eleven digit form.
            if len(self.keypad) == (11 if self.keypad.startswith("1") else 10):
                digits, self.keypad = self.keypad, ""
                return self.on_parsed(parsers.validate_phone_digits(digits), source="keypad")
        return []

    def on_silence_timeout(self) -> list[Action]:
        """The caller said nothing for the question's timeout."""
        if self._inactive:
            return []
        self.silences += 1
        self._log("silence", count=self.silences)
        if self.silences >= MAX_SILENCES:
            self.outcome = "abandoned_silent"
            self.tasks.append({"kind": "callback", "reason": "caller went silent mid-intake"})
            return self._finish([Say("I am not hearing anything, so I will end the call. Please ring again whenever you are ready.", "notice")],
                                "silence")
        q = self.question
        if self.state == State.CONSENT:
            return [Say("Is it all right if the firm records this call? Please say yes or no.")]
        if self.state == State.CONFIRM:
            return [Say(f"Are you still there? I have {parsers.readback(q, self.pending)}. Is that right?")]
        return [Say("Are you still there? " + q.prompt)]

    def on_transfer_result(self, ok: bool, detail: str = "") -> list[Action]:
        self.transfer.update({"result": "connected" if ok else "failed", "detail": detail})
        if ok:
            self.outcome = "transferred"
            self.state = State.DONE
            return []
        self.outcome = "callback_after_failed_transfer"
        self.tasks.append({"kind": "callback", "reason": f"transfer failed ({detail or 'no answer'}); caller wants a person"})
        return self._finish([Say(self.cfg.transfer.unavailable, "notice")], "transfer_failed")

    # ------------------------------------------------------------------ flow

    def _reask(self) -> list[Action]:
        if self.state == State.CONSENT:
            return [Say("Is it all right if the firm records this call?")]
        if self.state == State.CONFIRM:
            return [Say(f"I have {parsers.readback(self.question, self.pending)}. Is that right?")]
        return [Say(self.question.prompt)] if self.question else []

    def _advance(self) -> list[Action]:
        self.state = State.ASK
        self.index += 1
        self.keypad = ""
        if self.question is not None:
            return [Say(self.question.prompt)]
        return self._complete()

    def _decline_area(self) -> list[Action]:
        self.outcome = "declined_out_of_scope"
        self.tasks.append({"kind": "review_declined_caller", "reason": "matter is outside the practice areas"})
        self._log("area_unsupported")
        return self._finish([Say(self.cfg.guard.out_of_scope_message, "notice")], "out_of_scope")

    def _complete(self) -> list[Action]:
        self.qualification = self.evaluate_qualification()
        self.outcome = self.outcome or {"decline": "declined", "priority": "intake_priority", "needs_review": "intake_needs_review",
                                        "qualified": "intake_complete"}[self.qualification["result"]]
        self.tasks.append({"kind": "follow_up", "reason": "attorney review of the intake",
                           "priority": "high" if self.qualification["result"] == "priority" else "normal"})
        return self._finish([Say(self.cfg.closing, "closing")], "complete")

    def _escalate(self, reason: str) -> list[Action]:
        self.escalation = reason
        self._log("escalate", reason=reason)
        if self.cfg.transfer.enabled and self.in_hours:
            self.state = State.TRANSFERRING
            self.transfer = {"attempted": True, "reason": reason, "destination_alias": self.cfg.tenant_id + ":desk"}
            return [Say(self.cfg.transfer.announce, "notice"), StartTransfer(reason)]
        self.outcome = "callback_requested"
        self.tasks.append({"kind": "callback", "reason": f"caller needs a person ({reason}); office closed or transfer disabled"})
        text = self.cfg.after_hours_message if not self.in_hours else "I will make sure someone calls you back."
        return self._finish([Say(text, "notice")], "escalated_no_transfer")

    def _finish(self, says: list[Say], reason: str) -> list[Action]:
        self.state = State.DONE
        self.submitted = True
        return [*says, Submit(self.record()), Hangup(reason)]

    # ------------------------------------------------------------------ qualification and record

    def evaluate_qualification(self) -> dict:
        results = []
        worst = "qualified"
        order = {"qualified": 0, "needs_review": 1, "priority": 2, "decline": 3}
        for r in self.cfg.qualification:
            fs = self.fields[r.field]
            ok = self._rule_holds(r, fs)
            results.append({"rule": r.id, "passed": ok, "reason": "" if ok else r.reason})
            if not ok and order[r.if_fails] > order[worst]:
                worst = r.if_fails
        return {"result": worst, "rules": results}

    def _rule_holds(self, r, fs: FieldState) -> bool:
        v = fs.value
        if v is None:  # a field the caller never gave cannot satisfy a rule
            return False
        if r.op == "within_days":
            return (self.today - date.fromisoformat(str(v))).days <= int(r.value)
        if r.op == "in":
            return v in (r.value or [])
        if r.op == "not_in":
            return v not in (r.value or [])
        if r.op == "equals":
            return v == r.value
        if r.op == "is_true":
            return v is True
        if r.op == "is_false":
            return v is False
        return False

    def on_caller_hangup(self) -> None:
        """The caller's line went away before the machine ended the call (mid intake, or while a transfer was being set up): the call ends as
        `caller_hung_up` with a callback task, unless the intake was already handed on. A call that already has an outcome keeps it."""
        if self.state != State.DONE and not self.outcome:
            self.outcome = "caller_hung_up"
            if not self.submitted:
                self.tasks.append({"kind": "callback", "reason": "caller hung up before the intake finished"})

    def on_no_ack(self) -> None:
        """The line answered and the caller's ACK never came (RFC 3261 13.3.1.4): no session was set up and the intake never began. The call ends as
        `dropped_no_ack` with a callback task, so the firm has a trace of a call it answered."""
        if self.state != State.DONE and not self.outcome:
            self.outcome = "dropped_no_ack"
            self.state = State.DONE
            self._log("no_ack")
            self.tasks.append({"kind": "callback", "reason": "the call was answered but the caller's ACK never came, so no intake was taken; call this number back"})

    def idempotency_key(self) -> str:
        return hashlib.sha256(f"{self.cfg.tenant_id}|{self.call_id}|intake".encode()).hexdigest()

    def record(self) -> dict:
        return {
            **self.tenant.stamp,
            "call_id": self.call_id,
            "idempotency_key": self.idempotency_key(),
            "did": self.cfg.did,
            "caller_number": self.caller_number,
            "started_at": self.started_at.isoformat() + "Z",
            "in_hours": self.in_hours,
            "outcome": self.outcome or "in_progress",
            "escalation": self.escalation,
            "recording": self.recording,
            "fields": {k: {"value": v.value, "confirmed": v.confirmed, "source": v.source, "retries": v.retries, "skipped": v.skipped}
                       for k, v in self.fields.items()},
            "qualification": self.qualification,
            "tasks": self.tasks,
            "transfer": self.transfer,
            "events": self.events,
        }
