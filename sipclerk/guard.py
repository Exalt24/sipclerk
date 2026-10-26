"""The safety guard, in code, on both sides of the model.

What a caller says is screened BEFORE any model sees it (legal advice requests, existing clients, a
request for a person, an emergency, a practice area the firm does not take). What the agent is about
to say is screened AFTER the model and before the speech engine (promised outcomes, legal advice, an
implied attorney-client relationship, a commitment nobody made). A blocked sentence is replaced by a
fixed safe one, so a model failure cannot reach the caller.

Each rule is a named list of patterns so a mutation check can delete one rule at a time and a test
must notice. The patterns are deliberately plain: a lab cannot claim a classifier it does not have,
and a keyword rule is easy to audit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sipclerk.sip import message as sm
from sipclerk.tenants import TenantConfig

# --------------------------------------------------------------------------- caller side (before the model)

LEGAL_ADVICE_REQUEST = [
    r"\bdo i have a (good |strong |real )?(case|claim)\b",
    r"\bshould i (sue|file|settle|accept|sign|take|press|talk to|hire|get a lawyer|go to court)\b",
    r"\bcan i sue\b",
    r"\bwill i (win|lose|get)\b",
    r"\bwhat (are|is) my (chances|odds|rights|options)\b",
    r"\bhow much (is|could|can|would|will) (my|this|the) (case|claim)\b.*\b(worth|get)\b",
    r"\bwhat should i do\b",
    r"\bwhat would you do\b",
    r"\bam i entitled\b",
    r"\bis (this|that|it) (legal|illegal|against the law)\b",
    r"\bstatute of limitations\b",
    r"\bhow long do i have to (file|sue)\b",
    r"\bwhat do you think (my|about my) (case|chances)\b",
]
EXISTING_CLIENT = [
    r"\bi(?:'m| am) (?:already |currently )?(?:a |one of your )?client\b",
    r"\b(?:my|our) (?:lawyer|attorney|paralegal|case manager) (?:is|at|there)\b",
    r"\bmy (?:existing|current|open) (?:case|matter|file)\b",
    r"\bcalling (?:about|for|regarding) my (?:case|matter|file)\b",
    r"\bmy case number\b",
    r"\bi(?:'m| am) (?:already )?represented by (?:you|your firm)\b",
]
HUMAN_REQUEST = [
    r"\b(?:talk|speak) (?:to|with) (?:a |an |the )?(?:real )?(?:person|human|someone|representative|attorney|lawyer|receptionist|operator|staff|somebody)\b",
    r"\b(?:real|live) (?:person|human)\b",
    r"\boperator\b",
    r"\bget me (?:a |an )?(?:person|human|attorney|lawyer|representative)\b",
    r"\bi (?:want|need) (?:a |an )?(?:person|human)\b",
]
EMERGENCY = [
    r"\b(?:call|calling|dial|dialing|phone)\b(?: (?:now|me|us|them))? 911\b",
    r"\b911\b(?= (?:please|now|help|right now|immediately|asap)\b)|^\W*911\W*$",
    r"\bemergency\b(?! (?:rooms?|departments?|dept|ward|surgery|surgeries|contacts?|care|services?|vehicles?|personnel|crews?|responders?|medical|"
    r"technicians?|treatment|visits?|doctors?|physicians?|staff|lane|brakes?|exits?|lights?|stop|fund|calls?|bills?|transport|helicopter|workers?|"
    r"hearings?|motions?|orders?|petitions?|filings?|applications?|injunctions?|relief|appeals?|stay|custody|guardianship|protective|restraining|temporary|"
    r"ex parte|visitation|divorce|bail|court|session|extension|relocation|removal|proceedings?|consultations?|appointments?|meetings?)\b)",
    r"\bin (?:immediate )?danger\b(?! of (?:losing (?:my|our|his|her|their|the) (?:home|house|job|visa|custody|business|license|apartment|rights|status|car|"
    r"benefits|insurance|case|claim)|being (?:evicted|deported|fired|sued|arrested)|eviction|foreclosure|deportation|bankruptcy|defaulting)\b)",
    r"\b(?:is|are|am|['’]s|['’]re|['’]m) (?:badly |seriously )?(?:bleeding|dying|unconscious)\b",
    r"\b(?:is|are|am|['’]s|['’]re|['’]m) (?:badly |seriously )?hurt\b",
    r"\b(?:unconscious|overdos\w*|bleeding (?:badly|heavily|out))\b",
    r"\bsuicid\w*|\bkill myself\b",
    r"\bnot breathing\b",
    r"\bbeing (?:attacked|followed)\b",
    r"\bbeing threatened\b(?! with (?:an? )?(?:lawsuit|suit|eviction|foreclosure|deportation|termination|arrest|prosecution|legal|collections?|repossession|"
    r"garnishment|jail|losing|being (?:fired|evicted|sued|deported|arrested|reported))\b)",
]
# An emergency is a danger that is happening now. A keyword hit inside an account of something that happened (`I was unconscious for a minute after the
# crash`, `the paramedics said she was not breathing when they arrived`) is an ordinary injury story, so a hit is dropped when its clause is in the past
# and nothing right before it says it is now. The first rule fired on `taken to the emergency room`, which is how most injury calls begin.
_CLAUSE_BREAKS = (".", "!", "?", ";", " and ", " but ", " then ", " so ", " because ")
_PAST = re.compile(r"\b(?:was|were|wasn't|weren't|had|been|ago|yesterday|last (?:night|week|month|year)|when|after|while|until|since|before|rushed|taken|took|went|woke|arrived)\b")
_SUICIDE_PAST = re.compile(r"\b(?:died by|death by|death from|committed|completed|took (?:his|her|their|my) own life|ago|last (?:night|week|month|year))\b")
_NOW_BEFORE_HIT = re.compile(r"(?:\b(?:is|are|am|isn't|aren't)|'s|'re|'m|\bjust|\bnow|\bstill)\s+(?:(?:not|badly|seriously|really|completely|currently|an?|the|medical|so|very)\s+)*$")
# An injury REPORTED is not a danger now (`my back is hurt`, `my kids are hurt too` are how an injury call goes). `hurt` alone is an alarm only for a person
# who is badly or seriously hurt, or when the caller asks for help right then; a body part that is hurt is never an alarm. `emergency` after a hedge
# (`kind of an emergency`) says the caller is below an alarm.
_BODY_PART_BEFORE = re.compile(r"\b(?:back|neck|knees?|shoulders?|arms?|legs?|hips?|ankles?|wrists?|hands?|feet|foot|head|elbows?|spine|ribs?|chest|jaw|face|eyes?|"
                               r"ears?|fingers?|toes?|body|stomach|hip|thighs?|calf|calves)\s*$")
_HELP_NOW = re.compile(r"\b(?:right now|hurry|asap|immediately|please help|send (?:help|someone|an ambulance)|won't wake|not waking|not responding|not moving|can't move)\b")
_HEDGE_BEFORE = re.compile(r"\b(?:(?:kind|sort) of|(?:a )?(?:bit|little) of|somewhat of)\s*(?:an?\s+)?$")

# --------------------------------------------------------------------------- agent side (after the model)

PROMISED_OUTCOME = [
    r"\byou(?:'ll| will| are going to| should)\s+(?:definitely |certainly |surely )?(?:win|recover|be compensated|get (?:paid|compensated|a settlement|money))",
    r"\bguarantee",
    r"\byou have a (?:strong|good|solid|great|winning) (?:case|claim)\b",
    r"\b(?:we|the firm|an attorney) will (?:win|get you|recover)\b",
    r"\byour (?:case|claim) is (?:worth|strong|solid)\b",
    r"\bsettlement of\b",
    r"\bno[- ]brainer\b",
]
LEGAL_ADVICE_OUT = [
    r"\byou should (?:sue|file|settle|accept|not sign|sign|hire|press charges|take legal action)\b",
    r"\bi (?:advise|recommend|suggest) (?:that )?you\b",
    r"\blegally(?: speaking)?,? you\b",
    r"\bthe statute of limitations (?:is|expires|has)\b",
    r"\byou (?:have|do not have|don't have) (?:a|no) (?:legal )?(?:claim|case|right)\b",
    r"\byou(?:'re| are) (?:legally )?entitled\b",
    r"\bit(?:'s| is) (?:illegal|legal|unlawful)\b",
]
ATTORNEY_CLIENT = [
    r"\byou(?:'re| are) (?:now |already )?(?:a|our) client\b",
    r"\bwe (?:will|'ll) represent you\b",
    r"\b(?:our firm|the firm|we) represents? you\b",
    r"\battorney[- ]client relationship (?:has|is|was) (?:been )?(?:formed|established|created|started)\b",
    r"\byour (?:lawyer|attorney) (?:will be|is)\b",
    r"\bwe(?:'ve| have) (?:taken|accepted) (?:on )?your (?:case|matter)\b",
]
UNMADE_COMMITMENT = [
    r"\b(?:i|we)(?:'ve| have)? (?:booked|scheduled|confirmed|reserved) (?:your|a) (?:consultation|appointment|meeting)\b",
    r"\bsee you (?:on|at) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d)",
]

SAFE_REPLACEMENT = "I want to be careful here. I cannot give legal advice. An attorney will review your details."

INPUT_RULES = {
    "emergency": EMERGENCY,
    "existing_client": EXISTING_CLIENT,
    "human_request": HUMAN_REQUEST,
    "legal_advice_request": LEGAL_ADVICE_REQUEST,
}
OUTPUT_RULES = {
    "promised_outcome": PROMISED_OUTCOME,
    "legal_advice_out": LEGAL_ADVICE_OUT,
    "attorney_client_implied": ATTORNEY_CLIENT,
    "unmade_commitment": UNMADE_COMMITMENT,
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip()


def _match(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0)
    return None


@dataclass(frozen=True)
class InputVerdict:
    action: str  # "allow", "deflect", "escalate", "emergency", "out_of_scope", "blocked_caller"
    rule: str = ""
    matched: str = ""


def check_caller(number: str, tenant: TenantConfig) -> InputVerdict:
    """Spam and known-bad numbers are refused before the call is answered."""
    n = sm.caller_number(number)
    if n and sm.match_number(n, [sm.caller_number(b) for b in tenant.guard.blocked_callers]):
        return InputVerdict("blocked_caller", "blocked_caller", n)
    return InputVerdict("allow")


def _is_account(text: str, m: re.Match) -> bool:
    """True when the keyword `m` found sits in a clause that tells what happened, with no sign in front of it that it is happening now."""
    if re.match(r"(?:is|are)\b", m.group(0)):  # `is bleeding`: the pattern itself says now
        return False
    left = max((text.rfind(b, 0, m.start()) + len(b) for b in _CLAUSE_BREAKS if text.rfind(b, 0, m.start()) != -1), default=0)
    right = min((i for b in _CLAUSE_BREAKS if (i := text.find(b, m.end())) != -1), default=len(text))
    if _NOW_BEFORE_HIT.search(text[left:m.start()]):
        return False
    return bool((_SUICIDE_PAST if "suicid" in m.re.pattern else _PAST).search(text[left:right]))


def _is_report(text: str, m: re.Match) -> bool:
    """True when the hit `m` is an injury or an urgency being reported, not a danger happening now (see _BODY_PART_BEFORE)."""
    hit = m.group(0)
    if hit == "emergency":
        return bool(_HEDGE_BEFORE.search(text[:m.start()]))
    if hit.endswith("hurt"):
        if _BODY_PART_BEFORE.search(text[:m.start()]):
            return True
        return not re.search(r"(?:badly|seriously) hurt$", hit) and not _HELP_NOW.search(text)
    return False


def _emergency_now(text: str) -> str | None:
    """The first emergency keyword that is not part of an account of the past or a report of an injury, or None."""
    for p in EMERGENCY:
        for m in re.finditer(p, text):
            if not _is_account(text, m) and not _is_report(text, m):
                return m.group(0)
    return None


def check_input(text: str, tenant: TenantConfig, *, check_scope: bool = False, not_a_client: bool = False) -> InputVerdict:
    """Screen what the caller said. Called before the interpreter or any model sees the text.

    `not_a_client` is set once the caller has answered no to "are you already a client": `my case`, `my lawyer is` and the rest then describe the matter
    they are calling about, not a relationship with this firm, so the existing client rule is not run."""
    t = _norm(text)
    if hit := _emergency_now(t):
        return InputVerdict("emergency", "emergency", hit)
    if not not_a_client and (hit := _match(EXISTING_CLIENT, t)):
        return InputVerdict("escalate", "existing_client", hit)
    if hit := _match(HUMAN_REQUEST, t):
        return InputVerdict("escalate", "human_request", hit)
    if hit := _match(LEGAL_ADVICE_REQUEST, t):
        return InputVerdict("deflect", "legal_advice_request", hit)
    if check_scope:
        in_scope = any(_hit_words(t, a.keywords) for a in tenant.practice_areas)
        oos = _hit_words(t, tenant.guard.out_of_scope_keywords)
        if oos and not in_scope:
            return InputVerdict("out_of_scope", "out_of_scope_area", oos)
    return InputVerdict("allow")


def _hit_words(text: str, words: list[str]) -> str:
    for w in words:
        if re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", text):
            return w
    return ""


@dataclass(frozen=True)
class OutputVerdict:
    ok: bool
    rules: tuple[str, ...] = ()
    text: str = ""  # what may be spoken: the original, or the safe replacement


def check_output(text: str) -> OutputVerdict:
    """Screen what the agent is about to say. Nothing reaches the speech engine without passing this."""
    t = _norm(text)
    hits = tuple(name for name, pats in OUTPUT_RULES.items() if _match(pats, t))
    if hits:
        return OutputVerdict(False, hits, SAFE_REPLACEMENT)
    return OutputVerdict(True, (), text)
