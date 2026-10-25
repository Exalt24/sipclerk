"""The whole line with real SIP, RTP, voice activity detection and state machine, and stand-in speech models.

Fast enough to run on any machine and in CI (a few seconds of real time per call). The voice suite in
test_voice_suite.py replaces the stand-ins with Whisper, Kokoro and the language model.
"""
import asyncio
import logging
import time

import pytest

from fake_models import FakeModels
from sipclerk import audio
from sipclerk.harness import WAV_DIR, read_wav_8k, wav_name
from sipclerk.labrun import LabRig
from sipclerk.sip import message as sm
from sipclerk.sip.phone import SoftPhone, goertzel_power, tone
from sipclerk.store import Actor
from sipclerk.voice.llm import Llm

KEY = bytes(range(32)) * 2
ASH, TIDE = "+14155550101", "+13055550102"


def clip(text="Yes, that is fine.", voice="am_adam"):
    return read_wav_8k(WAV_DIR / wav_name(voice, text)) + bytes(800)


def rig(script, **kw):
    return LabRig(FakeModels(script), master_key=KEY, llm_factory=lambda: Llm(fail=True), timeout_scale=kw.pop("timeout_scale", 0.4), **kw)


async def place(r, did=ASH, caller="+14155550190", timeout=8):
    call, status = await SoftPhone(r.caller_stack).call(f"sip:{did}@127.0.0.1:{r.server.port}", from_user=caller, timeout=timeout)
    return call, status


async def finished(r, caller, wait=15):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        hit = [x for x in r.results if x.record["caller_number"] == caller]
        if hit:
            return hit[0]
        await asyncio.sleep(0.1)
    raise AssertionError("no call record was saved")


async def agent_done(call, quiet=1.2):
    await call.wait_for_speech(10, since=call.started)
    assert await call.wait_for_quiet(quiet, 30)


# ----------------------------------------------------------------------------------------------- routing


async def test_the_dialled_number_picks_the_tenant_and_other_numbers_get_404():
    async with rig(["no"]) as r:
        call, status = await place(r, "+19995550000")
        assert call is None and status == 404 and r.server.counters["wrong_number"] == 1
        call, status = await place(r, TIDE, "+17865550191")
        assert status == 200
        await agent_done(call)
        await call.hangup()
        res = await finished(r, "+17865550191")
        assert res.record["tenant_id"] == "tidewaterquint" and res.record["did"] == TIDE


async def test_a_blocked_caller_is_declined_and_no_session_starts():
    async with rig([]) as r:
        call, status = await place(r, ASH, "+15550000001")
        assert call is None and status == 603 and r.server.counters["blocked_caller"] == 1 and not r.server.sessions


async def test_the_same_caller_cannot_hold_two_calls_on_one_line():
    async with rig(["yes"]) as r:
        first, s1 = await place(r, ASH, "+14155550192")
        assert s1 == 200
        second, s2 = await place(r, ASH, "+14155550192")
        assert second is None and s2 == 486 and r.server.counters["duplicate_call"] == 1
        other, s3 = await place(r, TIDE, "+14155550192")  # the same number on the other firm's line is a different call
        assert s3 == 200
        await first.hangup()
        await other.hangup()


async def test_calls_beyond_the_limit_get_503_and_a_call_with_no_common_codec_gets_488():
    async with rig([]) as r:
        r.server.max_calls = 1
        a, s = await place(r, ASH, "+14155550193")
        assert s == 200
        b, s = await place(r, TIDE, "+14155550194")
        assert b is None and s == 503 and r.server.counters["busy"] == 1
        await a.hangup()
        await asyncio.sleep(0.3)
        phone = SoftPhone(r.caller_stack)
        c, s = await phone.call(f"sip:{ASH}@127.0.0.1:{r.server.port}", from_user="+14155550195", codecs=[audio.PCMA, audio.PCMU], timeout=5)
        assert s == 200 and c.codec == "PCMU"  # the line prefers PCMU even though the caller offered PCMA first
        await c.hangup()


# ----------------------------------------------------------------------------------------------- behaviour


