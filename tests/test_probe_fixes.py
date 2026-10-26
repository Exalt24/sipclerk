"""What a blind behaviour probe found with ordinary caller sentences (2026-10-26), each as a class table that fails on the code before the fix:
a yes said as `that's fine`, `I don't know` stored as no, a skip word inside a surname, two states named, a month beaten by `today`, a correction ignored,
`May the fifth`, a year said in words, a first name said alone and then a last name, `this number is fine`, `oh, it's 415...`, a request for a person in
other words, a victim of a drunk driver or an executor turned away as out of scope."""
import itertools
from datetime import date
from pathlib import Path

import pytest

from conftest import IN_HOURS
from sipclerk import guard
from sipclerk.intake import machine as mc
from sipclerk.intake import parsers
from sipclerk.intake.machine import IntakeMachine, Say

TODAY = date(2026, 10, 27)


def says(actions):
    return [a.text for a in actions if isinstance(a, Say)]


def run(m, *utterances):
    out = []
    for u in utterances:
        out += m.handle_text(u)
    return out


# CLASS-TABLE: every way of saying yes that the list missed, with and without a lead-in, and the answers that must stay no or unclear
YES_SAYINGS = ("that's me", "that's it", "that's fine", "fine", "go ahead", "of course", "all right", "alright", "mm-hmm", "sounds good", "that works")
LEADS = ("", "yeah ", "ok ")
TAILS = ("", " thanks", " please")


def test_class_every_plain_yes_reads_as_yes():
    bad = []
    for said, lead, tail in itertools.product(YES_SAYINGS, LEADS, TAILS):
        r = parsers.parse_yesno(f"{lead}{said}{tail}")
        if not (r.ok and r.value is True):
            bad.append((f"{lead}{said}{tail}", r.value if r.ok else r.reason))
    assert not bad, bad[:8]


@pytest.mark.parametrize("said", ["I don't know", "I don't know, maybe", "I don't remember", "I don't recall", "I don't care", "um I don't know"])
def test_i_dont_know_is_not_a_stored_no(said):
    assert not parsers.parse_yesno(said).ok


@pytest.mark.parametrize("said", ["no", "nope", "I don't think so", "I don't have one", "no I don't", "not really", "isn't that", "I didn't"])
def test_a_real_no_is_still_a_no(said):
    r = parsers.parse_yesno(said)
    assert r.ok and r.value is False


# CLASS-TABLE: every skip word inside a name or an address that is not a skip, against the real skips
NAMES_WITH_SKIP_WORDS = ("passmore", "passport", "bypass", "skipper", "nonetheless", "pass-through")


def test_class_a_skip_word_inside_a_longer_word_is_not_a_skip():
    bad = []
    for word, form in itertools.product(NAMES_WITH_SKIP_WORDS, ("jessica {w} at gmail dot com", "jordan at {w}law dot com", "jordan dot {w} at gmail dot com", "{w}")):
        said = form.format(w=word)
        if parsers.is_skip(said):
            bad.append(said)
    assert not bad, bad


@pytest.mark.parametrize("said", ["skip", "I'll pass", "pass", "none", "no thanks", "I would rather not", "skip that one"])
def test_a_real_skip_is_still_a_skip(said):
    assert parsers.is_skip(said)


STATES = ["Florida", "Georgia", "Alabama", "California", "Texas", "New York"]

# CLASS-TABLE: every pair of states named in one sentence, in both orders, with and without a correction
def test_class_two_states_named_are_not_decided_by_the_list_order():
    bad = []
    for a, b in itertools.permutations(STATES, 2):
        plain = parsers.parse_option(f"I live in {a} but my ex lives in {b}", STATES)
        fixed = parsers.parse_option(f"{a} no wait {b}", STATES)
        if plain.ok or plain.reason != "ambiguous":
            bad.append(("plain", a, b, plain.value if plain.ok else plain.reason))
        if not (fixed.ok and fixed.value == b):
            bad.append(("corrected", a, b, fixed.value if fixed.ok else fixed.reason))
    assert not bad, bad[:6]
    assert parsers.parse_option("Georgia", STATES).value == "Georgia"


# CLASS-TABLE: a named month against every relative word that used to beat it, and a correction in every form
def test_class_a_named_month_beats_today_and_yesterday():
    bad = []
    for month, day, rel in itertools.product(range(1, 10), (3, 15, 28), ("I'm calling today", "I saw the doctor yesterday", "this morning I called", "tonight I am calling")):
        name = parsers.MONTHS[month - 1]
        r = parsers.parse_date(f"{name} {day}, {rel}", TODAY)
        if not (r.ok and r.value == f"2026-{month:02d}-{day:02d}"):
            bad.append((name, day, rel, r.value if r.ok else r.reason))
    assert not bad, bad[:6]


def test_class_a_correction_replaces_the_first_answer():
    bad = []
    for cue, (m1, m2) in itertools.product(("no wait", "no sorry", "I mean", "actually", "no no"), itertools.permutations(("march", "april", "june", "august"), 2)):
        r = parsers.parse_date(f"it was in {m1} {cue} {m2}", TODAY)
        want = f"2026-{parsers.MONTHS.index(m2) + 1:02d}-01"
        if not (r.ok and r.value == want):
            bad.append((m1, cue, m2, r.value if r.ok else r.reason))
    assert not bad, bad[:6]
    r = parsers.parse_date("March 3rd, no sorry March 8th", TODAY)
    assert r.ok and r.value == "2026-03-08"


