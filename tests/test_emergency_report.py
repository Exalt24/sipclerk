"""The emergency rule fires on a danger happening now, not on an injury being reported and not on a legal use of the word.

A second review found the same class twice more. `my back is hurt` and `my kids are hurt too` are how an injury call goes, and `an emergency hearing`,
`an emergency protective order` and `it is kind of an emergency` are how a family law or immigration call goes: each got the 911 line and a transfer, and
after hours the intake was cut short. The guard stays keyword rules (README, Limits); these tables pin both directions, a phrasing that must pass and the
same words said as a real alarm that must still fire. The 19 live and 30 narrative phrasings in test_emergency_tense.py stay green beside them."""
import itertools
import random
import re

import pytest

from sipclerk import guard
from test_emergency_tense import LIVE, NARRATIVE

# What a caller says while reporting an injury or a deadline: none of it is a danger happening now.
REPORTS = [
    # a body part that is hurt, or a person reported hurt with nothing asking for help right then
    "I was rear ended on the freeway last week and my back is hurt",
    "my neck is hurt from the car accident in March",
    "my knees are badly hurt since the fall in august",
    "my shoulder and my wrist are hurt and I cannot lift anything at work",
    "my son is hurt, he broke his arm when he fell at the store two weeks ago",
    "my kids are hurt too",
    "I was in the car with my wife and she is hurt too, we want to know about the other driver's insurance",
    # the word emergency inside a court filing, a hearing or a request that names a court remedy
    "we have an emergency hearing next week about visitation",
    "my lawyer filed an emergency motion for custody",
    "I need to file for an emergency protective order in my divorce",
    "I need an emergency custody order, my ex took the kids out of state",
    "the court gave him emergency temporary custody last month",
    "we are asking for an emergency stay of the eviction",
    "the judge set an emergency session for Monday",
    "I need an emergency consultation about a restraining order",
    # the word emergency after a hedge: the caller says it is below an alarm
    "my visa expires friday so it is kind of an emergency",
    "it is sort of an emergency because my lease ends tomorrow",
    "this is a bit of an emergency, the court date is in two days",
    # a legal use of a condition word
    "I am in danger of losing my house if I miss the hearing",
    "my business is in danger of bankruptcy",
    "we are in danger of being evicted on Friday",
    "my landlord keeps threatening me and I am being threatened with eviction",
    "a collection agency is threatening me, I am being threatened with a lawsuit",
]

# The same words said as an alarm: every one of them must still be an emergency.
ALARMS = [
    "my husband is bleeding badly and won't wake up",
    "he is not breathing",
    "my husband is badly hurt",
    "she is seriously hurt",
    "my son is hurt, please help",
    "my daughter is hurt right now and she is not moving",
    "he fell off the ladder and is hurt, send an ambulance",
    "my back is broken and I am bleeding badly",
    "this is an emergency",
    "I have an emergency, please help",
    "it's kind of an emergency, my wife is not breathing",
    "I need an emergency hearing, she is unconscious",
    "I am in danger",
    "I am in danger of being killed by my husband",
    "I am being threatened with a gun",
    "I am being threatened right now",
    "I am in danger of losing my arm, the bleeding will not stop and he is bleeding badly",
]


@pytest.mark.parametrize("said", REPORTS)
def test_an_injury_or_a_deadline_being_reported_is_not_an_emergency(ashgrove, said):
    v = guard.check_input(said, ashgrove.config)
    assert v.action == "allow", f"{said!r} fired the {v.rule} rule on {v.matched!r}"


@pytest.mark.parametrize("said", ALARMS)
def test_the_same_words_said_as_an_alarm_are_still_an_emergency(ashgrove, said):
    assert guard.check_input(said, ashgrove.config).action == "emergency", said


def test_the_pinned_live_and_narrative_sets_have_not_moved(ashgrove):
    assert len(LIVE) == 19 and len(NARRATIVE) == 30
    assert all(guard.check_input(t, ashgrove.config).action == "emergency" for t in LIVE)
    assert all(guard.check_input(t, ashgrove.config).action == "allow" for t in NARRATIVE)
    assert not set(REPORTS) & set(ALARMS)


BODY_PARTS = ["back", "neck", "knee", "knees", "shoulder", "arm", "arms", "leg", "hip", "ankle", "wrist", "hand", "head", "elbow", "spine", "ribs", "chest", "jaw"]
PEOPLE = ["my husband", "my wife", "my son", "my daughter", "he", "she", "they", "my mother", "the passenger", "my kids"]
LEGAL_NOUNS = ["hearing", "hearings", "motion", "order", "petition", "filing", "application", "injunction", "relief", "appeal", "stay", "custody", "guardianship",
               "protective order", "restraining order", "temporary custody", "visitation", "divorce", "bail", "court", "session", "extension", "relocation", "removal",
               "proceeding", "consultation", "appointment", "meeting"]
