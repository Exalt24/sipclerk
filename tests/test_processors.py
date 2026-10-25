"""Pipeline processors called directly: what the Intake processor refuses to say, and what the Stt processor refuses to hear."""
import asyncio

import pytest

from conftest import IN_HOURS
from fake_models import FakeModels
from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from sipclerk import guard
from sipclerk.intake.machine import IntakeMachine, Say
from sipclerk.voice import processors as vp
from sipclerk.voice.llm import Llm
from sipclerk.voice.models import Transcript


class FakeRtp:
    tx_backlog_frames = 0


def make_ctx(tenant, llm=None, models=None):
    m = IntakeMachine(tenant, "c1", "+14155550142", IN_HOURS)
    return vp.CallContext("c1", tenant.config, m, models or FakeModels(), llm or Llm(fail=True), FakeRtp(), tenant.config.voice, m.today)


def capture(proc):
    out = []

    async def push(frame, direction=None):
        out.append(frame)

    proc.push_frame = push
    return out


async def test_an_unsafe_line_is_replaced_before_it_reaches_the_voice(ashgrove):
    ctx = make_ctx(ashgrove)
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._say(Say("You have a strong case and you will win."))
    assert [f.text for f in out if isinstance(f, TTSSpeakFrame)] == [guard.SAFE_REPLACEMENT]
    assert ctx.guard_events[0]["side"] == "output" and "promised_outcome" in ctx.guard_events[0]["rules"]
    assert any(e["kind"] == "output_blocked" for e in ctx.machine.events)


async def test_a_safe_line_is_spoken_as_written(ashgrove):
    ctx = make_ctx(ashgrove)
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._say(Say("May I have your first and last name?"))
    assert [f.text for f in out] == ["May I have your first and last name?"] and ctx.guard_events == []


class AckLlm(Llm):
    def __init__(self, text):
        super().__init__()
        self.text = text

    async def acknowledge(self, description):
        return self.text


async def test_an_unsafe_acknowledgement_is_dropped_not_replaced(ashgrove):
    ctx = make_ctx(ashgrove, AckLlm("I promise the firm will win your case for sure."))
    ctx.machine.fields["description"].value = "A truck hit my car."
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._say(Say("Thank you. Goodbye.", "closing"))
    assert [f.text for f in out] == ["Thank you. Goodbye."]
    assert ctx.guard_events and ctx.guard_events[0]["side"] == "output"


async def test_a_safe_acknowledgement_comes_before_the_closing_line(ashgrove):
    ctx = make_ctx(ashgrove, AckLlm("I am sorry to hear about the accident."))
    ctx.machine.fields["description"].value = "A truck hit my car."
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._say(Say("Thank you. Goodbye.", "closing"))
    assert [f.text for f in out] == ["I am sorry to hear about the accident.", "Thank you. Goodbye."]


async def test_no_acknowledgement_when_the_caller_never_described_anything(ashgrove):
    ctx = make_ctx(ashgrove, AckLlm("I am sorry to hear that."))
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._say(Say("Thank you. Goodbye.", "closing"))
    assert [f.text for f in out] == ["Thank you. Goodbye."]


async def test_the_guard_screens_the_words_before_the_model_is_asked(ashgrove):
    class Boom(Llm):
        async def classify_area(self, *a):
            raise AssertionError("the model must not see a guarded utterance")

    ctx = make_ctx(ashgrove, Boom())
    m = ctx.machine
    m.begin()
    m.handle_text("yes")
    m.handle_text("Jordan Reyes")
    m.handle_text("yes")
    m.handle_text("four one five five five five oh one two three")
    m.handle_text("yes")
    m.handle_text("no")
    assert m.question.id == "practice_area"
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._on_transcription(TranscriptionFrame(text="should I sue the other driver", user_id="c", timestamp="t"))
    assert ctx.guard_events[0]["rule"] == "legal_advice_request" and any("cannot give legal advice" in f.text for f in out)


