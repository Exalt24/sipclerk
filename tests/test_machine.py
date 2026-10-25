"""The intake state machine: every branch is an event in, actions out, no audio and no model."""
import pytest

from conftest import AFTER_HOURS, IN_HOURS
from sipclerk.intake import machine as mc
from sipclerk.intake.machine import Hangup, IntakeMachine, Say, StartTransfer, Submit


def says(actions):
    return [a.text for a in actions if isinstance(a, Say)]


def kinds(actions):
    return [type(a).__name__ for a in actions]


def new(tenant, now=IN_HOURS, call_id="c1", caller="+14155550142"):
    m = IntakeMachine(tenant, call_id, caller, now)
    return m, m.begin()


def run(m, *utterances):
    out = []
    for u in utterances:
        out += m.handle_text(u)
    return out


GOOD_ASHGROVE = ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no",
                 "I was in a car accident", "about two weeks ago", "A truck ran a red light and hit my car on Main Street"]


def test_begin_gives_greeting_and_both_disclosures_and_asks_for_consent(ashgrove):
    m, acts = new(ashgrove)
    t = says(acts)
    assert t[0] == ashgrove.config.greeting
    assert any("automated assistant" in x for x in t) and any("recorded" in x for x in t)
    assert m.state == mc.State.CONSENT and not m.recording and acts[-1].kind == "disclosure"


def test_notice_jurisdiction_records_without_asking_and_goes_straight_to_the_first_question(tidewater):
    m, acts = new(tidewater)
    assert m.recording and m.state == mc.State.ASK
    assert says(acts)[-1] == tidewater.config.questions[0].prompt


def test_consent_yes_starts_recording_and_no_does_not(ashgrove):
    m, _ = new(ashgrove)
    acts = run(m, "yes that's fine")
    assert m.recording and says(acts) == [ashgrove.config.questions[0].prompt]
    m2, _ = new(ashgrove)
    acts = run(m2, "no I would rather you did not")
    assert not m2.recording and "will not record" in says(acts)[0]


def test_consent_unclear_twice_means_no_recording(ashgrove):
    m, _ = new(ashgrove)
    run(m, "hmm what")
    assert m.state == mc.State.CONSENT
    run(m, "banana")
    assert not m.recording and m.state == mc.State.ASK
    assert any(e["kind"] == "recording_consent" and e.get("unclear") and e["granted"] is False for e in m.events)


def test_full_intake_completes_with_qualified_record_and_one_submit(ashgrove):
    m, _ = new(ashgrove)
    acts = run(m, *GOOD_ASHGROVE)
    assert m.done and kinds(acts[-3:]) == ["Say", "Submit", "Hangup"]
    rec = next(a for a in acts if isinstance(a, Submit)).record
    f = rec["fields"]
    assert f["full_name"]["value"] == "Jordan Reyes" and f["full_name"]["confirmed"]
    assert f["callback_number"]["value"] == "+14155550123" and f["callback_number"]["confirmed"]
    assert f["practice_area"]["value"] == "personal_injury" and f["incident_date"]["value"] == "2026-10-13"
    assert rec["outcome"] == "intake_complete" and rec["qualification"]["result"] == "qualified"
    assert rec["recording"] is True and rec["tasks"][0]["kind"] == "follow_up"
    assert sum(isinstance(a, Submit) for a in acts) == 1


def test_record_is_stamped_with_tenant_version_and_hash_and_a_stable_key(ashgrove):
    m, _ = new(ashgrove, call_id="abc")
    run(m, *GOOD_ASHGROVE)
    r = m.record()
    assert r["tenant_id"] == "ashgrovepell" and r["config_version"] == "2026.10.2" and r["config_hash"] == ashgrove.config_hash
    m2, _ = new(ashgrove, call_id="abc")
    assert m2.idempotency_key() == r["idempotency_key"]
    m3, _ = new(ashgrove, call_id="abd")
    assert m3.idempotency_key() != r["idempotency_key"]


