"""Pipecat processors for one call: RTP in, voice activity, speech to text, the intake brain, speech out, RTP out.

    RtpInput -> Vad -> Stt -> Intake -> Tts -> RtpOutput

Barge-in: when the caller starts talking while the agent is speaking, Intake broadcasts an interruption.
Every processor that holds queued speech drops it, and RtpOutput empties the RTP send queue, so the agent
stops within a frame or two instead of finishing its sentence.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from sipclerk import audio, guard
from sipclerk.intake import parsers
from sipclerk.intake.machine import Hangup, IntakeMachine, Say, StartTransfer, State, Submit
from sipclerk.intake.parsers import Parsed
from sipclerk.sip.rtp import RtpSession
from sipclerk.voice.llm import Llm
from sipclerk.voice.models import Models

log = logging.getLogger("sipclerk.voice")

PREROLL_S = 0.4
MIN_UTTERANCE_S = 0.25
CATCH_UP_MAX = 25  # frames pushed in one go when the clock fell behind: half a second of audio
BACKCHANNELS = {"wait", "hold on", "one second", "one sec", "just a second", "just a moment", "sorry", "um", "uh", "hmm", "okay wait",
                "hang on", "give me a second", "let me think"}
JUNK_TRANSCRIPTS = {"you", "thank you", "thanks", "bye", "the", "uh", "um", "hmm", "thanks for watching", "so", "oh"}


@dataclass
class Turn:
    """Timing of one caller turn as the agent saw it."""
    vad_stop: float = 0.0
    stt_done: float = 0.0
    decided: float = 0.0
    first_audio: float = 0.0
    text: str = ""
    tts_cached: bool = True
    llm_used: bool = False


@dataclass
class CallContext:
    call_id: str
    tenant_cfg: object
    machine: IntakeMachine
    models: Models
    llm: Llm
    rtp: RtpSession
    voice: str
    now_date: date
    outstanding: set = field(default_factory=set)
    transcript: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    guard_events: list = field(default_factory=list)
    user_speaking: bool = False
    turn_busy: bool = False
    barge_ins: int = 0
    stt_calls: int = 0
    stt_seconds: float = 0.0
    stt_dropped: int = 0
    catch_up_frames: int = 0
    tts_seconds: float = 0.0
    caller_audio_seconds: float = 0.0
    agent_audio_seconds: float = 0.0
    stt_fail_calls: set = field(default_factory=set)  # test hook: provider failure on the Nth transcription
    on_submit = None
    on_hangup = None
    on_transfer = None
    first_audio_event: asyncio.Event | None = None
    open_turn: Turn | None = None

    @property
    def bot_speaking(self) -> bool:
        return bool(self.outstanding) or self.rtp.tx_backlog_frames > 0

    def say_log(self, text: str) -> None:
        self.transcript.append({"speaker": "AGENT", "text": text, "t": time.time()})


# ------------------------------------------------------------------------------------------------ input


class RtpInput(FrameProcessor):
    """Turns the decoded RTP stream into 16 kHz audio frames on a steady 20 ms clock (silence when nothing arrives)."""

    def __init__(self, ctx: CallContext):
        super().__init__(name="rtp-in")
        self.ctx = ctx
        self._task: asyncio.Task | None = None
        self._upsampler = audio.StreamUpsampler()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and self._task is None:
            await self.push_frame(frame, direction)
            loop = asyncio.get_running_loop()
            self.ctx.rtp.on_dtmf = lambda d: loop.create_task(self._dtmf(d))
            self._task = self.create_task(self._pump())
            return
        if isinstance(frame, (EndFrame, CancelFrame)) and self._task:
            await self.cancel_task(self._task)
            self._task = None
        await self.push_frame(frame, direction)

    async def _dtmf(self, digit: str) -> None:
        try:
            await self.push_frame(InputDTMFFrame(button=KeypadEntry(digit)))
        except ValueError:
            pass  # A-D events are not on the keypad

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        step = audio.FRAME_MS / 1000
        next_at = loop.time()
        silence = bytes(audio.FRAME_SAMPLES * 2)
        rx = self.ctx.rtp.rx_frames
        while True:
            next_at += step
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:
                next_at = loop.time()
            behind = len(rx) > 3  # the loop ran late: catch up with the real audio that queued, never drop it
            for _ in range(min(len(rx), CATCH_UP_MAX) if behind else 1):
                pcm8 = rx.popleft() if rx else silence
                if behind:
                    self.ctx.catch_up_frames += 1
                self.ctx.caller_audio_seconds += step
                pcm16 = self._upsampler.feed(pcm8)
                await self.push_frame(InputAudioRawFrame(audio=pcm16, sample_rate=audio.STT_RATE, num_channels=1))


class Vad(FrameProcessor):
    """Silero voice activity detection on the 16 kHz stream: UserStarted and UserStopped speaking frames."""

    def __init__(self, ctx: CallContext, stop_secs: float = 0.5, start_secs: float = 0.2, confidence: float = 0.7, min_volume: float = 0.4):
        super().__init__(name="vad")
        self.ctx = ctx
        self.analyzer = SileroVADAnalyzer(sample_rate=audio.STT_RATE, params=VADParams(
            confidence=confidence, start_secs=start_secs, stop_secs=stop_secs, min_volume=min_volume))
        self.analyzer.set_sample_rate(audio.STT_RATE)
        self.stop_secs = stop_secs
        self._state = VADState.QUIET

    def _sync_endpointing(self) -> None:
        """Follow the machine: a longer silence ends a turn while the caller is telling a story or saying a number."""
        want = self.ctx.machine.endpoint_s
        if want != self.stop_secs:
            self.stop_secs = want
            p = self.analyzer.params
            self.analyzer.set_params(VADParams(confidence=p.confidence, start_secs=p.start_secs, stop_secs=want, min_volume=p.min_volume))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self._sync_endpointing()
            new = await self.analyzer.analyze_audio(frame.audio)
            if new != self._state and new not in (VADState.STARTING, VADState.STOPPING):
                self._state = new
                log.info("vad %s (bot_speaking=%s)", new.name, self.ctx.bot_speaking)
                if new == VADState.SPEAKING:
                    self.ctx.user_speaking = True
                    await self.push_frame(UserStartedSpeakingFrame())
                else:
                    self.ctx.user_speaking = False
                    await self.push_frame(UserStoppedSpeakingFrame())
        await self.push_frame(frame, direction)


class Stt(FrameProcessor):
    """Buffers audio around speech and transcribes each utterance with the shared Whisper model, in order."""

    def __init__(self, ctx: CallContext):
        super().__init__(name="stt")
        self.ctx = ctx
        self._preroll: deque[bytes] = deque(maxlen=int(PREROLL_S / (audio.FRAME_MS / 1000)))
        self._speech: list[bytes] = []
        self._speaking = False
        self._q: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and self._worker is None:
            self._worker = self.create_task(self._work())
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._speaking = True
            self._speech = list(self._preroll)
            self.ctx.open_turn = Turn()
        elif isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._speaking:
                self._speech.append(frame.audio)
            else:
                self._preroll.append(frame.audio)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._speaking = False
            turn = self.ctx.open_turn or Turn()
            turn.vad_stop = time.perf_counter()
            pcm = b"".join(self._speech)
            self._speech = []
            if len(pcm) / 2 / audio.STT_RATE >= MIN_UTTERANCE_S:
                await self._q.put((pcm, turn))
        elif isinstance(frame, (EndFrame, CancelFrame)) and self._worker:
            await self.cancel_task(self._worker)
            self._worker = None
        await self.push_frame(frame, direction)

    async def _work(self) -> None:
        while True:
            pcm, turn = await self._q.get()
            self.ctx.stt_calls += 1
            n = self.ctx.stt_calls
            try:
                if n in self.ctx.stt_fail_calls:
                    await asyncio.sleep(0.05)
                    raise TimeoutError("injected speech to text timeout")
                t = await asyncio.wait_for(self.ctx.models.transcribe(pcm), 8.0)
            except Exception as e:  # a failed transcription is a missed answer, never a dead call
                log.warning("stt failure on utterance %d: %s", n, e)
                turn.stt_done = time.perf_counter()
                await self.push_frame(TranscriptionFrame(text="", user_id="caller", timestamp=f"fail:{type(e).__name__}"))
                self.ctx.open_turn = turn
                continue
            self.ctx.stt_seconds += t.took
            turn.stt_done = time.perf_counter()
            text = t.text
            log.info("stt #%d %.2fs audio, %.2fs to transcribe, no_speech=%.2f: %r", n, t.seconds, t.took, t.no_speech_prob, text)
            if (not text or t.no_speech_prob > 0.7 or
                    (text.lower().strip(" .!?,") in JUNK_TRANSCRIPTS and t.seconds < 1.5)):
                self.ctx.stt_dropped += 1  # a phantom word Whisper invents on noise
                continue
            turn.text = text
            self.ctx.open_turn = turn
            await self.push_frame(TranscriptionFrame(text=text, user_id="caller", timestamp=str(time.time())))


# ------------------------------------------------------------------------------------------------ brain


class Intake(FrameProcessor):
    """Runs a turn: guard before the model, read the answer, let the machine decide, guard after, then speak."""

    def __init__(self, ctx: CallContext, silence_check_s: float = 0.05):
        super().__init__(name="intake")
        self.ctx = ctx
        self._watch: asyncio.Task | None = None
        self._quiet_since: float | None = None
        self._check = silence_check_s
        self._ended = False
        self._detached: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        ctx = self.ctx
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._watch = self.create_task(self._silence_watch())
            await self._perform(ctx.machine.begin())
            return
        if isinstance(frame, UserStartedSpeakingFrame):
            self._quiet_since = None
            if ctx.bot_speaking:
                ctx.barge_ins += 1
                log.info("barge-in: caller spoke over the agent")
                ctx.outstanding.clear()
                ctx.machine._log("barge_in")
                await self.broadcast_interruption()
        elif isinstance(frame, TranscriptionFrame):
            await self._on_transcription(frame)
            return
        elif isinstance(frame, InputDTMFFrame):
            self._quiet_since = None
            await self._perform(ctx.machine.on_dtmf(frame.button.value))
            return
        elif isinstance(frame, (EndFrame, CancelFrame)) and self._watch:
            await self.cancel_task(self._watch)
            self._watch = None
        if not isinstance(frame, (TranscriptionFrame, InputDTMFFrame)):
            await self.push_frame(frame, direction)

    # ---- one turn

    async def _on_transcription(self, frame: TranscriptionFrame) -> None:
        ctx = self.ctx
        m = ctx.machine
        if m.done:
            return
        ctx.turn_busy = True
        try:
            text = frame.text.strip()
            if not text:  # speech to text failed or came back empty: a missed answer
                ctx.machine._log("stt_failure", detail=frame.timestamp)
                if m.state in (State.ASK, State.CONFIRM, State.CONSENT):
                    await self._perform(m.on_parsed(Parsed(False, reason="stt_failure")))
                return
            ctx.transcript.append({"speaker": "CALLER", "text": text, "t": time.time()})
            if text.lower().strip(" .!?,") in BACKCHANNELS and m.state == State.ASK:
                await self._perform([Say("Of course, take your time.", "prompt")])
                return
            verdict = m.screen(text)  # the guard sees the words before any model does
            if verdict.action != "allow":
                ctx.guard_events.append({"side": "input", "rule": verdict.rule, "action": verdict.action})
                await self._perform(m.on_guard(verdict))
                return
            parsed = await self._interpret(text)
            await self._perform(m.on_parsed(parsed))
        finally:
            ctx.turn_busy = False
            if ctx.open_turn:
                ctx.open_turn.decided = time.perf_counter()

    async def _interpret(self, text: str) -> Parsed:
        """The plain parser first. The model is asked only when the parser could not read it, and its answer is validated again."""
        ctx, m = self.ctx, self.ctx.machine
        p = m.parse(text)
        q = m.question
        if p.ok or m.state != State.ASK or q is None:
            return p
        cfg = ctx.tenant_cfg
        if q.id == "practice_area" and p.reason in ("no_match", "ambiguous"):
            area = await ctx.llm.classify_area(text, [a.id for a in cfg.practice_areas])
            self._mark_llm()
            if area:
                return Parsed(True, area)
        elif q.type == "name" and p.reason in ("not_a_name", "empty"):
            name = await ctx.llm.extract_name(text)
            self._mark_llm()
            if name:
                return parsers.parse_name(name)
        elif q.type == "date" and p.reason == "not_a_date":
            iso = await ctx.llm.extract_date(text, ctx.now_date.isoformat())
            self._mark_llm()
            if iso:
                return parsers.parse_date(iso, ctx.now_date, q.max_age_days)
        return p

    def _mark_llm(self) -> None:
        if self.ctx.open_turn:
            self.ctx.open_turn.llm_used = True

    # ---- doing what the machine says

    async def _perform(self, actions: list) -> None:
        ctx = self.ctx
        for i, a in enumerate(actions):
            if isinstance(a, Say):
                await self._say(a)
            elif isinstance(a, Submit):
                self._submit(a)
            else:  # StartTransfer and Hangup end the turn: run them detached so a barge-in cannot cancel them half way
                self._detached = asyncio.get_running_loop().create_task(self._terminal(actions[i:]))
                return

    def _submit(self, a: Submit) -> None:
        """Hand the finished record on. A failure here is logged and recorded, and never stops the call from ending."""
        if not self.ctx.on_submit:
            return
        try:
            self.ctx.on_submit(a.record)
        except Exception as e:
            log.exception("submit failed")
            self.ctx.machine._log("submit_error", detail=type(e).__name__)

    async def _terminal(self, actions: list) -> None:
        ctx = self.ctx
        for a in actions:
            if isinstance(a, StartTransfer):
                await self._wait_quiet()
                if ctx.on_transfer:
                    try:
                        ok, detail = await ctx.on_transfer(a.reason)
                    except Exception as e:  # a transfer that blows up is a failed transfer: the caller gets the callback line, not silence
                        log.exception("transfer failed")
                        ok, detail = False, type(e).__name__
                    await self._perform(ctx.machine.on_transfer_result(ok, detail))
                return
            if isinstance(a, Hangup):
                await self._wait_quiet()
                self._ended = True
                if ctx.on_hangup:
                    ctx.on_hangup(a.reason)
                return
            if isinstance(a, Submit):
                self._submit(a)

    async def _say(self, a: Say) -> None:
        ctx = self.ctx
        text = a.text
        if a.kind == "closing":
            ack = await self._acknowledgement()
            if ack:
                await self._speak(ack)
        v = guard.check_output(text)  # the guard sees the words after the model and before the voice
        if not v.ok:
            ctx.guard_events.append({"side": "output", "rules": list(v.rules), "blocked": text[:80]})
            ctx.machine._log("output_blocked", rules=list(v.rules))
        await self._speak(v.text)

    async def _acknowledgement(self) -> str | None:
        ctx = self.ctx
        desc = ctx.machine.fields.get("description")
        if not desc or not desc.value or ctx.machine.outcome in ("abandoned_silent",):
            return None
        ack = await ctx.llm.acknowledge(str(desc.value))
        self._mark_llm()
        if not ack:
            return None
        v = guard.check_output(ack)
        if not v.ok:
            ctx.guard_events.append({"side": "output", "rules": list(v.rules), "blocked": ack[:80]})
            return None  # an unsafe acknowledgement is dropped, not replaced
        return ack

    async def _speak(self, text: str) -> None:
        frame = TTSSpeakFrame(text=text)
        log.info("say: %s", text[:60])
        self.ctx.outstanding.add(frame.id)
        self.ctx.say_log(text)
        await self.push_frame(frame)

    async def _wait_quiet(self, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        await asyncio.sleep(0.05)
        while self.ctx.bot_speaking and time.monotonic() < end:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)

    # ---- silence

    async def _silence_watch(self) -> None:
        ctx, m = self.ctx, self.ctx.machine
        while True:
            await asyncio.sleep(self._check)
            if self._ended or m.done:
                continue
            busy = ctx.bot_speaking or ctx.user_speaking or ctx.turn_busy or m.state == State.TRANSFERRING
            if busy:
                self._quiet_since = None
                continue
            now = time.monotonic()
            if self._quiet_since is None:
                self._quiet_since = now
            elif now - self._quiet_since >= m.current_timeout_s:
                self._quiet_since = None
                await self._perform(m.on_silence_timeout())


# ------------------------------------------------------------------------------------------------ output


class Tts(FrameProcessor):
    """Speaks TTSSpeakFrames with the shared Kokoro model, pushing 8 kHz audio as each chunk is ready."""

    def __init__(self, ctx: CallContext):
        super().__init__(name="tts")
        self.ctx = ctx

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            ctx = self.ctx
            try:
                async for chunk, cached in ctx.models.stream_synthesis(frame.text, ctx.voice):
                    if frame.id not in ctx.outstanding:
                        break  # interrupted while synthesising
                    if ctx.open_turn and ctx.open_turn.first_audio == 0.0:
                        ctx.open_turn.tts_cached = cached
                    ctx.tts_seconds += len(chunk) / 2 / audio.TELEPHONE_RATE
                    await self.push_frame(TTSAudioRawFrame(audio=chunk, sample_rate=audio.TELEPHONE_RATE, num_channels=1))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tts failed for %r", frame.text[:40])
            finally:
                ctx.outstanding.discard(frame.id)
            return
        await self.push_frame(frame, direction)


class RtpOutput(FrameProcessor):
    """Hands agent audio to the RTP sender. An interruption empties what has not gone out yet."""

    def __init__(self, ctx: CallContext):
        super().__init__(name="rtp-out")
        self.ctx = ctx

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        ctx = self.ctx
        if isinstance(frame, InterruptionFrame):
            ctx.rtp.clear_tx()
        elif isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
            if ctx.open_turn and ctx.open_turn.first_audio == 0.0:
                ctx.open_turn.first_audio = time.perf_counter()
                ctx.turns.append(ctx.open_turn)
                ctx.open_turn = None
            ctx.agent_audio_seconds += len(frame.audio) / 2 / audio.TELEPHONE_RATE
            ctx.rtp.send_linear(frame.audio)
            return
        await self.push_frame(frame, direction)