async def test_a_call_that_offers_no_codec_we_speak_is_refused_with_488():
    async with rig([]) as r:
        sdp = "\r\n".join(["v=0", "o=x 1 1 IN IP4 127.0.0.1", "s=x", "c=IN IP4 127.0.0.1", "t=0 0", "m=audio 4000 RTP/AVP 9", "a=rtpmap:9 G722/8000", ""])
        res = await r.caller_stack.invite(f"sip:{ASH}@127.0.0.1:{r.server.port}", "sip:+14155550179@127.0.0.1", sdp, timeout=4)
        assert res.status == 488 and res.dialog is None and r.server.counters["no_codec"] == 1 and not r.server.sessions


async def test_barge_in_silences_the_agent_within_a_second():
    async with rig(["yes that is fine"]) as r:
        call, _ = await place(r)
        await call.wait_for_speech(10, since=call.started)
        await asyncio.sleep(0.8)
        t0 = time.monotonic()
        call.say(clip())
        await asyncio.sleep(3.0)
        act = call.active_times(t0)
        stop = act[-1] - t0
        for a, b in zip(act, act[1:]):  # the first silent gap is where the agent stopped; what follows is its answer to us
            if b - a > 0.3:
                stop = a - t0
                break
        # the agent's long greeting would run for several more seconds; the tone must stop soon after we start talking
        assert stop < 1.6, stop
        await call.hangup()
        res = await finished(r, "+14155550190")
        assert res.record["barge_ins"] >= 1 and any(e["kind"] == "barge_in" for e in res.record["events"])


async def test_silence_is_reprompted_and_then_the_call_is_ended_with_a_callback():
    async with rig([], timeout_scale=0.3) as r:
        call, _ = await place(r)
        res = await finished(r, "+14155550190", wait=60)
        rec = res.record
        assert rec["outcome"] == "abandoned_silent" and rec["tasks"][-1]["kind"] == "callback" and rec["ended_by"] == "agent:silence"
        lines = [t["text"] for t in res.transcript if t["speaker"] == "AGENT"]
        assert sum(t.startswith("Are you still there?") or "Is it all right if the firm records" in t for t in lines) >= 2
        assert call.dialog.terminated.is_set()


async def test_recording_is_kept_only_when_the_caller_agreed():
    async with rig(["yes"]) as r:
        call, _ = await place(r, ASH, "+14155550196")
        await agent_done(call)
        call.say(clip())
        await asyncio.sleep(3)
        await call.hangup()
        yes = await finished(r, "+14155550196")
    assert yes.record["recording"] is True and yes.metrics["saved_parts"].count("recording") == 1 and yes.recording_bytes > 8000
    # the greeting and the consent question come before the yes: none of that audio may be in the recording
    assert yes.recording_bytes / 16000 <= yes.record["duration_s"] - 4.0, (yes.recording_bytes / 16000, yes.record["duration_s"])
    async with rig(["no"]) as r:
        call, _ = await place(r, ASH, "+14155550197")
        await agent_done(call)
        call.say(clip("No, I am not."))
        await asyncio.sleep(3)
        await call.hangup()
        no = await finished(r, "+14155550197")
        assert no.record["recording"] is False and "recording" not in no.metrics["saved_parts"] and no.recording_bytes == 0
        assert any(e["kind"] == "recording_consent" and e["granted"] is False for e in no.record["events"])


async def test_a_notice_state_records_from_the_start_without_asking():
    async with rig(["Dana Whitfield"]) as r:
        call, _ = await place(r, TIDE, "+17865550198")
        await agent_done(call)
        await asyncio.sleep(0.5)
        await call.hangup()
        res = await finished(r, "+17865550198")
        assert res.record["recording"] is True and not any(e["kind"] == "recording_consent" for e in res.record["events"])