def test_read_back_waits_for_yes_and_a_no_asks_again(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    acts = run(m, "Jordan Reyes")
    assert "Jordan Reyes. Is that right?" in says(acts)[0] and m.state == mc.State.CONFIRM
    assert m.fields["full_name"].value is None  # not stored until confirmed
    acts = run(m, "no")
    assert m.state == mc.State.ASK and says(acts)[0].startswith("Sorry about that")


def test_caller_correction_inside_the_no_is_taken_and_read_back_again(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Rayes")
    acts = run(m, "no it's Jordan Reyes")
    assert "Jordan Reyes. Is that right?" in says(acts)[0]
    run(m, "yes")
    assert m.fields["full_name"].value == "Jordan Reyes" and any(e["kind"] == "caller_correction" for e in m.events)


def test_unclear_confirm_is_asked_once_more_then_counts_as_no(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes")
    assert "please say yes or no" in says(run(m, "mmm"))[0].lower()
    acts = run(m, "banana")
    assert m.state == mc.State.ASK and says(acts)[0].startswith("Sorry about that")


def test_invalid_answers_retry_then_escalate_a_required_field(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    assert "last name" in says(run(m, "Jordan"))[0]
    assert "last name" in says(run(m, "Jordan"))[0]
    acts = run(m, "Jordan")  # third failure is past max_retries=2
    assert kinds(acts)[-1] == "StartTransfer" and acts[-1].reason == "retry_limit:full_name"


def test_phone_digits_heard_so_far_are_kept_and_only_the_rest_is_asked_for(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    first = says(run(m, "four one five five five five"))[0]
    assert first == "I have four one five, five five five so far. Please say the rest of the number."
    assert m.fields["callback_number"].retries == 0  # a pause between groups is not a failed answer
    acts = run(m, "oh one two three")
    assert "four one five, five five five, zero one two three. Is that right?" in says(acts)[0] and m.state == mc.State.CONFIRM


def test_a_caller_who_starts_the_number_over_after_a_partial_is_taken_whole(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes", "four one five")
    acts = run(m, "four one five five five five oh one two three")
    assert "four one five, five five five, zero one two three" in says(acts)[0]


def test_partial_digits_are_not_kept_forever_and_a_keypad_entry_is_never_completed_by_speech(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    for _ in range(3):
        assert "so far" in says(run(m, "four"))[0]
    assert "I only caught" in says(run(m, "four"))[0] and m.fields["callback_number"].retries == 1  # the fourth short answer is a failure
    m2, _ = new(ashgrove)
    run(m2, "yes", "Jordan Reyes", "yes")
    for d in "41555#":
        acts = m2.on_dtmf(d)
    assert "only caught 5 digits" in says(acts)[0]
    assert m2.fields["callback_number"].partial == ""


def test_phone_with_nothing_usable_is_an_ordinary_failed_answer(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    assert "only caught 0 digits" in says(run(m, "I really do not know it"))[0] and m.fields["callback_number"].retries == 1


def test_optional_field_is_skipped_after_the_limit_and_when_the_caller_declines(tidewater):
    m, _ = new(tidewater)
    run(m, "Dana Whitfield", "yes", "four one five five five five oh one two three", "yes")
    assert m.question.id == "email"
    acts = run(m, "I would rather not")
    assert m.fields["email"].skipped and says(acts)[0] == tidewater.config.question("practice_area").prompt
    m2, _ = new(tidewater)
    run(m2, "Dana Whitfield", "yes", "four one five five five five oh one two three", "yes")
    run(m2, "blah blah")
    run(m2, "blah blah")  # max_retries is 1 for email: the second miss skips it
    assert m2.fields["email"].skipped and m2.question.id == "practice_area"


def test_existing_client_answer_escalates_to_a_person(ashgrove):
    m, _ = new(ashgrove)
    acts = run(m, "yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "yes I am")
    assert isinstance(acts[-1], StartTransfer) and acts[-1].reason == "existing_client"
    assert m.state == mc.State.TRANSFERRING and not m.submitted


def test_existing_client_said_aloud_escalates_before_any_model(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    acts = run(m, "I'm calling about my case, my attorney is Ms Alvarez")
    assert isinstance(acts[-1], StartTransfer) and acts[-1].reason == "existing_client"


def test_legal_advice_request_is_deflected_then_asks_again_and_a_repeat_escalates(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    acts = run(m, "do I have a good case?")
    assert "cannot give legal advice" in says(acts)[0] and says(acts)[1] == ashgrove.config.questions[0].prompt
    assert not any(isinstance(a, StartTransfer) for a in acts)
    acts = run(m, "should I sue them")
    assert isinstance(acts[-1], StartTransfer) and acts[-1].reason == "legal_advice_repeat"


def test_a_tenant_that_does_not_escalate_on_repeat_keeps_deflecting(tidewater):
    m, _ = new(tidewater)
    run(m, "what should I do")
    acts = run(m, "should I sue")
    assert not any(isinstance(a, StartTransfer) for a in acts) and m.state == mc.State.ASK


def test_dtmf_zero_is_a_person_at_any_moment(ashgrove):
    for steps in ([], ["yes"], ["yes", "Jordan Reyes"]):
        m, _ = new(ashgrove)
        run(m, *steps)
        acts = m.on_dtmf("0")
        assert isinstance(acts[-1], StartTransfer) and acts[-1].reason == "dtmf_zero"


def test_dtmf_digits_fill_the_phone_field_and_pound_ends_the_entry(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    for d in "4155550123":
        assert m.on_dtmf(d) == []
    acts = m.on_dtmf("#")
    assert "four one five, five five five, zero one two three" in says(acts)[0]
    run(m, "yes")
    assert m.fields["callback_number"].value == "+14155550123" and m.fields["callback_number"].source == "keypad"


def test_a_zero_inside_a_keypad_number_is_a_digit_not_a_transfer(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    for d in "415":
        m.on_dtmf(d)
    assert m.on_dtmf("0") == [] and m.keypad == "4150"


def test_dtmf_is_ignored_where_the_question_does_not_take_it_and_star_repeats(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    assert m.on_dtmf("5") == [] and m.on_dtmf("#") == []
    assert says(m.on_dtmf("*")) == [ashgrove.config.questions[0].prompt]


def test_dtmf_never_lands_in_the_event_log(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    for d in "4155550123#":
        m.on_dtmf(d)
    logged = " ".join(str(e) for e in m.events)
    assert "digit': 'x'" in logged and "'4'" not in logged and "'5'" not in logged


def test_overlong_keypad_entry_is_cut_off_with_an_answer(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes")
    acts = []
    for d in "41555501239":
        acts += m.on_dtmf(d)
    assert "more digits" in says(acts)[0]


def test_silence_reprompts_then_ends_the_call_with_a_callback_task(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    a1 = m.on_silence_timeout()
    assert says(a1)[0].startswith("Are you still there?") and ashgrove.config.questions[0].prompt in says(a1)[0]
    m.on_silence_timeout()
    a3 = m.on_silence_timeout()
    assert kinds(a3)[-2:] == ["Submit", "Hangup"] and m.outcome == "abandoned_silent"
    assert m.tasks[-1]["kind"] == "callback"


def test_an_answer_resets_the_silence_count(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    m.on_silence_timeout()
    m.on_silence_timeout()
    run(m, "Jordan Reyes")
    assert m.silences == 0


def test_silence_during_a_read_back_repeats_the_read_back(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes")
    assert "Jordan Reyes. Is that right?" in says(m.on_silence_timeout())[0]


def test_unsupported_practice_area_by_keyword_ends_politely_with_a_review_task(ashgrove):
    m, _ = new(ashgrove)
    acts = run(m, "yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no",
               "I need a lawyer for a DUI")
    assert m.outcome == "declined_out_of_scope" and kinds(acts)[-2:] == ["Submit", "Hangup"]
    assert says(acts)[-1] == ashgrove.config.guard.out_of_scope_message
    assert m.tasks[0]["kind"] == "review_declined_caller"


def test_unmatched_area_text_is_a_retry_not_a_decline(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no")
    acts = run(m, "it is complicated")
    assert m.state == mc.State.ASK and m.question.id == "practice_area" and "did not catch" in says(acts)[0]


def test_old_incident_is_needs_review_and_other_state_is_declined(ashgrove, tidewater):
    m, _ = new(ashgrove)
    run(m, *GOOD_ASHGROVE[:7], "in March 2021", "I slipped on a wet floor at a grocery store")
    assert m.qualification["result"] == "needs_review" and m.outcome == "intake_needs_review"
    m2, _ = new(tidewater)
    run(m2, "Dana Whitfield", "yes", "four one five five five five oh one two three", "yes", "skip", "my husband and I want a divorce",
        "Georgia", "no", "we are splitting up and need to sort out custody")
    assert m2.qualification["result"] == "decline" and m2.outcome == "declined"


def test_deadline_pressure_makes_the_intake_priority(tidewater):
    m, _ = new(tidewater)
    run(m, "Dana Whitfield", "yes", "four one five five five five oh one two three", "yes", "skip", "my husband and I want a divorce",
        "we live in Florida", "yes", "there is a hearing next week about the custody schedule")
    assert m.qualification["result"] == "priority" and m.tasks[-1]["priority"] == "high"


def test_after_hours_says_so_and_a_person_request_becomes_a_callback_not_a_transfer(ashgrove):
    m, acts = new(ashgrove, now=AFTER_HOURS)
    assert ashgrove.config.after_hours_message in says(acts)
    run(m, "yes")
    acts = run(m, "I want to talk to a person")
    assert not any(isinstance(a, StartTransfer) for a in acts) and m.outcome == "callback_requested"
    assert kinds(acts)[-2:] == ["Submit", "Hangup"] and m.tasks[-1]["kind"] == "callback"


def test_emergency_gets_the_911_line_first_then_a_person(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    acts = run(m, "my husband is bleeding badly")
    assert "911" in says(acts)[0] and isinstance(acts[-1], StartTransfer) and acts[-1].reason == "emergency"


def test_transfer_result_connected_hands_the_call_over_and_failed_leaves_a_callback(ashgrove):
    m, _ = new(ashgrove)
    run(m, "yes")
    run(m, "let me speak to a person")
    assert m.on_transfer_result(True, "answered") == [] and m.outcome == "transferred" and m.done
    m2, _ = new(ashgrove)
    run(m2, "yes")
    run(m2, "let me speak to a person")
    acts = m2.on_transfer_result(False, "busy")
    assert says(acts)[0] == ashgrove.config.transfer.unavailable and kinds(acts)[-2:] == ["Submit", "Hangup"]
    assert m2.outcome == "callback_after_failed_transfer" and m2.transfer["result"] == "failed"
    assert m2.tasks[-1]["kind"] == "callback" and "busy" in m2.tasks[-1]["reason"]


def test_qualification_rules_read_missing_fields_as_failed(ashgrove):
    m, _ = new(ashgrove)
    assert m.evaluate_qualification()["result"] == "decline"
    assert all(not r["passed"] for r in m.evaluate_qualification()["rules"])


def test_the_machine_never_speaks_anything_the_output_guard_would_block(registry):
    from sipclerk import guard

    for t in registry.all:
        m, acts = new(t)
        spoken = says(acts)
        for u in ["yes", "I want a person", "do I have a case", "banana", "my attorney is Ms Lee", "emergency", "four one five"]:
            spoken += says(m.handle_text(u))
            spoken += says(m.on_silence_timeout())
        assert all(guard.check_output(s).ok for s in spoken), t.config.tenant_id


def test_unit_state_after_done_ignores_further_events(ashgrove):
    m, _ = new(ashgrove)
    run(m, *GOOD_ASHGROVE)
    assert m.done and m.on_parsed(m.parse("hello")) == [] and m.on_dtmf("5") == []


def test_a_decline_outranks_a_priority_flag_when_both_rules_fail(tidewater):
    m, _ = new(tidewater)
    run(m, "Dana Whitfield", "yes", "four one five five five five oh one two three", "yes", "skip", "my husband and I want a divorce",
        "Georgia", "yes", "there is a hearing next week about the custody schedule")
    assert m.qualification["result"] == "decline" and m.outcome == "declined"
    assert {r["rule"]: r["passed"] for r in m.qualification["rules"]} == {"florida_only": False, "no_deadline_pressure": False}


def test_the_three_year_window_includes_its_last_day_and_excludes_the_next(ashgrove):
    from datetime import timedelta

    for days, expected in ((1095, True), (1096, False)):
        m, _ = new(ashgrove)
        m.fields["incident_date"].value = (m.today - timedelta(days=days)).isoformat()
        m.fields["practice_area"].value = "personal_injury"
        rules = {r["rule"]: r["passed"] for r in m.evaluate_qualification()["rules"]}
        assert rules["within_three_years"] is expected, days


def test_a_practice_area_the_firm_does_not_list_is_declined_even_when_a_model_named_it(ashgrove):
    from sipclerk.intake.parsers import Parsed

    m, _ = new(ashgrove)
    run(m, "yes", "Jordan Mitchell", "yes", "four one five five five five oh one two three", "yes", "no")
    assert m.question.id == "practice_area"
    acts = m.on_parsed(Parsed(True, "other"))  # what the language model returns for a matter outside the list
    assert m.outcome == "declined_out_of_scope" and kinds(acts)[-2:] == ["Submit", "Hangup"]
    assert says(acts)[0] == ashgrove.config.guard.out_of_scope_message
