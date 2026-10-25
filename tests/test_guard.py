"""One test per guard rule, each with several phrasings, so deleting a rule turns a test red."""
import pytest

from sipclerk import guard


@pytest.mark.parametrize("text", [
    "Do I have a good case?", "should I sue him", "Can I sue my landlord", "will I win this", "what are my chances",
    "How much is my case worth", "what should I do", "Am I entitled to anything", "is that legal",
    "what's the statute of limitations", "how long do I have to file",
])
def test_legal_advice_request_is_deflected(ashgrove, text):
    v = guard.check_input(text, ashgrove.config)
    assert v.action == "deflect" and v.rule == "legal_advice_request"


@pytest.mark.parametrize("text", [
    "I'm already a client", "I am a client of yours", "my attorney is Ms Alvarez", "I'm calling about my case",
    "this is about my existing case", "what's my case number", "I am already represented by your firm",
])
def test_existing_client_escalates(ashgrove, text):
    v = guard.check_input(text, ashgrove.config)
    assert v.action == "escalate" and v.rule == "existing_client", text


@pytest.mark.parametrize("text", [
    "I want to speak to a person", "let me talk to someone", "get me a human", "operator", "I need a real person",
    "can I speak with an attorney please",
])
def test_human_request_escalates(ashgrove, text):
    v = guard.check_input(text, ashgrove.config)
    assert v.action == "escalate" and v.rule == "human_request", text


@pytest.mark.parametrize("text", ["this is an emergency", "call 911", "I am in danger", "he is bleeding", "I want to kill myself",
                                  "she's not breathing"])
def test_emergency_is_its_own_verdict(ashgrove, text):
    assert guard.check_input(text, ashgrove.config).action == "emergency"


def test_out_of_scope_is_only_checked_when_asked_and_in_scope_words_win(ashgrove):
    assert guard.check_input("I need help with a DUI charge", ashgrove.config).action == "allow"
    v = guard.check_input("I need help with a DUI charge", ashgrove.config, check_scope=True)
    assert v.action == "out_of_scope" and v.rule == "out_of_scope_area"
    mixed = guard.check_input("I was hurt in a crash and now there is a DUI charge", ashgrove.config, check_scope=True)
    assert mixed.action == "allow"


def test_out_of_scope_words_differ_per_tenant(ashgrove, tidewater):
    text = "I was in a car accident"
    assert guard.check_input(text, ashgrove.config, check_scope=True).action == "allow"
    assert guard.check_input(text, tidewater.config, check_scope=True).action == "out_of_scope"


def test_plain_intake_answers_are_allowed(ashgrove):
    for t in ("Jordan Reyes", "a truck hit my car last month", "yes", "four one five five five five oh one two three"):
        assert guard.check_input(t, ashgrove.config, check_scope=True).action == "allow", t


def test_blocked_caller_is_refused_before_answering(ashgrove):
    assert guard.check_caller("+15550000001", ashgrove.config).action == "blocked_caller"
    assert guard.check_caller("+15550000002", ashgrove.config).action == "allow"


@pytest.mark.parametrize("text,rule", [
    ("You will definitely win this one.", "promised_outcome"),
    ("I guarantee the firm will help.", "promised_outcome"),
    ("You have a strong case.", "promised_outcome"),
    ("Your claim is worth a lot.", "promised_outcome"),
    ("We will win your case.", "promised_outcome"),
    ("You should sue them.", "legal_advice_out"),
    ("I recommend that you accept the offer.", "legal_advice_out"),
    ("The statute of limitations is two years.", "legal_advice_out"),
    ("You don't have a claim here.", "legal_advice_out"),
    ("You are entitled to compensation.", "legal_advice_out"),
    ("You're now a client of the firm.", "attorney_client_implied"),
    ("We will represent you.", "attorney_client_implied"),
    ("The attorney-client relationship has been established.", "attorney_client_implied"),
    ("We have accepted your case.", "attorney_client_implied"),
    ("I have booked your consultation.", "unmade_commitment"),
    ("See you on Tuesday.", "unmade_commitment"),
])
def test_output_rules_block_and_replace(text, rule):
    v = guard.check_output(text)
    assert not v.ok and rule in v.rules and v.text == guard.SAFE_REPLACEMENT


def test_clean_agent_text_passes_unchanged():
    for t in ("Thank you. An attorney will review this and someone will contact you.",
              "I cannot give legal advice. An attorney will look at your details.",
              "I am sorry to hear that. Let me take a few details."):
        v = guard.check_output(t)
        assert v.ok and v.text == t, t


def test_every_scripted_line_in_every_tenant_passes_the_output_guard(registry):
    for t in registry.all:
        c = t.config
        lines = [c.greeting, c.after_hours_message, c.closing, c.transfer.announce, c.transfer.unavailable,
                 c.guard.out_of_scope_message, *(d.text for d in c.disclosures), *(q.prompt for q in c.questions)]
        for line in lines:
            assert guard.check_output(line).ok, (c.tenant_id, line)