async def test_pressing_zero_transfers_and_the_two_legs_are_bridged_with_a_whisper_first():
    async with rig([]) as r:
        call, _ = await place(r, TIDE, "+17865550199")
        await agent_done(call)
        await call.dtmf("0")
        end = time.monotonic() + 15
        staff = r.staff["tidewaterquint"]
        while not staff.calls and time.monotonic() < end:
            await asyncio.sleep(0.05)
        assert staff.calls, "the desk phone never rang"
        s = staff.calls[-1]
        await s.wait_for_speech(20, since=s.started)
        await s.wait_for_quiet(1.2, 20)
        t0 = time.monotonic()
        assert sum(1 for t, f in s.frames if audio.rms(f) > 350) > 40  # the whisper reached the desk phone
        call.say(tone(1000, 0.8, amp=6000))
        await asyncio.sleep(1.2)
        s.say(tone(880, 0.8, amp=6000))
        await asyncio.sleep(1.6)
        heard_staff, heard_caller = s.heard_pcm(t0), call.heard_pcm(t0 + 1.0)
        assert goertzel_power(heard_staff, 1000) > 20 * goertzel_power(heard_staff, 2500)
        assert goertzel_power(heard_caller, 880) > 20 * goertzel_power(heard_caller, 2500)
        await call.hangup()
        res = await finished(r, "+17865550199")
        assert res.record["transfer"]["result"] == "connected" and res.record["escalation"] == "dtmf_zero" and res.record["ended_by"] == "caller"
        assert s.dialog.terminated.is_set() or await asyncio.wait_for(s.dialog.terminated.wait(), 3)


async def test_the_desk_phone_hanging_up_ends_the_callers_call_too():
    async with rig([]) as r:
        call, _ = await place(r, TIDE, "+17865550180")
        await agent_done(call)
        await call.dtmf("0")
        staff = r.staff["tidewaterquint"]
        end = time.monotonic() + 15
        while not staff.calls and time.monotonic() < end:
            await asyncio.sleep(0.05)
        await staff.calls[-1].wait_for_speech(20, since=staff.calls[-1].started)
        await staff.calls[-1].wait_for_quiet(1.2, 20)
        await staff.calls[-1].hangup()
        await asyncio.wait_for(call.dialog.terminated.wait(), 5)
        res = await finished(r, "+17865550180")
        assert res.record["ended_by"] == "staff"


async def test_a_failed_stt_utterance_asks_again_and_a_digit_pound_entry_fills_the_phone_field():
    async with rig(["yes", "Jordan Mitchell", "yes", "yes"]) as r:
        r.faults.stt_fail_calls = {2}  # the second utterance times out before the stand-in model is asked
        call, _ = await place(r, ASH, "+14155550181")
        await agent_done(call)
        for text in ("Yes, that is fine.", "My name is Jordan Mitchell.", "My name is Jordan Mitchell.", "Yes, that is right."):
            call.say(clip(text))  # utterance 1: consent, 2: speech to text fails, 3: the name, 4: the read-back
            await call.wait_for_speech(10, since=time.monotonic())
            await call.wait_for_quiet(1.2, 30)
        await call.dtmf("4155550123#")
        await call.wait_for_speech(10, since=time.monotonic())
        await call.wait_for_quiet(1.2, 30)
        call.say(clip("Yes, that is right."))  # utterance 5: confirms the number read back from the keypad entry
        await call.wait_for_speech(10, since=time.monotonic())
        await call.hangup()
        res = await finished(r, "+14155550181")
        ev = [e["kind"] for e in res.record["events"]]
        spoken = " ".join(t["text"] for t in res.transcript if t["speaker"] == "AGENT")
        assert "stt_failure" in ev and res.record["fields"]["full_name"]["value"] == "Jordan Mitchell"
        assert any(t["speaker"] == "AGENT" and "did not catch that" in t["text"] for t in res.transcript)  # the failed turn was answered, not ignored
        phone = res.record["fields"]["callback_number"]
        assert "four one five, five five five, zero one two three" in spoken  # the keypad entry was read back
        assert (phone["source"], phone["value"], phone["confirmed"]) == ("keypad", "+14155550123", True)


async def test_the_saved_call_is_readable_only_by_that_firms_staff_and_both_deliveries_are_queued():
    async with rig(["yes"]) as r:
        call, _ = await place(r, ASH, "+14155550182")
        await agent_done(call)
        await call.hangup()
        res = await finished(r, "+14155550182")
        rec = res.record
        assert r.store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", rec["call_id"], "record")["call_id"] == rec["call_id"]
        assert r.store.list_calls("tidewaterquint") == []
        assert [e.target for e in r.outbox.entries.values() if e.key == rec["idempotency_key"]] == ["console"]  # no intake was submitted