HEDGES = ["kind of", "sort of", "a bit of", "a little bit of", "somewhat of"]


def test_class_a_body_part_that_is_hurt_is_never_an_alarm_and_a_person_who_is_badly_hurt_always_is(ashgrove):
    for part, verb in itertools.product(BODY_PARTS, ["is hurt", "is badly hurt", "are hurt", "are seriously hurt"]):
        said = f"since the crash my {part} {verb}"
        assert guard.check_input(said, ashgrove.config).action == "allow", said
    for who, how in itertools.product(PEOPLE, ["badly", "seriously"]):
        said = f"{who} {'are' if who == 'my kids' else 'is'} {how} hurt"
        assert guard.check_input(said, ashgrove.config).action == "emergency", said
    for who in PEOPLE:
        verb = "are" if who == "my kids" else "is"
        assert guard.check_input(f"{who} {verb} hurt", ashgrove.config).action == "allow", who
        for ask in ("please help", "right now", "send an ambulance", "won't wake up"):
            assert guard.check_input(f"{who} {verb} hurt, {ask}", ashgrove.config).action == "emergency", (who, ask)


def test_class_the_word_emergency_before_a_legal_noun_or_after_a_hedge_is_not_an_alarm_and_alone_it_is(ashgrove):
    for noun in LEGAL_NOUNS:
        assert guard.check_input(f"I need an emergency {noun} this week", ashgrove.config).action == "allow", noun
        assert guard.check_input(f"this is an emergency, I need a {noun}", ashgrove.config).action == "emergency", noun
    for hedge in HEDGES:
        assert guard.check_input(f"it is {hedge} an emergency because my visa ends friday", ashgrove.config).action == "allow", hedge
        assert guard.check_input(f"it is an emergency, my visa ends friday", ashgrove.config).action == "emergency"


PREVIOUS_EMERGENCY = [
    r"\b(?:call|calling|dial|dialing|phone)\b(?: (?:now|me|us|them))? 911\b",
    r"\b911\b(?= (?:please|now|help|right now|immediately|asap)\b)|^\W*911\W*$",
    r"\bemergency\b(?! (?:rooms?|departments?|dept|ward|surgery|surgeries|contacts?|care|services?|vehicles?|personnel|crews?|responders?|medical|"
    r"technicians?|treatment|visits?|doctors?|physicians?|staff|lane|brakes?|exits?|lights?|stop|fund|calls?|bills?|transport|helicopter|workers?)\b)",
    r"\bin (?:immediate )?danger\b",
    r"\b(?:is|are) (?:badly |seriously )?(?:hurt|bleeding|dying|unconscious)\b",
    r"\b(?:unconscious|overdos\w*|bleeding (?:badly|heavily|out))\b",
    r"\bsuicid\w*|\bkill myself\b",
    r"\bnot breathing\b",
    r"\bbeing (?:attacked|followed|threatened)\b",
]
WORDS = ["I", "he", "she", "was", "is", "are", "am", "my", "back", "neck", "kids", "son", "hurt", "badly", "seriously", "bleeding", "unconscious", "emergency",
         "hearing", "motion", "order", "protective", "custody", "court", "kind", "of", "an", "a", "sort", "bit", "in", "danger", "being", "threatened", "with",
         "lawsuit", "eviction", "losing", "house", "please", "help", "now", "right", "and", "ago", "call", "911", "not", "breathing", "the", "visa", "room", "too"]


def previous_rule_fires(text: str) -> bool:
    """The rule as it was before this change: the old patterns with the same account test."""
    t = guard._norm(text)
    return any(not guard._is_account(t, m) for p in PREVIOUS_EMERGENCY for m in re.finditer(p, t))


def test_class_the_rule_fires_on_strictly_fewer_inputs_than_before_except_the_live_phrasings(ashgrove):
    """6,000 seeded random utterances plus every pinned phrasing: nothing fires now that did not fire before, and some inputs that did are let through."""
    rnd = random.Random(26)
    before = after = dropped = 0
    texts = [" ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 9))) for _ in range(6000)] + REPORTS + ALARMS + NARRATIVE + LIVE
    for text in texts:
        was, now = previous_rule_fires(text), guard.check_input(text, ashgrove.config).action == "emergency"
        before, after = before + was, after + now
        assert was or not now, f"{text!r} fires now and did not before"
        dropped += was and not now
        if text in LIVE or text in ALARMS:
            assert now, text
    assert after < before and dropped > 50, (before, after, dropped)
