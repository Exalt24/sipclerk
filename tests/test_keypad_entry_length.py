"""A keypad number is complete at ten digits unless it starts with 1 (output round 3, F4).

A caller who typed ten digits and did not press pound heard `Are you still there?`, retyped, was told it was too many digits and then too few, and was one miss
from a hand off. No US area code starts with 1, so ten digits that do not start with 1 are a whole number; a number that starts with 1 is the eleven digit form.
"""
import itertools
from datetime import datetime

import pytest

from sipclerk.intake.machine import IntakeMachine, Say, State

NOW = datetime(2026, 10, 27, 17, 30)
SPOKEN = "four one five five five five oh one two three"


def _at_the_phone_question(ashgrove) -> IntakeMachine:
    m = IntakeMachine(ashgrove, "c1", "+14155550142", NOW)
    m.begin()
    for said in ("yes", "Jordan Reyes", "yes"):
        m.handle_text(said)
    assert m.question.id == "callback_number" and m.state == State.ASK
    return m


def _says(acts):
    return [a.text for a in acts if isinstance(a, Say)]


def _digits(first: str, length: int) -> str:
    """Digits that make a real number once there are enough of them: area code and exchange never start with 0 or 1 after the first digit."""
    return (first + "55555012345")[:length]


# CLASS-TABLE: every first digit x every length from 1 to 12, each digit pressed one at a time, and a pound after each length
@pytest.mark.parametrize("first,length", list(itertools.product("123456789", range(1, 13))))
def test_a_keypad_entry_is_closed_when_it_is_complete(ashgrove, first, length):
    m = _at_the_phone_question(ashgrove)
    complete_at = 11 if first == "1" else 10
    digits = _digits(first, length)
    closed_at = None
    for i, d in enumerate(digits, 1):
        acts = m.on_dtmf(d)
        if acts and closed_at is None:
            closed_at = i
        elif closed_at is not None:
            assert acts == [], (first, i)  # a digit after the number is closed changes nothing
    if length < complete_at:
        assert closed_at is None and m.keypad == digits and m.state == State.ASK
        reply = _says(m.on_dtmf("#"))
        if first == "1" and length == 10:  # ten digits that start with 1 are no number: the pound is answered with the question again
            assert len(reply) == 1 and reply[0].startswith("Sorry, I did not catch that."), reply
        else:
            assert reply == [f"I only caught {length} digits. Please say the whole number, or use your keypad."], (first, length, reply)
    else:
        assert closed_at == complete_at and m.state == State.CONFIRM and m.keypad == ""
        assert m.pending == "+1" + digits[complete_at - 10:complete_at]


@pytest.mark.parametrize("first", "123456789")
def test_the_read_back_comes_at_the_last_digit_not_at_the_pound(ashgrove, first):
    m = _at_the_phone_question(ashgrove)
    full = "1" + "4155550123" if first == "1" else first + "155550123"
    out = []
    for d in full:
        out += _says(m.on_dtmf(d))
    assert len(out) == 1 and out[0].endswith("Is that right?")
    assert m.on_dtmf("#") == []  # the pound after a closed entry has nothing left to end


def test_the_ten_digits_of_the_round_are_taken_with_no_pound(ashgrove):
    m = _at_the_phone_question(ashgrove)
    out = []
    for d in "4155550123":
        out += _says(m.on_dtmf(d))
    assert out == ["I have four one five, five five five, zero one two three. Is that right?"]
    m.handle_text("yes")
    assert m.fields["callback_number"].value == "+14155550123" and m.fields["callback_number"].source == "keypad"


@pytest.mark.parametrize("digits", ["4155550123#", "14155550123", "14155550123#"])
def test_the_old_ways_of_ending_an_entry_still_work(ashgrove, digits):
    m = _at_the_phone_question(ashgrove)
    for d in digits:
        m.on_dtmf(d)
    assert m.state == State.CONFIRM and m.pending == "+14155550123"


def test_a_ten_digit_entry_that_is_not_a_number_is_answered_not_awaited(ashgrove):
    m = _at_the_phone_question(ashgrove)
    out = []
    for d in "4151550123":  # the exchange starts with 1
        out += _says(m.on_dtmf(d))
    assert len(out) == 1 and "Sorry, I did not catch that" in out[0] and m.state == State.ASK and m.keypad == ""