async def test_a_full_intake_with_stand_in_models_is_saved_and_queued_to_both_receivers(caplog):
    caplog.set_level(logging.DEBUG)
    script = ["yes", "Jordan Mitchell", "yes", "four one five five five five oh one two three", "yes", "no", "I was in a car accident",
              "about two weeks ago", "A truck ran a red light and hit my car on Main Street and my neck has hurt ever since"]
    async with rig(script) as r:
        call, _ = await place(r, ASH, "+14155550183")
        for i, line in enumerate(script):
            await agent_done(call) if i == 0 else await call.wait_for_quiet(1.2, 30)
            call.say(clip())
            await call.wait_for_speech(10, since=time.monotonic())
        res = await finished(r, "+14155550183", wait=40)
        rec = res.record
        assert rec["outcome"] == "intake_complete" and rec["fields"]["callback_number"]["value"] == "+14155550123"
        assert rec["fields"]["practice_area"]["value"] == "personal_injury" and rec["ended_by"] == "agent:complete"
        queued = sorted(e.target for e in r.outbox.entries.values() if e.key == rec["idempotency_key"])
        assert queued == ["bridge", "console"]
        end = time.monotonic() + 10
        while r.outbox.stats()["delivered"] < 2 and time.monotonic() < end:
            await asyncio.sleep(0.1)
        sent = {url: body for url, _headers, body in r.stub.received}  # what the receivers got; the outbox keeps no body once delivered
        bridge, console = sent[r.bridge_url], sent[r.console_url]
        assert bridge["data"]["client"]["name"] == "Jordan Mitchell" and bridge["meta"]["config_hash"] == rec["config_hash"]
        assert console["reason"] == "OTHER" and "555" not in str(console["transcript"])
        assert all(e.payload is None and e.sealed == "" for e in r.outbox.entries.values())  # delivered: only id, status and times stay
        # class: caller data in a file the line writes outside the encrypted store. Read EVERY file under the data folder, and every log line.
        planted = [b"Jordan", b"Mitchell", b"4155550123", b"Main Street", b"neck has hurt"]
        for f in r.data_dir.rglob("*"):
            if f.is_file() and not f.name.endswith(".enc"):
                assert not [p for p in planted if p in f.read_bytes()], f"caller data in the clear in {f.relative_to(r.data_dir)}"
        logged = " ".join(rec.getMessage() for rec in caplog.records)
        assert not [p.decode() for p in planted if p.decode() in logged], "caller words reached a log line"
        assert "chars" in logged  # the log still says that it heard something, only not what


# ----------------------------------------------------------------------------------------------- a transfer that the caller abandons


async def _press_zero(r, caller):
    call, _ = await place(r, TIDE, caller)
    await agent_done(call)
    await call.dtmf("0")
    return call, r.staff["tidewaterquint"]


async def test_the_caller_hanging_up_while_the_desk_phone_rings_cancels_the_desk_invite_and_still_leaves_a_callback():
    async with rig([]) as r:
        r.staff["tidewaterquint"].answer_delay = 3.0
        call, staff = await _press_zero(r, "+17865550170")
        end = time.monotonic() + 10
        while not staff.invites and time.monotonic() < end:
            await asyncio.sleep(0.05)
        assert staff.invites, "the desk phone never rang"
        await asyncio.sleep(0.5)
        await call.hangup()
        res = await finished(r, "+17865550170")
        assert staff.invites[-1].cancelled.is_set(), "the line never cancelled the desk INVITE"
        await asyncio.sleep(3.3)  # past the moment the desk phone would have picked up
        assert staff.calls == [], "the desk phone answered a call that was already over"
        rec = res.record
        assert rec["outcome"] == "caller_hung_up" and rec["transfer"]["result"] == "cancelled" and any(t["kind"] == "callback" for t in rec["tasks"])
        assert not r.server.sessions