async def test_the_model_is_asked_only_when_the_plain_parser_could_not_read_the_answer(ashgrove):
    asked = []

    class Spy(Llm):
        async def classify_area(self, text, ids):
            asked.append(text)
            return "employment"

    ctx = make_ctx(ashgrove, Spy())
    m = ctx.machine
    m.begin()
    for u in ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no"]:
        m.handle_text(u)
    intake = vp.Intake(ctx)
    capture(intake)
    p = await intake._interpret("I was in a car accident")
    assert p.ok and p.value == "personal_injury" and asked == []
    p = await intake._interpret("my landlord did something odd")
    assert p.ok and p.value == "employment" and asked == ["my landlord did something odd"]


async def test_a_model_answer_goes_through_the_same_validators(ashgrove):
    class BadDate(Llm):
        async def extract_date(self, text, today):
            return "2031-01-01"

    ctx = make_ctx(ashgrove, BadDate())
    m = ctx.machine
    m.begin()
    for u in ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no", "car accident"]:
        m.handle_text(u)
    assert m.question.id == "incident_date"
    p = await vp.Intake(ctx)._interpret("sometime in the far future")
    assert not p.ok and p.reason == "in_the_future"


class StubStt(vp.Stt):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.out = []

    async def push_frame(self, frame, direction=None):
        self.out.append(frame)


async def transcribe_one(ctx, text, seconds, no_speech=0.0):
    class M:
        async def transcribe(self, pcm):
            return Transcript(text, no_speech, seconds, 0.01)

    ctx.models = M()
    stt = StubStt(ctx)
    await stt._q.put((bytes(int(seconds * 32000)), vp.Turn()))
    task = asyncio.get_running_loop().create_task(stt._work())
    await asyncio.sleep(0.1)
    task.cancel()
    return stt.out


@pytest.mark.parametrize("text,seconds,nsp,kept", [
    ("You", 0.8, 0.1, False), ("Thank you.", 1.0, 0.1, False), ("thank you", 3.0, 0.1, True), ("Jordan Reyes", 1.0, 0.1, True),
    ("Jordan Reyes", 1.0, 0.9, False), ("", 1.0, 0.0, False),
])
async def test_phantom_words_are_not_passed_on_as_the_callers_answer(ashgrove, text, seconds, nsp, kept):
    ctx = make_ctx(ashgrove)
    frames = await transcribe_one(ctx, text, seconds, nsp)
    got = [f for f in frames if isinstance(f, TranscriptionFrame)]
    assert bool(got) is kept
    if not kept:
        assert ctx.stt_dropped == 1


async def test_a_failing_transcription_becomes_an_empty_frame_marked_as_a_failure(ashgrove):
    ctx = make_ctx(ashgrove)

    class Boom:
        async def transcribe(self, pcm):
            raise TimeoutError("provider down")

    ctx.models = Boom()
    stt = StubStt(ctx)
    await stt._q.put((bytes(32000), vp.Turn()))
    task = asyncio.get_running_loop().create_task(stt._work())
    await asyncio.sleep(0.1)
    task.cancel()
    fr = [f for f in stt.out if isinstance(f, TranscriptionFrame)]
    assert len(fr) == 1 and fr[0].text == "" and fr[0].timestamp.startswith("fail:")


async def test_a_backchannel_gets_a_kind_word_and_does_not_count_as_an_answer(ashgrove):
    ctx = make_ctx(ashgrove)
    m = ctx.machine
    m.begin()
    m.handle_text("yes")
    intake = vp.Intake(ctx)
    out = capture(intake)
    before = m.fields["full_name"].retries
    await intake._on_transcription(TranscriptionFrame(text="Hold on.", user_id="c", timestamp="t"))
    assert [f.text for f in out] == ["Of course, take your time."] and m.fields["full_name"].retries == before


