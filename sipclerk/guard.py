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
    r"\b911\b",
    r"\bemergency\b",
    r"\bin (?:immediate )?danger\b",
    r"\b(?:is|are) (?:badly |seriously )?(?:hurt|bleeding|dying|unconscious)\b",
    r"\b(?:unconscious|overdos\w*|bleeding (?:badly|heavily|out))\b",
    r"\bsuicid\w*|\bkill myself\b",
    r"\bnot breathing\b",
    r"\bbeing (?:attacked|followed|threatened)\b",
]

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
    if number and number in tenant.guard.blocked_callers:
        return InputVerdict("blocked_caller", "blocked_caller", number)
    return InputVerdict("allow")


def check_input(text: str, tenant: TenantConfig, *, check_scope: bool = False) -> InputVerdict:
    """Screen what the caller said. Called before the interpreter or any model sees the text."""
    t = _norm(text)
    if hit := _match(EMERGENCY, t):
        return InputVerdict("emergency", "emergency", hit)
    if hit := _match(EXISTING_CLIENT, t):
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
