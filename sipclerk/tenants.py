"""Tenant configuration: one versioned file per firm, validated against a schema, loaded by dialled number.

Everything a firm can change lives here (practice areas, intake questions, disclosures, qualification
rules, transfer destination, hours, language). Adding a firm is adding a file, not changing code.
The SHA-256 of the file's canonical form and its version are stamped on every call record, so a call
can be replayed against exactly the configuration that handled it.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sipclerk.sip.message import normalize_did

SCHEMA_VERSION = 1
FieldType = Literal["name", "phone", "email", "date", "enum", "yesno", "text"]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Disclosure(Strict):
    id: str
    text: str = Field(min_length=10)
    ask_consent: bool = False  # the caller must say yes before we continue (all-party consent states)


class PracticeArea(Strict):
    id: str
    label: str
    keywords: list[str] = Field(min_length=1)


class Question(Strict):
    id: str
    type: FieldType
    prompt: str = Field(min_length=5)
    required: bool = True
    confirm: bool = False  # read the value back and wait for a yes
    max_retries: int = Field(default=2, ge=0, le=5)
    timeout_s: float = Field(default=8.0, gt=0)
    options: list[str] = Field(default_factory=list)  # for enum questions that are not the practice area
    min_words: int = Field(default=1, ge=1)
    max_age_days: int | None = None  # date questions: reject a date older than this
    allow_dtmf: bool = False
    reprompt: str = ""
    escalate_on: Literal["yes", "no"] | None = None  # a yes/no answer that hands the caller to a person
    endpointing_s: float | None = Field(default=None, gt=0.2, le=3.0)  # silence that ends the caller's turn; None means the default for the type

    @model_validator(mode="after")
    def _enum_has_options(self) -> Question:
        if self.type == "enum" and self.id != "practice_area" and not self.options:
            raise ValueError(f"enum question {self.id} needs options")
        return self


class Rule(Strict):
    id: str
    field: str
    op: Literal["within_days", "in", "not_in", "equals", "is_true", "is_false"]
    value: int | str | list[str] | None = None
    if_fails: Literal["needs_review", "decline", "priority"] = "needs_review"
    reason: str


class Hours(Strict):
    timezone: str = "UTC"
    utc_offset_hours: float = 0.0  # fixed offset: keeps the lab free of tzdata and makes tests exact
    weekly: dict[str, tuple[str, str]]

    @field_validator("weekly")
    @classmethod
    def _days_and_times(cls, v: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
        for day, (a, b) in v.items():
            if day not in DAYS:
                raise ValueError(f"unknown day {day}")
            if not (re.fullmatch(r"\d\d:\d\d", a) and re.fullmatch(r"\d\d:\d\d", b)) or a >= b:
                raise ValueError(f"bad hours for {day}: {a}-{b}")
        return v


class Transfer(Strict):
    enabled: bool = True
    destination: str  # a SIP URI of the firm's desk phone or PBX
    ring_timeout_s: float = Field(default=15.0, gt=0)
    announce: str = Field(min_length=5)  # spoken to the caller while we connect
    whisper: str = Field(min_length=5)  # spoken to staff before the bridge opens ({summary} is filled in)
    unavailable: str = Field(min_length=5)  # said to the caller when nobody picks up


class Guard(Strict):
    legal_advice_repeat_escalates: bool = True
    blocked_callers: list[str] = Field(default_factory=list)
    out_of_scope_keywords: list[str] = Field(default_factory=list)
    out_of_scope_message: str = Field(min_length=10)


class Jurisdiction(Strict):
    code: str
    recording: Literal["notice", "all_party_consent"] = "notice"


class Integrations(Strict):
    case_management: bool = True
    call_console: bool = True


class TenantConfig(Strict):
    schema_version: int
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,30}$")
    version: str
    display_name: str
    did: str
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    voice: str = "af_heart"
    jurisdiction: Jurisdiction
    hours: Hours
    after_hours_message: str = Field(min_length=10)
    disclosures: list[Disclosure] = Field(min_length=1)
    greeting: str = Field(min_length=10)
    practice_areas: list[PracticeArea] = Field(min_length=1)
    questions: list[Question] = Field(min_length=1)
    qualification: list[Rule] = Field(default_factory=list)
    transfer: Transfer
    guard: Guard
    closing: str = Field(min_length=10)
    retention_days: int = Field(default=30, ge=1, le=3650)
    recording_enabled: bool = True
    staff_roles: list[str] = Field(default_factory=lambda: ["intake_staff"])  # may read record, transcript, summary
    recording_roles: list[str] = Field(default_factory=lambda: ["attorney"])  # the only roles that may play a recording
    integrations: Integrations = Field(default_factory=Integrations)

    @field_validator("did")
    @classmethod
    def _did(cls, v: str) -> str:
        n = normalize_did(v)
        if not re.fullmatch(r"\+\d{8,15}", n):
            raise ValueError("did must be an E.164 number")
        return n

    @model_validator(mode="after")
    def _consistent(self) -> TenantConfig:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate question id")
        for r in self.qualification:
            if r.field not in ids:
                raise ValueError(f"rule {r.id} reads unknown field {r.field}")
        if "practice_area" not in ids:
            raise ValueError("every tenant needs a practice_area question")
        return self

    # ---- helpers the rest of the code uses

    def question(self, qid: str) -> Question:
        return next(q for q in self.questions if q.id == qid)

    def area(self, area_id: str) -> PracticeArea | None:
        return next((a for a in self.practice_areas if a.id == area_id), None)

    def is_open(self, utc_now) -> bool:
        local = utc_now.replace(tzinfo=None) + _hours(self.hours.utc_offset_hours)
        window = self.hours.weekly.get(DAYS[local.weekday()])
        if not window:
            return False
        start, end = (time.fromisoformat(w) for w in window)
        return start <= local.time() < end


def _hours(h: float):
    from datetime import timedelta

    return timedelta(hours=h)


def canonical_hash(raw: dict) -> str:
    """Hash of the config's meaning, not its formatting: sorted keys, no whitespace."""
    blob = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class LoadedTenant:
    def __init__(self, config: TenantConfig, config_hash: str, source: Path):
        self.config = config
        self.config_hash = config_hash
        self.source = source

    @property
    def stamp(self) -> dict:
        return {"tenant_id": self.config.tenant_id, "config_version": self.config.version, "config_hash": self.config_hash}


