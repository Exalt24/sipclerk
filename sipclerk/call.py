"""One live call: the SIP dialog, its RTP leg, the Pipecat pipeline, the transfer and the saved record."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

from sipclerk import audio, delivery, guard
from sipclerk.intake.machine import IntakeMachine
from sipclerk.sip import message as sm
from sipclerk.sip.phone import tone
from sipclerk.sip.rtp import RtpSession
from sipclerk.sip.stack import Dialog, SipStack
from sipclerk.tenants import LoadedTenant
from sipclerk.voice import processors as vp
from sipclerk.voice.llm import Llm
from sipclerk.voice.models import Models

log = logging.getLogger("sipclerk.call")

RINGBACK_ON_S, RINGBACK_OFF_S = 1.0, 2.0


@dataclass
class CallResult:
    record: dict
    transcript: list[dict]
    summary: str
    recording_bytes: int
    metrics: dict


@dataclass
class Faults:
    """Provider failures a test can inject. All off by default."""
    stt_fail_calls: set = field(default_factory=set)
    llm_fail: bool = False


class CallSession:
    def __init__(self, *, server, tenant: LoadedTenant, dialog: Dialog, rtp: RtpSession, codec: str, caller: str, call_id: str,
                 now_utc: datetime, models: Models, llm: Llm, stack: SipStack, faults: Faults):
        self.server = server
        self.tenant = tenant
        self.cfg = tenant.config
        self.dialog = dialog
        self.rtp = rtp
        self.codec = codec
        self.caller = caller
        self.call_id = call_id
        self.models = models
        self.llm = llm
        self.stack = stack
        self.faults = faults
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.machine = IntakeMachine(tenant, call_id, caller, now_utc)
        self.ctx = vp.CallContext(call_id, self.cfg, self.machine, models, llm, rtp, self.cfg.voice, self.machine.today)
        self.ctx.stt_fail_calls = set(faults.stt_fail_calls)
        self.ctx.on_submit = self._on_submit
        self.ctx.on_hangup = lambda reason: asyncio.get_running_loop().create_task(self._hang_up(reason))
        self.ctx.on_transfer = self._transfer
        self.finished = asyncio.Event()
        self.ended_by = ""
        self.task: PipelineTask | None = None
        self._rec_in: list[bytes] = []
        self._rec_out: list[bytes] = []
        self.staff: tuple[Dialog, RtpSession] | None = None
        self.transfer_info: dict = {}
        self.bridged_at: float | None = None
        self.submitted_key: str | None = None
        rtp.on_frame = self._tap_in
        rtp.on_tx = self._tap_out

    # ---- recording taps (only while consent allows)

    def _tap_in(self, pcm: bytes) -> None:
        if self.machine.recording:
            self._rec_in.append(pcm)

    def _tap_out(self, payload: bytes) -> None:
        if self.machine.recording:
            self._rec_out.append(audio.decode(payload, self.rtp.codec))

    def recording_pcm(self) -> bytes | None:
        if not (self._rec_in or self._rec_out) or not self.machine.recording:
            return None
        n = max(len(self._rec_in), len(self._rec_out))
        zero = bytes(audio.FRAME_SAMPLES * 2)
        a = np.frombuffer(b"".join(self._rec_in + [zero] * (n - len(self._rec_in))), dtype=np.int16).astype(np.int32)
        b = np.frombuffer(b"".join(self._rec_out + [zero] * (n - len(self._rec_out))), dtype=np.int16).astype(np.int32)
        return np.clip(a + b, -32768, 32767).astype(np.int16).tobytes()

    # ---- machine callbacks

    def _on_submit(self, record: dict) -> None:
        """The intake is complete: queue the case-management delivery now, while the closing line is still being spoken."""
        self.submitted_key = record["idempotency_key"]
        if self.cfg.integrations.case_management:
            self.server.outbox.enqueue("bridge", record["idempotency_key"], delivery.build_bridge_payload(record, ""),
                                       tenant_id=self.cfg.tenant_id, call_id=self.call_id)
            asyncio.get_running_loop().create_task(self.server.outbox.deliver_due())

    async def _hang_up(self, reason: str) -> None:
        self.ended_by = self.ended_by or f"agent:{reason}"
        await self.dialog.bye()
        self.finished.set()

    async def _transfer(self, reason: str) -> tuple[bool, str]:
        t = self.cfg.transfer
        info = self.transfer_info = {"attempted": True, "reason": reason, "destination_alias": f"{self.cfg.tenant_id}:desk"}
        t0 = time.monotonic()
        rtp2 = await RtpSession.open("0.0.0.0", 0, audio.PCMU, 101)
        host, port = sm.uri_hostport(t.destination)
        adv = self.stack.local_host_for((host, port))
        stop = asyncio.Event()
        ring = asyncio.get_running_loop().create_task(self._ringback(stop))
        try:
            res = await self.stack.invite(t.destination, f"sip:{self.cfg.did}@{adv}", sm.build_sdp(adv, rtp2.local_port, [audio.PCMU, audio.PCMA], 101),
                                          timeout=t.ring_timeout_s)
        finally:
            stop.set()
            await ring
        info["ring_s"] = round(time.monotonic() - t0, 2)
        if res.dialog is None:
            await rtp2.close()
            info.update(result="failed", detail=f"{res.status} {res.reason}")
            return False, f"{res.status} {res.reason}"
        ans = sm.parse_sdp(res.answer_sdp)
        rtp2.codec, rtp2.pt, rtp2.dtmf_pt = ans.codec_names[0], audio.CODEC_PAYLOAD_TYPES[ans.codec_names[0]], ans.dtmf_pt
        rtp2.set_remote(ans.host, ans.port)
        rtp2.start()
        self.staff = (res.dialog, rtp2)
        whisper = t.whisper.format(summary=self._whisper_summary(reason))
        v = guard.check_output(whisper)
        info["whisper"] = v.text
        pcm, _ = await self.models.synthesize(v.text, self.cfg.voice)
        rtp2.send_linear(pcm)
        end = time.monotonic() + 30
        while rtp2.tx_backlog_frames and time.monotonic() < end:
            await asyncio.sleep(0.02)
        self.rtp.clear_tx()
        self.rtp.bridge_to, rtp2.bridge_to = rtp2, self.rtp
        self.bridged_at = time.monotonic()
        info.update(result="connected", detail="answered", codec_staff_leg=rtp2.codec)
        loop = asyncio.get_running_loop()
        loop.create_task(self._stop_pipeline())
        loop.create_task(self._watch_bridge())
        return True, "answered"

    def _whisper_summary(self, reason: str) -> str:
        f = self.machine.fields
        name = f["full_name"].value
        area = f["practice_area"].value
        bits = [f"Caller {name}." if name else "Caller name not captured."]
        if area:
            bits.append(f"Matter type {self.cfg.area(str(area)).label if self.cfg.area(str(area)) else area}.")
        bits.append("Reason for transfer: " + reason.replace("_", " ").split(":")[0] + ".")
        return " ".join(bits)

    async def _ringback(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.rtp.send_linear(tone(440, RINGBACK_ON_S, amp=3000))
            try:
                await asyncio.wait_for(stop.wait(), RINGBACK_ON_S + RINGBACK_OFF_S)
            except asyncio.TimeoutError:
                pass
        self.rtp.clear_tx()

    async def _stop_pipeline(self) -> None:
        if self.task:
            await self.task.cancel()

    async def _watch_bridge(self) -> None:
        d_staff, _ = self.staff
        waiters = [asyncio.ensure_future(self.dialog.terminated.wait()), asyncio.ensure_future(d_staff.terminated.wait())]
        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        if self.dialog.terminated.is_set():
            self.ended_by = "caller"
            await d_staff.bye()
        else:
            self.ended_by = "staff"
            await self.dialog.bye()
        self.finished.set()

    # ---- run

    async def run(self) -> CallResult:
        pipeline = Pipeline([vp.RtpInput(self.ctx), vp.Vad(self.ctx), vp.Stt(self.ctx), vp.Intake(self.ctx), vp.Tts(self.ctx),
                             vp.RtpOutput(self.ctx)])
        self.task = PipelineTask(pipeline, params=PipelineParams(audio_in_sample_rate=audio.STT_RATE, audio_out_sample_rate=audio.TELEPHONE_RATE),
                                 idle_timeout_secs=None, cancel_on_idle_timeout=False, enable_rtvi=False, check_dangling_tasks=False)
        runner = PipelineRunner(handle_sigint=False, check_dangling_tasks=False)
        run = asyncio.get_running_loop().create_task(runner.run(self.task))
        watcher = asyncio.get_running_loop().create_task(self._watch_caller_bye())
        try:
            await self.finished.wait()
            log.info("call %s finished (ended_by=%s)", self.call_id, self.ended_by)
        finally:
            watcher.cancel()
            try:
                await self.task.cancel()
            except Exception:
                pass
            try:
                await asyncio.wait_for(run, 5)
            except Exception:
                run.cancel()
            await self.rtp.close()
            if self.staff:
                await self.staff[1].close()
        return await self._finalize()

    async def _watch_caller_bye(self) -> None:
        await self.dialog.terminated.wait()
        if self.staff is None:  # when bridged, _watch_bridge owns the hang-up
            self.ended_by = self.ended_by or "caller"
            self.finished.set()

    # ---- after the call

    async def _finalize(self) -> CallResult:
        m = self.machine
        duration = time.monotonic() - self.started
        if m.state.value != "done" and not m.outcome:
            m.outcome = "caller_hung_up"
            if not m.submitted:
                m.tasks.append({"kind": "callback", "reason": "caller hung up before the intake finished"})
        record = m.record()
        record.update({
            "ended_by": self.ended_by,
            "duration_s": round(duration, 1),
            "codec": self.codec,
            "transfer": {**m.transfer, **self.transfer_info},
            "guard_events": self.ctx.guard_events,
            "barge_ins": self.ctx.barge_ins,
            "usage": {"llm": self.llm.usage.as_dict(), "stt_audio_s": round(self.ctx.stt_seconds, 2), "stt_calls": self.ctx.stt_calls,
                      "stt_dropped": self.ctx.stt_dropped, "rx_catch_up_frames": self.ctx.catch_up_frames, "tts_audio_s": round(self.ctx.tts_seconds, 2),
                      "caller_audio_s": round(self.ctx.caller_audio_seconds, 1), "agent_audio_s": round(self.ctx.agent_audio_seconds, 1)},
            "latency_agent_side_ms": [self._turn_ms(t) for t in self.ctx.turns],
            "rtp": self.rtp.stats(),
        })
        transcript = [{"speaker": t["speaker"], "text": t["text"], "t": round(t["t"] - self.started_wall, 2)} for t in self.ctx.transcript]
        summary = await self._summary(record)
        rec_pcm = self.recording_pcm()
        parts = self.server.store.save_call(self.cfg.tenant_id, self.call_id, record, transcript, summary, rec_pcm)
        if self.cfg.integrations.call_console:
            payload = delivery.build_console_payload(record, transcript, int(duration), self.cfg.display_name)
            self.server.outbox.enqueue("console", record["idempotency_key"], payload, tenant_id=self.cfg.tenant_id, call_id=self.call_id)
        asyncio.get_running_loop().create_task(self.server.outbox.deliver_due())
        return CallResult(record, transcript, summary, len(rec_pcm or b""), {"saved_parts": parts})

    @staticmethod
    def _turn_ms(t: vp.Turn) -> dict:
        return {"endpointed_to_text": round((t.stt_done - t.vad_stop) * 1000), "text_to_audio": round((t.first_audio - t.stt_done) * 1000),
                "vad_stop_to_audio": round((t.first_audio - t.vad_stop) * 1000), "tts_cached": t.tts_cached, "llm_used": t.llm_used}

    async def _summary(self, record: dict) -> str:
        f = {k: v["value"] for k, v in record["fields"].items() if v["value"] is not None}
        facts = {"firm": self.cfg.display_name, "outcome": record["outcome"], "fields": f, "qualification": record["qualification"].get("result", ""),
                 "tasks": [t["kind"] for t in record["tasks"]]}
        fallback = (f"{f.get('full_name', 'An unnamed caller')} called {self.cfg.display_name}"
                    + (f" about {self.cfg.area(str(f['practice_area'])).label}" if f.get("practice_area") and self.cfg.area(str(f["practice_area"])) else "")
                    + f". Outcome: {record['outcome'].replace('_', ' ')}.")
        text = await self.llm.summarize(facts)
        if text and guard.check_output(text).ok:
            return text
        if text:
            record["guard_events"].append({"side": "output", "rules": list(guard.check_output(text).rules), "blocked": "summary"})
        return fallback
