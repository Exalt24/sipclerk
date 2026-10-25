"""The desk leg of a transfer never outlives the call that started it.

A call session with a real SIP dialog to a softphone caller, a real second call to a stand-in desk phone, and nothing else: no speech models and
no voice pipeline, so every stage of the transfer can be hit exactly. The same thing through the whole line is in test_pipeline_fast.py."""
import asyncio
import socket
import time

import pytest

from conftest import IN_HOURS
from fake_models import FakeModels, speech_tone
from sipclerk import audio
from sipclerk.call import CallSession, Faults
from sipclerk.harness import make_registry
from sipclerk.sip import message as sm
from sipclerk.sip.phone import SoftPhone, StaffEndpoint, tone
from sipclerk.sip.rtp import RtpSession
from sipclerk.sip.stack import SipStack
from sipclerk.voice.llm import Llm

ASH = "+14155550101"


class LongWhisper(FakeModels):
    """A whisper that takes 20 seconds, so the caller can hang up in the middle of it."""

    async def synthesize(self, text, voice):
        return tone(330, 20.0, amp=3500), True


class Rig:
    def __init__(self, staff_mode="answer", answer_delay=0.05, models=None):
        self.staff_mode, self.answer_delay, self.models = staff_mode, answer_delay, models or FakeModels()
        self.sessions: list[CallSession] = []
        self.opened: list[RtpSession] = []

    async def __aenter__(self):
        self.tasks_before = asyncio.all_tasks()
        real_open = RtpSession.open.__func__

        async def recording_open(cls, *a, **kw):
            s = await real_open(cls, *a, **kw)
            self.opened.append(s)
            return s

        self._patched = (RtpSession, real_open)
        RtpSession.open = classmethod(recording_open)
        self.staff_stack, self.line_stack, self.caller_stack = (SipStack("127.0.0.1", 0) for _ in range(3))
        for s in (self.staff_stack, self.line_stack, self.caller_stack):
            await s.start()
        self.staff = StaffEndpoint(self.staff_stack, mode=self.staff_mode, answer_delay=self.answer_delay)
        self.registry = make_registry({"ashgrovepell": self.staff_stack.port, "tidewaterquint": self.staff_stack.port}, 1.0, ring_timeout_s=8)
        self.tenant = self.registry.by_id("ashgrovepell")
        self.line_stack.on_invite = self._on_invite
        return self

    async def _on_invite(self, inv):
        offer = sm.parse_sdp(inv.offer)
        codec, _ = sm.negotiate(offer, [audio.PCMU, audio.PCMA])
        rtp = await RtpSession.open("127.0.0.1", 0, codec, offer.dtmf_pt)
        rtp.set_remote(offer.host, offer.port)
        dialog = await inv.answer(sm.build_sdp("127.0.0.1", rtp.local_port, [codec], offer.dtmf_pt), local_host="127.0.0.1")
        await asyncio.wait_for(inv.acked.wait(), 5)
        rtp.start()
        session = CallSession(server=None, tenant=self.tenant, dialog=dialog, rtp=rtp, codec=codec, caller=inv.caller, call_id=f"unit{len(self.sessions)}",
                              now_utc=IN_HOURS, models=self.models, llm=Llm(fail=True), stack=self.line_stack, faults=Faults())
        session.watcher = asyncio.get_running_loop().create_task(session._watch_caller_bye())
        self.sessions.append(session)

    async def call(self):
        call, status = await SoftPhone(self.caller_stack).call(f"sip:{ASH}@127.0.0.1:{self.line_stack.port}", from_user="+14155550190", timeout=5)
        assert status == 200
        while not self.sessions:
            await asyncio.sleep(0.01)
        return call, self.sessions[-1]

    async def wait(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return predicate()

    async def end_of_call(self, session):
        """What CallSession.run() does when the call finishes."""
        await asyncio.wait_for(session.finished.wait(), 5)
        session.watcher.cancel()
        await session._end_transfer()
        await session.rtp.close()

    async def __aexit__(self, *exc):
        for c in self.staff.calls:
            await c.rtp.close()  # the desk phone's own media, not the line's
        await self.caller_stack.stop()
        await self.line_stack.stop()
        await self.staff_stack.stop()
        cls, real_open = self._patched
        cls.open = classmethod(real_open)

    def leaks(self):
        """Media sockets of the line that are still open, and tasks that outlived the call."""
        staff_rtp = {id(c.rtp) for c in self.staff.calls}
        sockets = [s for s in self.opened if id(s) not in staff_rtp and not s.transport.is_closing()]
        desk_phone_tickers = {c.rtp._ticker for c in self.staff.calls}  # the stand-in desk phone's own media, closed when the test ends
        tasks = [t for t in asyncio.all_tasks() - self.tasks_before if not t.done() and t is not asyncio.current_task() and t not in desk_phone_tickers]
        return sockets, tasks


async def test_the_caller_hanging_up_while_the_desk_phone_rings_cancels_the_desk_invite():
    async with Rig(answer_delay=3.0) as r:
        call, session = await r.call()
        transfer = asyncio.get_running_loop().create_task(session._transfer("dtmf_zero"))
        assert await r.wait(lambda: r.staff.invites)
        await asyncio.sleep(0.3)
        await call.hangup()
        await r.end_of_call(session)
        assert r.staff.invites[-1].cancelled.is_set(), "the line never cancelled the desk INVITE"
        await asyncio.sleep(3.2)  # past the moment the desk phone would have answered
        assert r.staff.calls == [], "the desk phone answered a call that was already over"
        assert transfer.cancelled() and session.transfer_info["result"] == "cancelled"
        sockets, tasks = r.leaks()
        assert not sockets and not tasks, (sockets, tasks)


async def test_the_caller_hanging_up_while_the_desk_phone_hears_the_whisper_ends_the_desk_call_and_its_media():
    async with Rig(answer_delay=0.05, models=LongWhisper()) as r:
        call, session = await r.call()
        transfer = asyncio.get_running_loop().create_task(session._transfer("dtmf_zero"))
        assert await r.wait(lambda: r.staff.calls and len(r.staff.calls[-1].frames) > 10)  # the whisper is playing
        desk = r.staff.calls[-1]
        await call.hangup()
        await r.end_of_call(session)
        assert await r.wait(lambda: desk.dialog.terminated.is_set(), 2.0), "the desk phone was never hung up on"
        assert desk.dialog.ended_by == "remote"
        before = desk.rtp.packets_in
        await asyncio.sleep(1.0)
        assert desk.rtp.packets_in == before, "RTP kept flowing to the desk phone after the call ended"
        assert transfer.cancelled()
        sockets, tasks = r.leaks()
        assert not sockets and not tasks, (sockets, tasks)


async def test_after_the_bridge_the_callers_hang_up_still_hangs_up_the_desk_phone():
    async with Rig(answer_delay=0.05) as r:
        call, session = await r.call()
        ok, detail = await asyncio.wait_for(session._transfer("dtmf_zero"), 20)
        assert ok and session.bridged_at is not None
        desk = r.staff.calls[-1]
        await call.hangup()
        await r.end_of_call(session)
        assert session.ended_by == "caller" and await r.wait(lambda: desk.dialog.terminated.is_set(), 2.0)
        await asyncio.sleep(0.3)
        sockets, tasks = r.leaks()
        assert not sockets and not tasks, (sockets, tasks)


@pytest.mark.parametrize("stage,delay", [("invite just sent", 0.0), ("ringing", 0.4), ("answered, whisper starting", None), ("whisper", None), ("bridged", None)])
async def test_class_the_caller_hanging_up_at_every_stage_leaves_no_desk_call_media_socket_or_task(stage, delay):
    """The class: a transfer leg that outlives the call that started it. Whatever the stage, afterwards no desk call is up, nothing is sent
    to the desk, the line's sockets are closed and its tasks are gone."""
    slow = stage in ("invite just sent", "ringing")
    async with Rig(answer_delay=2.0 if slow else 0.05, models=LongWhisper() if stage.startswith("whisper") or stage.startswith("answered") else None) as r:
        call, session = await r.call()
        transfer = asyncio.get_running_loop().create_task(session._transfer("dtmf_zero"))
        if stage == "bridged":
            await asyncio.wait_for(asyncio.shield(transfer), 20)
        elif stage.startswith("whisper"):
            assert await r.wait(lambda: r.staff.calls and len(r.staff.calls[-1].frames) > 10)
        elif stage.startswith("answered"):
            assert await r.wait(lambda: r.staff.calls)
        else:
            assert await r.wait(lambda: r.staff.invites)
            await asyncio.sleep(delay)
        await call.hangup()
        await r.end_of_call(session)
        await asyncio.sleep(2.3 if slow else 0.5)
        assert all(c.dialog.terminated.is_set() for c in r.staff.calls), "a desk call is still up"
        for c in r.staff.calls:
            before = c.rtp.packets_in
            await asyncio.sleep(0.4)
            assert c.rtp.packets_in == before, "media still arriving at the desk phone"
        if slow:
            assert r.staff.invites[-1].cancelled.is_set() and r.staff.calls == []
        sockets, tasks = r.leaks()
        assert not sockets and not tasks, (stage, sockets, tasks)


async def test_a_2xx_that_crosses_our_cancel_is_acknowledged_and_hung_up():
    """The far end answers at the moment we cancel: it must not be left with a call nobody wants."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.setblocking(False)
    stack = SipStack("127.0.0.1", 0)
    await stack.start()
    seen: list[str] = []

    async def desk():
        loop = asyncio.get_running_loop()
        invite = None
        while True:
            data, addr = await loop.sock_recvfrom(server, 4096)
            msg = sm.parse(data)
            seen.append(msg.method)
            reply = lambda code, reason, req, body="": sm.make_response(req, code, reason, to_tag="desk1", contact=f"sip:desk@127.0.0.1:{server.getsockname()[1]}", body=body)  # noqa: E731
            if msg.method == "INVITE":
                invite = msg
                server.sendto(reply(180, "Ringing", msg).encode(), addr)
            elif msg.method == "CANCEL":
                server.sendto(sm.make_response(msg, 200, "OK").encode(), addr)
                server.sendto(reply(200, "OK", invite, sm.build_sdp("127.0.0.1", 40990, [audio.PCMU])).encode(), addr)  # the answer crossed the CANCEL
            elif msg.method == "BYE":
                server.sendto(sm.make_response(msg, 200, "OK").encode(), addr)
                return

    desk_task = asyncio.get_running_loop().create_task(desk())
    try:
        inv = asyncio.get_running_loop().create_task(stack.invite(f"sip:desk@127.0.0.1:{server.getsockname()[1]}", "sip:line@127.0.0.1",
                                                                   sm.build_sdp("127.0.0.1", 40991, [audio.PCMU]), timeout=10))
        while "INVITE" not in seen:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        inv.cancel()
        with pytest.raises(asyncio.CancelledError):
            await inv
        await asyncio.wait_for(desk_task, 5)
        assert seen[:2] == ["INVITE", "CANCEL"] and "ACK" in seen and seen[-1] == "BYE"
        assert not stack.dialogs, "a dialog for the call nobody wanted is still held"
    finally:
        desk_task.cancel()
        server.close()
        await stack.stop()