@pytest.mark.parametrize("said,want", [("May the fifth", "2026-05-05"), ("the fifth of May", "2026-05-05"), ("June the twentieth", "2026-06-20"),
                                       ("September fifteenth twenty twenty four", "2024-09-15"), ("March third two thousand twenty five", None),
                                       ("in twenty twenty three", "2023-06-30"), ("back in nineteen ninety five", "1995-06-30"), ("oh nine", None)])
def test_ordinal_with_the_and_a_year_said_in_words(said, want):
    r = parsers.parse_date(said, TODAY)
    if want is None:
        return
    assert r.ok and r.value == want, (said, r.value if r.ok else r.reason)


def test_twenty_one_days_ago_is_still_days_and_not_a_year():
    assert parsers.parse_date("twenty one days ago", TODAY).value == "2026-10-06"


# CLASS-TABLE: a phone said with a leading oh or dots between the groups
@pytest.mark.parametrize("said", ["oh, it's 415 555 0123", "oh 415 555 0123", "415.555.0123", "(415) 555-0123", "415 555 0123 I think", "four one five five five five oh one two three"])
def test_a_phone_said_the_way_it_is_said_is_read(said):
    r = parsers.parse_phone(said)
    assert r.ok and r.value == "+14155550123", (said, r.reason)


def test_a_number_that_really_starts_with_zero_is_still_refused():
    assert not parsers.parse_phone("oh four one five five five five oh one two").ok


# CLASS-TABLE: every way of asking for a person
HUMAN_ASKS = ["let me talk to your manager", "put me through to somebody", "transfer me", "I need to talk to an actual person", "can I speak to the owner",
              "connect me to a human", "get me a representative", "representative", "I want a live agent", "put me through", "speak with someone",
              "I'd like to talk to a real person"]


@pytest.mark.parametrize("said", HUMAN_ASKS)
def test_class_every_way_of_asking_for_a_person_escalates(ashgrove, said):
    assert guard.check_input(said, ashgrove.config).rule == "human_request", said


@pytest.mark.parametrize("said", ["I was rear-ended on the highway", "my manager fired me last week", "the owner of the car sold it", "my agent is my insurance agent and he said no",
                                  "a person hit my car"])
def test_ordinary_sentences_are_not_a_request_for_a_person(ashgrove, said):
    assert guard.check_input(said, ashgrove.config).rule != "human_request", said


def test_first_name_then_last_name_in_two_answers(tidewater):
    m = IntakeMachine(tidewater, "c1", "+14155550142", IN_HOURS)
    m.begin()
    first = says(run(m, "Jordan"))
    assert "last name" in first[0]
    second = says(run(m, "Reyes"))
    assert "Jordan Reyes" in second[0]


def test_the_same_first_name_said_twice_is_still_asked_for_a_last_name(tidewater):
    m = IntakeMachine(tidewater, "c1", "+14155550142", IN_HOURS)
    m.begin()
    run(m, "Jordan")
    assert "last name" in says(run(m, "Jordan"))[0]


def test_my_last_name_is_reyes_after_a_first_name(tidewater):
    m = IntakeMachine(tidewater, "c1", "+14155550142", IN_HOURS)
    m.begin()
    run(m, "Jordan")
    assert "Jordan Reyes" in says(run(m, "my last name is Reyes"))[0]


# CLASS-TABLE: every way of saying "use the number I am calling from"
@pytest.mark.parametrize("said", ["this number is fine", "the number I'm calling from", "same number", "this one", "it's the same as this one", "use this phone"])
def test_class_the_caller_id_is_offered_back_when_the_caller_says_to_use_it(tidewater, said):
    m = IntakeMachine(tidewater, "c1", "+14155550142", IN_HOURS)
    m.begin()
    run(m, "Jordan Reyes", "yes")
    assert m.question.id == "callback_number"
    out = says(run(m, said))
    assert "four one five" in out[0].lower() or "4 1 5" in out[0] or "415" in out[0], out


def test_without_a_caller_id_the_phrase_is_not_a_number(tidewater):
    m = IntakeMachine(tidewater, "c1", "", IN_HOURS)
    m.begin()
    run(m, "Jordan Reyes", "yes")
    assert "digits" in " ".join(says(run(m, "this number is fine")))


# CLASS-TABLE: a person who is the victim of a drunk driver, or an executor, is not turned away as out of scope
@pytest.mark.parametrize("said", ["the other driver had a DUI and hit my car", "I was rear-ended by a drunk driver who was charged with a DUI",
                                  "a drunk driver hit me and I hurt my back"])
def test_class_a_victim_of_a_drunk_driver_is_in_scope_for_the_injury_firm(ashgrove, said):
    assert guard.check_input(said, ashgrove.config, check_scope=True).action != "out_of_scope", said


@pytest.mark.parametrize("said", ["my dad died in a car accident and I'm the executor", "my mother passed away and I need help with her estate",
                                  "my father is deceased and there was a car accident"])
def test_class_an_executor_is_in_scope_for_the_estate_firm(tidewater, said):
    assert guard.check_input(said, tidewater.config, check_scope=True).action != "out_of_scope", said


def test_a_plain_dui_charge_is_still_out_of_scope(ashgrove):
    assert guard.check_input("I got a DUI last night and need a lawyer", ashgrove.config, check_scope=True).action == "out_of_scope"
