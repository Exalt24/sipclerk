"""The existing client rule is not run on a caller who has just said they are not a client (output round 3, F5).

`I'm calling about my case, I was rear-ended` after a no to `Are you already a client of the firm?` was escalated as existing_client, and after hours the intake
stopped before the matter, the date and the story. The same went for `my case manager at the hospital` and `my lawyer is not returning my calls`.
"""
import itertools
from datetime import datetime

import pytest

from sipclerk import guard
from sipclerk.intake.machine import IntakeMachine, Say, StartTransfer

IN_HOURS = datetime(2026, 10, 27, 17, 30)
AFTER = datetime(2026, 11, 1, 3, 0)
HEAD_BEFORE_THE_QUESTION = ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes"]

# One phrase for every pattern of the rule (and more than one way of saying each), then the sentences where the words belong to the matter being reported.
CLIENT_PHRASES = [
    "I'm already a client", "I am a client of yours", "I'm currently a client", "I am already a client of the firm",
    "my lawyer is Ms Pell", "our attorney at your office", "my paralegal there", "my case manager at the hospital", "my lawyer is not returning my calls",
    "my existing case", "my current matter", "my open file", "I'm calling about my case", "calling regarding my matter", "calling for my file",
    "my case number is 4471", "I'm represented by your firm", "I am already represented by you",
    "I'm calling about my case, I was rear-ended on the freeway last week",
    "My case manager at the hospital told me to call a lawyer about the crash",
    "My lawyer is not returning my calls so I want a new firm for my wrongful termination",
]


def _machine(ashgrove, now, answer_to_client_question):
    m = IntakeMachine(ashgrove, "c1", "+14155550142", now)
    m.begin()
    for said in HEAD_BEFORE_THE_QUESTION:
        m.handle_text(said)
    assert m.question.id == "existing_client"
    if answer_to_client_question:
        m.handle_text(answer_to_client_question)
    return m


def _escalated(acts) -> bool:
    return any(isinstance(a, StartTransfer) for a in acts) or any(isinstance(a, Say) and "someone will call you back" in a.text for a in acts)


# CLASS-TABLE: every answer to the question x every phrase x in hours and after hours, the phrase said at the next question
@pytest.mark.parametrize("answer,phrase,now", list(itertools.product(["no", "No, I'm not", "nope", "no that's not me"], CLIENT_PHRASES, [IN_HOURS, AFTER])))
def test_after_a_no_the_phrase_is_the_matter_not_a_client(ashgrove, answer, phrase, now):
    m = _machine(ashgrove, now, answer)
    assert m.fields["existing_client"].value is False and m.question.id == "practice_area"
    acts = m.handle_text(phrase)
    assert m.escalation == "" and not _escalated(acts), (answer, phrase)


@pytest.mark.parametrize("phrase,now", list(itertools.product(CLIENT_PHRASES, [IN_HOURS, AFTER])))
def test_after_a_no_the_description_is_stored_whatever_it_says(ashgrove, phrase, now):
    m = _machine(ashgrove, now, "no")
    m.handle_text("I was in a car accident")
    m.handle_text("about two weeks ago")
    assert m.question.id == "description"
    acts = m.handle_text(phrase + " and I was hit while crossing")
    assert m.escalation == "" and m.fields["description"].value is not None and not _escalated(acts), phrase


@pytest.mark.parametrize("phrase,now", list(itertools.product(CLIENT_PHRASES, [IN_HOURS, AFTER])))
def test_before_the_question_is_answered_the_rule_still_runs(ashgrove, phrase, now):
    m = IntakeMachine(ashgrove, "c1", "+14155550142", now)
    m.begin()
    m.handle_text("yes")
    assert m.question.id == "full_name"
    v = m.screen(phrase)
    assert (v.action, v.rule) == ("escalate", "existing_client"), phrase


@pytest.mark.parametrize("answer,now", list(itertools.product(["yes", "Yes I am", "yeah"], [IN_HOURS, AFTER])))
def test_a_yes_still_goes_to_a_person(ashgrove, answer, now):
    m = _machine(ashgrove, now, None)
    acts = m.handle_text(answer)
    assert m.escalation == "existing_client" and _escalated(acts)


@pytest.mark.parametrize("phrase", CLIENT_PHRASES)
def test_a_firm_that_never_asks_the_question_keeps_the_rule(tidewater, phrase):
    m = IntakeMachine(tidewater, "c1", "+13055550142", IN_HOURS)
    m.begin()
    for said in ("Jordan Reyes", "yes", "three oh five five five five oh one four two", "yes", "skip"):
        m.handle_text(said)
    assert "existing_client" not in m.fields and m.question.id == "practice_area"
    v = m.screen(phrase)
    assert (v.action, v.rule) == ("escalate", "existing_client"), phrase


@pytest.mark.parametrize("phrase", CLIENT_PHRASES)
def test_the_guard_flag_turns_off_only_the_existing_client_rule(ashgrove, phrase):
    skipped = guard.check_input(phrase, ashgrove.config, not_a_client=True)
    assert skipped.rule != "existing_client"
    for urgent, rule in [("my husband is not breathing", "emergency"), ("let me speak to a real person", "human_request"),
                         ("do I have a case against my boss", "legal_advice_request")]:
        assert guard.check_input(phrase + ". " + urgent, ashgrove.config, not_a_client=True).rule == rule


@pytest.mark.parametrize("phrase", CLIENT_PHRASES)
def test_the_guard_runs_the_rule_unless_it_is_told_the_answer_was_no(ashgrove, phrase):
    assert guard.check_input(phrase, ashgrove.config).rule == "existing_client", phrase
    assert guard.check_input(phrase, ashgrove.config, check_scope=True).rule == "existing_client", phrase
