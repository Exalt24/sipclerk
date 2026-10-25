"""A practice area the caller already confirmed in scope is not overturned by a word in the description.

A personal injury caller says `he rear ended me and the cops arrested him for DUI`: the matter is the crash, the firm takes it, and the intake was ended
with `not an area this firm handles` because `dui` is on the firm's out of scope list. The word check stays where the area is not yet known or is not
one the firm takes (the practice area question itself, a skipped area, a model's `other`)."""
import pytest

from conftest import IN_HOURS
from sipclerk.intake.machine import IntakeMachine

AREA_SAYING = {"ashgrovepell": {"personal_injury": "a car accident", "employment": "I was fired"},
               "tidewaterquint": {"family_law": "a custody case", "immigration": "a visa problem", "estate_planning": "a will"}}


def to_description(tenant, area_said):
    """The machine at the description question, every earlier question answered the way an ordinary caller does."""
    m = IntakeMachine(tenant, "c1", "+14155559999", IN_HOURS)
    m.begin()
    if m.state.value == "consent":
        m.handle_text("yes")
    while m.question is not None and m.question.id != "description":
        q = m.question
        answers = {"name": ["Jordan Reyes", "yes"], "phone": ["four one five five five five zero one two three", "yes"], "yesno": ["no"], "email": ["skip"],
                   "date": ["two weeks ago", "yes"]}.get(q.type, [])
        if q.id == "practice_area":
            answers = [area_said]
        elif q.id == "state":
            answers = ["Florida", "yes"]
        for a in answers:
            m.handle_text(a)
    assert m.question is not None and m.question.id == "description", m.outcome
    return m


def cases(registry):
    for t in registry.all:
        for area_id, said in AREA_SAYING[t.config.tenant_id].items():
            for word in t.config.guard.out_of_scope_keywords:
                yield t, area_id, said, word


def test_a_confirmed_in_scope_area_is_not_turned_away_by_a_word_in_the_description(registry):
    seen = 0
    for t, area_id, said, word in cases(registry):
        m = to_description(t, said)
        assert m.fields["practice_area"].value == area_id, (t.config.tenant_id, said, m.fields["practice_area"].value)
        m.handle_text(f"it started when the police mentioned {word} in the report")
        assert m.outcome != "declined_out_of_scope" and m.fields["description"].value, (t.config.tenant_id, area_id, word, m.outcome)
        seen += 1
    assert seen >= 30


def test_the_word_check_still_runs_while_the_area_is_unknown_or_one_the_firm_does_not_take(registry):
    for t in registry.all:
        word = t.config.guard.out_of_scope_keywords[0]
        said = next(iter(AREA_SAYING[t.config.tenant_id].values()))
        for value in (None, "other", "not_an_area"):  # skipped, a model's `other`, an id the firm does not list
            m = to_description(t, said)
            m.fields["practice_area"].value = value
            m.handle_text(f"I want help with a {word} matter")
            assert m.outcome == "declined_out_of_scope", (t.config.tenant_id, value, m.outcome)


def test_the_practice_area_question_itself_still_declines_on_the_word(ashgrove):
    m = IntakeMachine(ashgrove, "c1", "+14155559999", IN_HOURS)
    m.begin()
    for a in ["yes", "Jordan Reyes", "yes", "four one five five five five zero one two three", "yes", "no", "I need a lawyer for a DUI"]:
        m.handle_text(a)
    assert m.outcome == "declined_out_of_scope"


@pytest.mark.parametrize("said", ["A truck ran a red light and hit my car on Main Street", "I was rear ended and my neck has hurt ever since"])
def test_an_ordinary_description_is_taken_as_before(ashgrove, said):
    m = to_description(ashgrove, "a car accident")
    m.handle_text(said)
    assert m.outcome == "intake_complete" and m.fields["description"].value == said


def test_a_description_that_names_a_charge_after_a_crash_completes_the_intake(ashgrove):
    m = to_description(ashgrove, "a car accident")
    m.handle_text("He rear ended me at a red light and the cops arrested him for DUI")
    assert m.outcome == "intake_complete"
