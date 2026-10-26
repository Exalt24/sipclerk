"""The emergency rule fires on a danger that is happening now, not on an account of one that happened.

An injury caller says `a truck hit me and I was taken to the emergency room`, `I called 911 at the scene`, `I was unconscious for a minute`: all of
them are the ordinary start of an injury intake, and the first rule hung up on them with the 911 line and a transfer. The same class as the earlier
`I was hurt in a crash`. The guard stays keyword rules (README, Limits): these tests pin the phrasings, they do not claim a classifier."""
import itertools

import pytest

from conftest import IN_HOURS
from sipclerk import guard
from sipclerk.intake.machine import IntakeMachine, StartTransfer, Say

# 30 ways a caller describes something that already happened (invented). None of them may be taken for an emergency.
NARRATIVE = [
    "A truck hit me and I was taken to the emergency room",
    "I called 911 at the scene and an ambulance came",
    "I went to the emergency room right after the accident",
    "The emergency room doctor said I had a concussion",
    "I spent three nights in the hospital after the emergency surgery",
    "I was unconscious for a few minutes after the crash",
    "My husband was unconscious when the paramedics arrived",
    "She was bleeding badly from her knee after she slipped at the store",
    "I was bleeding heavily and a bystander called 911",
    "Someone at the scene called 911 for me",
    "The 911 dispatcher sent an ambulance within minutes",
    "I was in danger when the other driver crossed the center line",
    "We were in immediate danger when the scaffolding came down",
    "The paramedics said she was not breathing when they got there",
    "I was being followed by a truck for a mile before it hit me",
    "My daughter was being attacked by the neighbor's dog when I got outside",
    "I called 911 and the police wrote a report",
    "After the accident I was rushed to the emergency department",
    "My brother died by suicide last year and we want to understand what his employer did",
    "My mother overdosed on pills the pharmacy gave her, two years ago",
    "The ambulance and other emergency vehicles blocked the road for an hour",
    "My emergency contact on the form was my sister",
    "I have been in and out of the emergency room for six months since the crash",
    "The 911 call was recorded and the dispatcher gave the wrong address",
    "He was unconscious at the scene and was airlifted to the trauma center",
    "I was bleeding out on the road until a nurse stopped to help",
    "The emergency room bill was eleven thousand dollars and the insurer denied it",
    "I woke up in the emergency room with a broken wrist after the crash",
    "My husband overdosed last year after the surgery and the hospital never warned us",
    "I was in danger of losing my house after the injury",
]

# 15 ways a caller says it is happening now (invented). Each of them must still be an emergency.
LIVE = [
    "this is an emergency",
    "it's a medical emergency",
    "I have an emergency, please help",
    "call 911",
    "please call 911 right now",
    "I'm calling 911 now",
    "I am in danger",
    "we are in immediate danger",
    "he is bleeding",
    "my husband is bleeding badly",
    "she is unconscious",
    "my son just overdosed, please help",
    "I want to kill myself",
    "I'm thinking about suicide",
    "she's not breathing",
    "my husband is unconscious after he fell on the stairs",
    "I think he's unconscious since he fell off the ladder",
    "I'm being attacked right now",
    "he collapsed and he is not breathing",
]


@pytest.mark.parametrize("text", NARRATIVE)
def test_an_account_of_something_that_happened_is_not_an_emergency(ashgrove, text):
    v = guard.check_input(text, ashgrove.config)
    assert v.action == "allow", f"{text!r} fired the {v.rule} rule on {v.matched!r}"


@pytest.mark.parametrize("text", LIVE)
def test_a_danger_happening_now_still_is(ashgrove, text):
    assert guard.check_input(text, ashgrove.config).action == "emergency", text


def test_the_two_sets_are_the_size_the_class_test_promised():
    assert len(NARRATIVE) == 30 and len(LIVE) == 19 and not set(NARRATIVE) & set(LIVE)


SUBJECTS_PAST = ["I was", "he was", "she was", "they were", "my husband was", "the passenger was"]
SUBJECTS_NOW = ["I am", "I'm", "he is", "he's", "she's", "they are", "my husband is", "my mother is"]
CONDITIONS = ["unconscious", "bleeding badly", "bleeding heavily", "not breathing", "in danger", "in immediate danger", "being followed", "being attacked", "being threatened"]