async def test_the_caller_hanging_up_during_the_whisper_hangs_up_the_desk_phone_and_stops_the_media():
    async with rig([]) as r:
        call, staff = await _press_zero(r, "+17865550171")
        end = time.monotonic() + 15
        while not (staff.calls and len(staff.calls[-1].frames) > 10) and time.monotonic() < end:
            await asyncio.sleep(0.05)
        desk = staff.calls[-1]
        assert not desk.dialog.terminated.is_set()
        t_hangup = time.monotonic()
        await call.hangup()
        end = t_hangup + 4
        while not desk.dialog.terminated.is_set() and time.monotonic() < end:
            await asyncio.sleep(0.02)
        assert desk.dialog.terminated.is_set() and time.monotonic() - t_hangup < 1.0, "the desk phone kept hearing the whisper of a call that was over"
        res = await finished(r, "+17865550171")
        before = desk.rtp.packets_in
        await asyncio.sleep(1.0)
        assert desk.rtp.packets_in == before, "RTP kept flowing to the desk phone after the call ended"
        assert res.record["outcome"] in ("caller_hung_up", "transferred") and not r.server.sessions


# ----------------------------------------------------------------------------------------------- withheld caller ids


async def test_two_anonymous_callers_on_one_line_are_two_calls_a_real_number_twice_is_one_and_a_retransmission_is_not_a_second_call():
    async with rig(["yes"]) as r:
        a, s1 = await place(r, ASH, "anonymous")
        b, s2 = await place(r, ASH, "restricted")
        c, s3 = await place(r, ASH, "+266696687")
        assert (s1, s2, s3) == (200, 200, 200) and await _wait_until(lambda: len(r.server.sessions) == 3) and r.server.counters["duplicate_call"] == 0
        real, s4 = await place(r, ASH, "+14155550177")
        again, s5 = await place(r, ASH, "+14155550177")
        assert s4 == 200 and again is None and s5 == 486 and r.server.counters["duplicate_call"] == 1
        for call in (a, b, c, real):
            await call.hangup()
        assert await _wait_until(lambda: not r.server.sessions)


async def test_a_retransmitted_invite_from_an_anonymous_caller_is_one_call():
    import socket

    async with rig([]) as r:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("127.0.0.1", 0))
        sdp = f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {rtp.getsockname()[1]} RTP/AVP 0 101\r\na=rtpmap:101 telephone-event/8000\r\n"
        msg = (f"INVITE sip:{ASH}@127.0.0.1:{r.server.port} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{sock.getsockname()[1]};branch=z9hG4bKretx\r\nFrom: <sip:anonymous@anonymous.invalid>;tag=a\r\n"
               f"To: <sip:{ASH}@127.0.0.1>\r\nCall-ID: retx1@x\r\nCSeq: 1 INVITE\r\nContact: <sip:anon@127.0.0.1:{sock.getsockname()[1]}>\r\nContent-Type: application/sdp\r\n"
               f"Content-Length: {len(sdp)}\r\n\r\n{sdp}")
        try:
            for _ in range(3):
                sock.sendto(msg.encode(), ("127.0.0.1", r.server.port))
                await asyncio.sleep(0.15)
            replies = []
            while True:
                try:
                    replies.append(sm.parse(sock.recv(8192)))
                except BlockingIOError:
                    break
            oks = [m for m in replies if m.status_code == 200]
            assert oks and len({m.tag_of("to") for m in oks}) == 1, "every retransmission must get the first call's answer"
            ack = (f"ACK sip:{ASH}@127.0.0.1:{r.server.port} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{sock.getsockname()[1]};branch=z9hG4bKretack\r\n"
                   f"From: <sip:anonymous@anonymous.invalid>;tag=a\r\nTo: <sip:{ASH}@127.0.0.1>;tag={oks[0].tag_of('to')}\r\nCall-ID: retx1@x\r\nCSeq: 1 ACK\r\n"
                   "Content-Length: 0\r\n\r\n")
            sock.sendto(ack.encode(), ("127.0.0.1", r.server.port))
            assert await _wait_until(lambda: r.server.counters["answered"] == 1)
            assert r.server.counters["duplicate_call"] == 0 and r.server.stack.stats["retransmits_absorbed"] >= 2 and len(r.server.sessions) == 1
        finally:
            sock.close()
            rtp.close()


async def _wait_until(predicate, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()