class TenantError(ValueError):
    pass


def load_file(path: Path) -> LoadedTenant:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    try:
        cfg = TenantConfig.model_validate(raw)
    except ValidationError as e:
        raise TenantError(f"{path.name}: {e.error_count()} problem(s): " + "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5])) from e
    return LoadedTenant(cfg, canonical_hash(raw), Path(path))


class TenantRegistry:
    """Maps a dialled number to a tenant. A DID or a tenant id listed twice is a startup error."""

    def __init__(self, tenants: list[LoadedTenant]):
        self._by_did: dict[str, LoadedTenant] = {}
        self._by_id: dict[str, LoadedTenant] = {}
        for t in tenants:
            if t.config.did in self._by_did:
                raise TenantError(f"two tenants answer {t.config.did}")
            if t.config.tenant_id in self._by_id:
                raise TenantError(f"tenant id {t.config.tenant_id} loaded twice")
            self._by_did[t.config.did] = t
            self._by_id[t.config.tenant_id] = t

    @classmethod
    def from_dir(cls, directory: Path) -> TenantRegistry:
        files = sorted(Path(directory).glob("*.yaml"))
        if not files:
            raise TenantError(f"no tenant files in {directory}")
        return cls([load_file(f) for f in files])

    def by_did(self, did: str) -> LoadedTenant | None:
        return self._by_did.get(normalize_did(did))

    def resolve(self, candidates: list[str]) -> LoadedTenant | None:
        """The firm a call is for, from what the caller dialled (in the order they are trusted), or None.

        A candidate is accepted only on an exact match of its normalised digits with a firm's number. A national number (ten digits, no country code) is
        accepted only when exactly one firm's number ends with it, so a call is never loaded onto a firm that merely looks similar."""
        for c in candidates:
            if c.startswith("+"):
                t = self._by_did.get(c)
            elif re.fullmatch(r"\d{10}", c):
                hits = [t for did, t in self._by_did.items() if did.endswith(c)]
                t = hits[0] if len(hits) == 1 else None
            else:
                t = None
            if t is not None:
                return t
        return None

    def by_id(self, tenant_id: str) -> LoadedTenant | None:
        return self._by_id.get(tenant_id)

    @property
    def all(self) -> list[LoadedTenant]:
        return list(self._by_id.values())


def export_json_schema(path: Path) -> None:
    Path(path).write_text(json.dumps(TenantConfig.model_json_schema(), indent=2) + "\n", encoding="utf-8")
