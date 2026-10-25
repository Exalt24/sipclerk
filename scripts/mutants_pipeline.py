"""Mutants for the pipeline and the line: real SIP, RTP and voice activity detection with stand-in speech models.

Kept apart from scripts/mutants.py because each one runs a few real-time calls (seconds each, not milliseconds).
"""
from mutants import Mutant, PIPE_FAST

PROC = "sipclerk/voice/processors.py"
CALL = "sipclerk/call.py"
SERVER = "sipclerk/server.py"


def P(id, file, old, new, desc, k):
    return Mutant(id, file, old, new, [*PIPE_FAST, "tests/test_processors.py", "-k", k], desc, "replace", "pipeline")


PIPELINE_MUTANTS = [
    P("pipe-barge-in", PROC, "            if ctx.bot_speaking:\n                ctx.barge_ins += 1", "            if False:\n                ctx.barge_ins += 1",
      "talking over the agent does not interrupt it", "barge_in"),
    P("pipe-clear-queue", PROC, "        if isinstance(frame, InterruptionFrame):\n            ctx.rtp.clear_tx()", "        if isinstance(frame, InterruptionFrame):\n            pass",
      "an interruption leaves the agent's queued speech in the RTP sender", "barge_in"),
    P("pipe-silence-timer", PROC, "            elif now - self._quiet_since >= m.current_timeout_s:", "            elif now - self._quiet_since >= m.current_timeout_s * 1000:",
      "a silent caller is never re-prompted", "silence"),
    P("pipe-dtmf-forward", PROC, "            await self._perform(ctx.machine.on_dtmf(frame.button.value))", "            pass",
      "keypad digits never reach the state machine", "pressing_zero or digit_pound"),
    P("pipe-dtmf-hook", PROC, "            self.ctx.rtp.on_dtmf = lambda d: loop.create_task(self._dtmf(d))", "            pass",
      "RFC 4733 events are not turned into keypad frames", "pressing_zero or digit_pound"),
    P("pipe-stt-failure", PROC, '                    await self._perform(m.on_parsed(Parsed(False, reason="stt_failure")))', "                    pass",
      "a failed transcription is silently dropped", "failed_stt or empty_transcription"),
    P("pipe-recording-gate", CALL, "        if self.machine.recording:\n            self._rec_in.append(pcm)", "        if True:\n            self._rec_in.append(pcm)",
      "caller audio is kept without consent", "recording_is_kept"),
    P("pipe-whisper", CALL, "        rtp2.send_linear(pcm)\n        end = time.monotonic() + 30", "        end = time.monotonic() + 30",
      "staff get no whisper before the bridge opens", "pressing_zero"),
    P("pipe-bridge", CALL, "        self.rtp.bridge_to, rtp2.bridge_to = rtp2, self.rtp", "        pass", "the two legs are never bridged", "pressing_zero"),
    P("pipe-desk-hangup", CALL, '            self.ended_by = "staff"\n            await self.dialog.bye()', '            self.ended_by = "staff"',
      "the caller stays on the line after the desk phone hangs up", "desk_phone_hanging_up"),
    P("pipe-agent-bye", CALL, '        self.ended_by = self.ended_by or f"agent:{reason}"\n        await self.dialog.bye()', '        self.ended_by = self.ended_by or f"agent:{reason}"',
      "the agent never hangs up", "silence"),
    P("pipe-blocked-caller", SERVER, '        if v.action == "blocked_caller":', "        if False:", "a blocked number is answered", "blocked_caller"),
    P("pipe-wrong-number", SERVER, "        if tenant is None:  # a wrong number", "        if False:  # a wrong number", "a wrong number loads a tenant", "dialled_number"),
    P("pipe-capacity", SERVER, "        if len(self.sessions) >= self.max_calls:", "        if False:", "there is no limit on simultaneous calls", "beyond_the_limit"),
    P("pipe-no-codec", SERVER, '        if pick is None:\n            self.counters["no_codec"] += 1\n            inv.reject(488',
      '        if False:\n            self.counters["no_codec"] += 1\n            inv.reject(488', "a call with no common codec is answered", "no_codec_we_speak"),
]
