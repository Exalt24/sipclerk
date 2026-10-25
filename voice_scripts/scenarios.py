"""The spoken scenarios the voice regression suite replays through the real pipeline.

Every caller line is text here and a WAV on disk (`python scripts/make_wavs.py` renders them with the
local Kokoro voices, different voices from the agent's). A step is what the caller does, in order:

    say      speak a clip, after the agent has finished talking
    barge    speak a clip `delay` seconds after the agent starts its next line, talking over it
    dtmf     send keypad digits as RFC 4733 events
    silence  say nothing for `secs` seconds
    wait     let the agent talk for `secs` seconds without answering
All callers, firms and numbers are invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ASHGROVE = "+14155550101"
TIDEWATER = "+13055550102"
CALLER_VOICES = ["am_adam", "af_bella", "bf_emma", "am_michael"]


@dataclass
class Step:
    kind: str
    text: str = ""
    voice: str = "am_adam"
    secs: float = 0.0
    delay: float = 0.0
    digits: str = ""


@dataclass
class Scenario:
    name: str
    did: str
    caller: str
    steps: list[Step]
    snr_db: float | None = None
    after_hours: bool = False
    stt_fail_calls: tuple[int, ...] = ()
    llm_down: bool = False
    webhook_statuses: tuple[int, ...] = ()
    staff_mode: str = "answer"
    expects_transfer: bool = False
    note: str = ""


def say(text: str, voice: str = "am_adam") -> Step:
    return Step("say", text=text, voice=voice)


PHONE = "four one five, five five five, zero one two three"
ASHGROVE_STEPS = [
    say("Yes, that is fine."),
    say("My name is Jordan Mitchell."),
    say("Yes, that is right."),
    say(PHONE + "."),
    say("Yes."),
    say("No, I am not."),
    say("I was in a car accident."),
    say("It happened about two weeks ago."),
    say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since."),
]

TIDEWATER_STEPS = [
    say("Dana Whitfield.", "af_bella"),
    say("Yes.", "af_bella"),
    say("Seven eight six, five five five, zero one four seven.", "af_bella"),
    say("Yes, that is correct.", "af_bella"),
    say("I would rather not.", "af_bella"),
    say("My husband and I want a divorce and we need to sort out custody.", "af_bella"),
    say("We live in Florida.", "af_bella"),
    say("Yes there is a hearing next week.", "af_bella"),
    say("We are separating and the hearing is about the custody schedule for our two children.", "af_bella"),
]

SCENARIOS: list[Scenario] = [
    Scenario("normal_intake", ASHGROVE, "+14155550111", list(ASHGROVE_STEPS)),
    Scenario("normal_intake_second_firm", TIDEWATER, "+17865550112", list(TIDEWATER_STEPS)),
    Scenario("barge_in", ASHGROVE, "+14155550113", [
        say("Yes, that is fine."), say("My name is Jordan Mitchell."), say("Yes, that is right."),
        Step("barge", text="It is " + PHONE + ".", delay=1.2),
        say("Yes."), say("No."), say("I was injured in a car accident."), say("About two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")],
        note="the caller talks over the phone number question"),
    Scenario("long_silence", ASHGROVE, "+14155550114", [
        Step("silence", secs=24.0), say("Sorry, yes, that is fine."), say("My name is Jordan Mitchell."), say("Yes."),
        say(PHONE + "."), say("Yes."), say("No."), say("I was in a car accident."), say("About two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")],
        note="the caller is silent after the recording question, then carries on after the reprompt"),
    Scenario("abandoned_silence", ASHGROVE, "+14155550115", [Step("silence", secs=40.0)],
             note="the caller never answers; the agent ends the call and leaves a callback task"),
    Scenario("noisy_line", ASHGROVE, "+14155550116", list(ASHGROVE_STEPS), snr_db=15.0,
             note="white noise at 15 dB signal to noise ratio on every caller clip"),
    Scenario("caller_correction", ASHGROVE, "+14155550117", [
        say("Yes, that is fine."), say("My name is Jordan Reed."), say("No, it is Jordan Mitchell."), say("Yes."),
        say(PHONE + "."), say("Yes."), say("No."), say("I was in a car accident."), say("About two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")]),
    Scenario("invalid_answers", ASHGROVE, "+14155550118", [
        say("Yes, that is fine."), say("Jordan."), say("Jordan Mitchell."), say("Yes."),
        say("four one five, five five five."), say(PHONE + "."), say("Yes."), say("No."),
        say("I was in a car accident."), say("It was next year."), say("It happened about two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")],
        note="a first name only, six digits of a phone number, and a date in the future"),
    Scenario("unsupported_practice_area", ASHGROVE, "+14155550119", [
        say("Yes, that is fine."), say("My name is Jordan Mitchell."), say("Yes."), say(PHONE + "."), say("Yes."), say("No."),
        say("I need a lawyer for a drunk driving charge.")]),
    Scenario("legal_advice_request", ASHGROVE, "+14155550120", [
        say("Yes, that is fine."), say("Do I have a good case against them?"), say("My name is Jordan Mitchell."), say("Yes."),
        say(PHONE + "."), say("Yes."), say("No."), say("I was in a car accident."), say("About two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")],
        note="asks for legal advice at the first question; the agent declines and carries on"),
    Scenario("legal_advice_repeated", ASHGROVE, "+14155550121", [
        say("Yes, that is fine."), say("Should I sue them?"), say("Will I win?"), Step("bridge_check")], expects_transfer=True,
        note="a second request for advice hands the caller to a person"),
    Scenario("existing_client_transfer", ASHGROVE, "+14155550122", [
        say("Yes, that is fine."), say("Actually I am already a client, my attorney is Ms Alvarez."),
        Step("bridge_check")], expects_transfer=True,
        note="warm transfer to the firm's desk phone, answered"),
    Scenario("transfer_busy", ASHGROVE, "+14155550123", [
        say("Yes, that is fine."), say("I want to talk to a person please.")], expects_transfer=True, staff_mode="busy",
        note="the desk phone is busy: a callback task and an honest line, no dead air"),
    Scenario("transfer_no_answer", ASHGROVE, "+14155550124", [
        say("Yes, that is fine."), say("I want to talk to a person please.")], expects_transfer=True, staff_mode="no_answer",
        note="the desk phone rings out"),
    Scenario("dtmf_phone_number", ASHGROVE, "+14155550125", [
        say("Yes, that is fine."), say("My name is Jordan Mitchell."), say("Yes."), Step("dtmf", digits="4155550123#"),
        say("Yes."), say("No."), say("I was in a car accident."), say("About two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")]),
    Scenario("sipp_reference", TIDEWATER, "+17865550131", [
        say("Dana Whitfield.", "af_bella"), say("Yes, that is correct.", "af_bella"), Step("dtmf", digits="7865550147#"),
        say("Yes, that is correct.", "af_bella"), say("I would rather not.", "af_bella"),
        say("My husband and I want a divorce and we need to sort out custody.", "af_bella"), say("We live in Florida.", "af_bella"),
        say("Yes there is a hearing next week.", "af_bella"),
        say("We are separating and the hearing is about the custody schedule for our two children.", "af_bella")],
        note="the reference run whose timeline becomes the sipp media file: name, keypad number, the rest spoken"),
    Scenario("dtmf_zero_for_a_person", TIDEWATER, "+17865550126", [Step("wait", secs=1.0), Step("dtmf", digits="0"), Step("bridge_check")],
             expects_transfer=True, note="press zero at any moment for a person"),
    Scenario("provider_stt_timeout", ASHGROVE, "+14155550127", [
        say("Yes, that is fine."), say("My name is Jordan Mitchell."), say("My name is Jordan Mitchell."), say("Yes, that is right."),
        say(PHONE + "."), say("Yes."), say("No, I am not."), say("I was in a car accident."), say("It happened about two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")], stt_fail_calls=(2,),
        note="speech to text times out on the name: the agent asks again, the caller repeats it, and the call completes"),
    Scenario("provider_llm_error", ASHGROVE, "+14155550128", [
        say("Yes, that is fine."), say("My name is Jordan Mitchell."), say("Yes."), say(PHONE + "."), say("Yes."), say("No."),
        say("A driver did not see me at the light."), say("I was injured in a car accident."), say("About two weeks ago."),
        say("A truck ran a red light and hit my car on Main Street, and my neck has hurt ever since.")], llm_down=True,
        note="the language model is down: plain parsing carries the call, the staff summary falls back to a template"),
    Scenario("provider_webhook_500", ASHGROVE, "+14155550129", list(ASHGROVE_STEPS), webhook_statuses=(500, 500, 503),
             note="the case-management receiver fails three times: the call is unaffected and the delivery is retried"),
    Scenario("after_hours", ASHGROVE, "+14155550130", list(ASHGROVE_STEPS), after_hours=True,
             note="the office is closed: the agent says so and takes the intake for a callback"),
]
BY_NAME = {s.name: s for s in SCENARIOS}


def all_texts() -> list[tuple[str, str]]:
    """Every (voice, text) the scenarios speak, once."""
    seen: dict[tuple[str, str], None] = {}
    for sc in SCENARIOS:
        for st in sc.steps:
            if st.text:
                seen[(st.voice, st.text)] = None
    return list(seen)