async def test_an_empty_transcription_counts_as_a_missed_answer(ashgrove):
    ctx = make_ctx(ashgrove)
    m = ctx.machine
    m.begin()
    m.handle_text("yes")
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._on_transcription(TranscriptionFrame(text="", user_id="c", timestamp="fail:TimeoutError"))
    assert m.fields["full_name"].retries == 1 and any("did not catch that" in f.text for f in out)
    assert any(e["kind"] == "stt_failure" for e in m.events)


def test_the_fixed_lines_cover_every_unformatted_say_in_the_machine(registry):
    from sipclerk.intake.machine import FIXED_LINES
    from sipclerk.voice.models import fixed_lines, split_sentences

    cached = {s for _, line in fixed_lines(registry) for s in split_sentences(line)}
    for line in FIXED_LINES:
        assert all(s in cached for s in split_sentences(line))
    for t in registry.all:
        for q in t.config.questions:
            assert q.prompt in cached or all(s in cached for s in split_sentences(q.prompt))


async def test_a_failing_submit_is_recorded_and_the_call_still_ends(ashgrove):
    from sipclerk.intake.machine import Hangup, Submit

    ended = []
    ctx = make_ctx(ashgrove)

    def boom(record):
        raise RuntimeError("the receiver is down")

    ctx.on_submit, ctx.on_hangup = boom, ended.append
    intake = vp.Intake(ctx)
    capture(intake)
    await intake._perform([Submit({"x": 1}), Hangup("complete")])
    await intake._detached
    assert ended == ["complete"] and any(e["kind"] == "submit_error" for e in ctx.machine.events)


async def test_a_transfer_that_raises_becomes_a_failed_transfer_with_the_callback_line(ashgrove):
    from sipclerk.intake.machine import StartTransfer

    ctx = make_ctx(ashgrove)
    ctx.machine.begin()
    ctx.machine.handle_text("yes")
    acts = ctx.machine.handle_text("let me speak to a person")
    assert isinstance(acts[-1], StartTransfer)

    async def boom(reason):
        raise OSError("socket closed")

    ended = []
    ctx.on_transfer, ctx.on_hangup = boom, ended.append
    intake = vp.Intake(ctx)
    out = capture(intake)
    await intake._perform([acts[-1]])
    await intake._detached  # the failed transfer queues the callback line and the hang-up in a second detached task
    await asyncio.sleep(0.3)
    if intake._detached:
        await intake._detached
    assert ctx.machine.transfer["result"] == "failed" and ctx.machine.outcome == "callback_after_failed_transfer"
    assert any(f.text == ashgrove.config.transfer.unavailable for f in out) and ended == ["transfer_failed"]


async def test_the_voice_activity_detector_waits_longer_while_the_caller_tells_a_story_or_says_a_number(ashgrove):
    ctx = make_ctx(ashgrove)
    vad = vp.Vad(ctx)
    m = ctx.machine
    m.begin()
    m.handle_text("yes")
    vad._sync_endpointing()
    assert vad.analyzer.params.stop_secs == 0.5  # the name: short
    m.handle_text("Jordan Mitchell")  # now reading back: a yes or no, short
    m.handle_text("yes")
    vad._sync_endpointing()
    assert vad.analyzer.params.stop_secs == 1.0  # the phone number: groups of digits come with pauses
    for u in ["four one five five five five oh one two three", "yes", "no", "car accident", "two weeks ago"]:
        m.handle_text(u)
    assert m.question.id == "description"
    vad._sync_endpointing()
    assert vad.analyzer.params.stop_secs == 1.2  # the story


def test_a_question_can_set_its_own_endpointing(ashgrove):
    from sipclerk.intake.machine import IntakeMachine

    m = IntakeMachine(ashgrove, "c", "+1", IN_HOURS)
    m.begin()
    m.handle_text("yes")
    assert m.endpoint_s == 0.5
    m.question.endpointing_s = 2.0
    assert m.endpoint_s == 2.0