# CLASS-TABLE: the emergency rule firing on an account of the past, for every condition word with every past and present subject and every tail
def test_class_the_same_condition_in_the_past_is_an_account_and_in_the_present_is_an_emergency(ashgrove):
    """Every condition the rule knows, said with a past subject and with a present one, with and without a trailing account."""
    for cond, tails in itertools.product(CONDITIONS, ["", " at the scene", " when the paramedics arrived", " after the crash"]):
        for subject in SUBJECTS_PAST:
            text = f"{subject} {cond}{tails}"
            assert guard.check_input(text, ashgrove.config).action == "allow", text
        for subject in SUBJECTS_NOW:
            text = f"{subject} {cond}{tails}"
            assert guard.check_input(text, ashgrove.config).action == "emergency", text


@pytest.mark.parametrize("noun", ["room", "rooms", "department", "surgery", "contact", "services", "vehicles", "personnel", "lane", "visit", "doctor", "bill"])
def test_emergency_as_a_place_or_a_thing_is_not_the_word_said_as_an_alarm(ashgrove, noun):
    assert guard.check_input(f"I went to the emergency {noun} on Tuesday", ashgrove.config).action == "allow"
    assert guard.check_input("it is an emergency", ashgrove.config).action == "emergency"


@pytest.mark.parametrize("said", ["911", "call 911!", "can you call 911 for me", "should I call 911", "dial 911", "I need to call 911"])
def test_911_as_an_instruction_or_alone_is_live(ashgrove, said):
    assert guard.check_input(said, ashgrove.config).action == "emergency"


INTAKE = ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no", "I was in a car accident", "about two weeks ago"]


def drive(tenant, lines):
    m = IntakeMachine(tenant, "c1", "+14155550142", IN_HOURS)
    m.begin()
    out = []
    for u in lines:
        out += m.handle_text(u)
    return m, out


@pytest.mark.parametrize("story", NARRATIVE[:5] + ["A truck hit me and I was taken to the emergency room and I called 911 at the scene"])
def test_an_injury_story_with_an_er_visit_completes_the_intake_instead_of_hanging_up_on_the_caller(ashgrove, story):
    m, out = drive(ashgrove, [*INTAKE, story])
    assert m.done and m.outcome == "intake_complete", (m.outcome, m.escalation)
    assert not any(isinstance(a, StartTransfer) for a in out)
    assert not any("911" in a.text for a in out if isinstance(a, Say))
    assert m.fields["description"].value == story


def test_a_real_emergency_in_the_middle_of_an_intake_still_gets_the_911_line_and_a_person(ashgrove):
    m, out = drive(ashgrove, [*INTAKE, "my husband is bleeding badly right now"])
    assert any("911" in a.text for a in out if isinstance(a, Say)) and isinstance(out[-1], StartTransfer) and out[-1].reason == "emergency"


OLD_EMERGENCY = [r"\b911\b", r"\bemergency\b", r"\bin (?:immediate )?danger\b", r"\b(?:is|are) (?:badly |seriously )?(?:hurt|bleeding|dying|unconscious)\b",
                 r"\b(?:unconscious|overdos\w*|bleeding (?:badly|heavily|out))\b", r"\bsuicid\w*|\bkill myself\b", r"\bnot breathing\b",
                 r"\bbeing (?:attacked|followed|threatened)\b"]
WORDS = ["I", "he", "she", "was", "is", "are", "am", "I'm", "he's", "were", "had", "been", "call", "called", "calling", "911", "emergency", "room", "unconscious",
         "bleeding", "badly", "heavily", "not", "breathing", "in", "danger", "immediate", "being", "followed", "attacked", "overdosed", "suicide", "after", "when", "ago",
         "the", "a", "crash", "hospital", "and", "but", ".", "now", "just", "please", "help", "dial", "my", "husband", "mother", "kill", "myself", "last", "year"]


def test_class_the_rule_only_ever_fires_on_fewer_inputs_than_before(ashgrove):
    """The new rule can miss what the old one caught (that is what the narrative tests are for) but must never fire where the old one stayed silent."""
    import random
    import re

    rnd = random.Random(12)
    fired = 0
    for _ in range(6000):
        text = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 9)))
        if guard.check_input(text, ashgrove.config).action == "emergency":
            fired += 1
            assert any(re.search(p, guard._norm(text)) for p in OLD_EMERGENCY), text
    assert fired > 200  # the sample really does contain emergencies
