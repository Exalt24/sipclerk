"""The line waits for the caller's ACK as long as the stack still accepts one, and a call whose ACK never comes is ended and recorded.

RFC 3261 13.3.1.4: the UAS resends its 2xx, starting at T1 and doubling up to T2, until the ACK arrives; "if the server retransmits the 2xx response for 64*T1
seconds without receiving an ACK, the dialog is confirmed, but the session SHOULD be terminated. This is accomplished with a BYE". The first version gave up
on the call after 5 seconds while the stack went on resending the 2xx for 32, so an ACK that came at 6 s (a lost ACK that was resent, a slow carrier) left an
answered call with no session, no media and no record, and an ACK that never came dropped the dialog silently. T1 is `SipStack.t1`; these tests shorten it."""
import asyncio
import re
import secrets
import time

import pytest

from sipclerk.sip.rtp import RtpSession
from sipclerk.store import Actor
from fake_models import FakeModels
from sipclerk.labrun import LabRig
from sipclerk.voice.llm import Llm
from test_pipeline_fast import ASH, KEY, rig


class Udp(asyncio.DatagramProtocol):
    def __init__(self):
        self.log: list[tuple[float, bytes]] = []

    def connection_made(self, t):
        self.t = t

    def datagram_received(self, d, a):
        self.log.append((time.monotonic(), d))


async def udp():
    t, p = await asyncio.get_running_loop().create_datagram_endpoint(Udp, local_addr=("127.0.0.1", 0))
    return t, p, t.get_extra_info("sockname")[1]


def sdp(port):
    return "\r\n".join(["v=0", "o=c 1 1 IN IP4 127.0.0.1", "s=c", "c=IN IP4 127.0.0.1", "t=0 0", f"m=audio {port} RTP/AVP 0 101", "a=rtpmap:0 PCMU/8000",
                        "a=rtpmap:101 telephone-event/8000", "a=fmtp:101 0-16", "a=sendrecv"]) + "\r\n"


def invite(sport, lport, rport, caller, cid):
    body = sdp(rport)
    return (f"INVITE sip:{ASH}@127.0.0.1:{sport} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{lport};branch=z9hG4bK{secrets.token_hex(5)};rport\r\nMax-Forwards: 70\r\n"
            f"From: <sip:{caller}@127.0.0.1>;tag=ft1\r\nTo: <sip:{ASH}@127.0.0.1:{sport}>\r\nCall-ID: {cid}\r\nCSeq: 1 INVITE\r\nContact: <sip:{caller}@127.0.0.1:{lport}>\r\n"
            f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode()


def request(method, sport, lport, caller, cid, cseq, totag):
    return (f"{method} sip:127.0.0.1:{sport} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{lport};branch=z9hG4bK{secrets.token_hex(5)};rport\r\nMax-Forwards: 70\r\n"
            f"From: <sip:{caller}@127.0.0.1>;tag=ft1\r\nTo: <sip:{ASH}@127.0.0.1:{sport}>;tag={totag}\r\nCall-ID: {cid}\r\nCSeq: {cseq} {method}\r\nContent-Length: 0\r\n\r\n").encode()


def answer_to_bye(bye: bytes) -> bytes:
    """A 200 for the BYE the line sent, copying the headers a response copies."""
    text = bye.decode()
    keep = [ln for ln in text.split("\r\n") if ln.split(":")[0].lower() in ("via", "from", "to", "call-id", "cseq")]
    return ("SIP/2.0 200 OK\r\n" + "\r\n".join(keep) + "\r\nContent-Length: 0\r\n\r\n").encode()


async def until(predicate, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


class Caller:
    def __init__(self, r, caller):
        self.r, self.caller, self.cid = r, caller, f"ack-{secrets.token_hex(4)}@127.0.0.1"

    async def start(self):
        self.t, self.p, self.lport = await udp()
        self.rt, self.rp, self.rport = await udp()  # where the line's RTP goes
        self.sport = self.r.server.port
        self.t.sendto(invite(self.sport, self.lport, self.rport, self.caller, self.cid), ("127.0.0.1", self.sport))
        assert await until(lambda: any(d.startswith(b"SIP/2.0 200") for _, d in self.p.log), 5), "no 200 OK"
        ok = next(d for _, d in self.p.log if d.startswith(b"SIP/2.0 200")).decode()
        self.totag = re.search(r"To:[^\r]*tag=([^;>\r\s]+)", ok).group(1)
        self.t0 = next(t for t, d in self.p.log if d.startswith(b"SIP/2.0 200"))
        return self

    def count(self, prefix: bytes) -> int:
        return sum(1 for _, d in self.p.log if d.startswith(prefix))

    def send(self, method, cseq=1):
        self.t.sendto(request(method, self.sport, self.lport, self.caller, self.cid, cseq, self.totag), ("127.0.0.1", self.sport))

    def close(self):
        self.t.close()
        self.rt.close()


@pytest.fixture
def opened_media(monkeypatch):
    """Every RTP socket the line opens, to see that a call that never started has its media freed."""
    seen: list[RtpSession] = []
    real = RtpSession.open.__func__

    async def recording_open(cls, *a, **kw):
        s = await real(cls, *a, **kw)
        seen.append(s)
        return s

    monkeypatch.setattr(RtpSession, "open", classmethod(recording_open))
    return seen


async def test_an_ack_that_comes_in_time_starts_the_call_and_the_window_running_out_later_does_nothing_to_it():
    async with rig(["yes"]) as r:
        r.server.stack.t1 = 0.05  # the ACK window is 3.2 s
        c = await Caller(r, "+14155550171").start()
        await asyncio.sleep(0.3)
        c.send("ACK")
        assert await until(lambda: r.server.sessions, 3) and r.server.counters["answered"] == 1 and r.server.counters.get("no_ack", 0) == 0
        assert await until(lambda: len(c.rp.log) > 20, 5), "no audio reached the caller"
        resent = c.count(b"SIP/2.0 200")
        await asyncio.sleep(3.6)  # past the moment the window would have ended had the ACK not come
        assert c.count(b"BYE") == 0 and r.server.sessions and c.count(b"SIP/2.0 200") == resent, "the 2xx kept being resent or the call was ended after its ACK"
        c.send("BYE", cseq=2)
        assert await until(lambda: r.results, 15) and r.results[0].record["caller_number"] == "+14155550171"
        c.close()


async def test_an_ack_that_comes_after_the_old_five_second_wait_still_starts_the_call():
    """T1 0.2 s: the stack resends the 2xx for 12.8 s. The ACK comes at 5.6 s, which is after the five seconds the line used to wait."""
    async with rig(["yes"]) as r:
        r.server.stack.t1 = 0.2
        c = await Caller(r, "+14155550172").start()
        await asyncio.sleep(5.6)
        assert not r.server.sessions and c.count(b"BYE") == 0 and c.count(b"SIP/2.0 200") >= 4  # still waiting, still resending the 2xx
        c.send("ACK")
        assert await until(lambda: r.server.sessions, 3), "the late ACK did not start the call"
        assert await until(lambda: len(c.rp.log) > 20, 5), "no audio reached the caller after the late ACK"
        assert c.count(b"BYE") == 0 and r.server.counters.get("no_ack", 0) == 0
        c.send("BYE", cseq=2)
        assert await until(lambda: r.results, 15) and r.results[0].record["outcome"] == "caller_hung_up"
        c.close()


async def test_an_ack_that_never_comes_ends_the_session_with_a_bye_frees_the_media_and_leaves_a_record_with_a_callback(opened_media):
    """T1 0.05 s: 64 * T1 is 3.2 s. Nothing comes back. The line must resend the 2xx, then send BYE, free its media and store the call."""
    async with rig([]) as r:
        r.server.stack.t1 = 0.05
        c = await Caller(r, "+14155550173").start()
        assert await until(lambda: c.count(b"BYE") >= 1, 3.2 + 3), "no BYE from the line after the ACK window"
        bye_after = next(t for t, d in c.p.log if d.startswith(b"BYE")) - c.t0
        assert 3.0 <= bye_after <= 5.0, bye_after  # 64 * T1, not the old five seconds and not never
        assert c.count(b"SIP/2.0 200") >= 5, "the 2xx was not resent while the ACK was awaited"
        c.t.sendto(answer_to_bye(next(d for _, d in c.p.log if d.startswith(b"BYE"))), ("127.0.0.1", c.sport))
        assert await until(lambda: r.server.counters["no_ack"] == 1 and r.store.list_calls("ashgrovepell"), 5)
        assert not r.server.sessions and r.server.counters["answered"] == 0
        assert opened_media and all(s.transport.is_closing() for s in opened_media), "the media socket of a call that never started is still open"
        (call_id,) = r.store.list_calls("ashgrovepell")
        rec = r.store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", call_id, "record")
        assert rec["outcome"] == "dropped_no_ack" and rec["ended_by"] == "no-ack" and rec["caller_number"] == "+14155550173"
        assert rec["tasks"] and rec["tasks"][0]["kind"] == "callback" and "ACK" in rec["tasks"][0]["reason"]
        assert sorted(e.target for e in r.outbox.entries.values() if e.key == rec["idempotency_key"]) == ["bridge", "console"]
        c.send("ACK")  # a stray ACK after the BYE changes nothing
        await asyncio.sleep(0.3)
        assert not r.server.sessions and len(r.store.list_calls("ashgrovepell")) == 1
        c.close()


async def test_a_caller_who_hangs_up_before_the_ack_is_not_recorded_as_a_lost_ack():
    async with rig([]) as r:
        r.server.stack.t1 = 0.05
        c = await Caller(r, "+14155550174").start()
        c.send("BYE", cseq=2)
        await asyncio.sleep(4.0)
        assert r.server.counters.get("no_ack", 0) == 0 and not r.server.sessions and c.count(b"BYE") == 0
        c.close()


def test_the_resend_rule_is_the_rfc_one():
    """T1 is the stack's own attribute and defaults to the RFC value; the window is 64 times it."""
    from sipclerk.sip.stack import T1, SipStack

    assert T1 == 0.5 and SipStack("127.0.0.1", 0).t1 == 0.5 and 64 * T1 == 32


async def test_a_caller_redialling_the_instant_after_its_ack_is_still_one_caller():
    """The line now waits for an ACK for as long as the stack takes one, so a caller who is answered and not yet confirmed already counts as on the line:
    a second INVITE from the same number right behind the first one's ACK gets 486, as it does a moment later."""
    async with rig(["yes"]) as r:
        c = await Caller(r, "+14155550175").start()
        c.send("ACK")
        t, p, lport = await udp()
        t.sendto(invite(c.sport, lport, c.rport, "+14155550175", f"second-{secrets.token_hex(4)}@127.0.0.1"), ("127.0.0.1", c.sport))  # the same tick as the ACK
        assert await until(lambda: any(d.startswith(b"SIP/2.0 486") or d.startswith(b"SIP/2.0 200") for _, d in p.log), 3)
        first_answer = next(d for _, d in p.log if d.startswith(b"SIP/2.0 486") or d.startswith(b"SIP/2.0 200")).split(b"\r\n")[0]
        assert first_answer == b"SIP/2.0 486 Busy Here", first_answer
        assert r.server.counters["duplicate_call"] == 1
        t.close()
        c.close()


async def test_a_caller_who_has_been_answered_and_not_yet_confirmed_blocks_a_second_call_from_the_same_number():
    async with rig([]) as r:
        r.server.stack.t1 = 0.05
        c = await Caller(r, "+14155550176").start()  # answered, ACK withheld
        t, p, lport = await udp()
        t.sendto(invite(c.sport, lport, c.rport, "+14155550176;cpc=ordinary", f"second-{secrets.token_hex(4)}@127.0.0.1"), ("127.0.0.1", c.sport))
        assert await until(lambda: any(d.startswith(b"SIP/2.0 486") for _, d in p.log), 3), [d[:20] for _, d in p.log]
        assert await until(lambda: r.server.counters["no_ack"] == 1, 6)
        t.sendto(invite(c.sport, lport, c.rport, "+14155550176", f"third-{secrets.token_hex(4)}@127.0.0.1"), ("127.0.0.1", c.sport))  # the lost call is over: the number is free again
        assert await until(lambda: sum(1 for _, d in p.log if d.startswith(b"SIP/2.0 200")) >= 1, 3)
        t.close()
        c.close()


async def test_stopping_the_line_while_a_call_waits_for_its_ack_starts_no_call():
    """The ACK wait is as long as the stack takes an ACK, so a line that is stopped in the middle of it must not start the call afterwards."""
    async with rig(["yes"]) as r:
        c = await Caller(r, "+14155550177").start()
        c.send("ACK")
        await r.server.stop()
        assert r.server.counters["answered"] == 0 and not r.server.sessions
        assert not [t for t in asyncio.all_tasks() if "WorkerRunner" in t.get_coro().__qualname__], "a voice pipeline was started after the line stopped"
        c.close()


class SlowSummary(Llm):
    async def summarize(self, facts):
        await asyncio.sleep(3.0)  # a call takes a moment to file its record
        return None


async def test_stopping_the_line_waits_for_every_live_call_to_file_its_record_and_leaves_no_pipeline_behind():
    """`stop` waits until each call has filed its record and torn its voice pipeline down, so nothing outlives the line (a fixed 50 ms did not)."""
    async with LabRig(FakeModels([]), master_key=KEY, llm_factory=lambda: SlowSummary(fail=True), timeout_scale=0.4) as r:
        callers = [await Caller(r, f"+1415555018{i}").start() for i in range(3)]
        for c in callers:
            c.send("ACK")
        assert await until(lambda: len(r.server.sessions) == 3, 5)
        await r.server.stop()
        try:
            assert not r.server.sessions and len(r.results) == 3, (len(r.server.sessions), len(r.results))
            assert not [t for t in asyncio.all_tasks() if "WorkerRunner" in t.get_coro().__qualname__], "a voice pipeline outlived the line"
        finally:
            for s in list(r.server.sessions.values()):  # when the line did not wait, end the calls here: a pipeline left running hangs the process at the end of the loop
                s.finished.set()
            await until(lambda: not r.server.sessions, 15)
            for c in callers:
                c.close()


# ---- the class, enumerated: how long the stack's ACK window is x when the caller's ACK arrives inside it, after it, or never
#
# GROUNDING (docs/DECISIONS.md): "an ACK at 6 seconds (a lost ACK that was resent, a slow carrier) left an answered call with no session, no media and no record, and an ACK that
# never came dropped the dialog silently." and "when it never comes the stack sends the BYE, the line frees its media and stores a record (outcome `dropped_no_ack`, a callback
# task, both deliveries) so the firm has a trace of a call it answered."
# WHY: a person phones the firm over a carrier that is slow with the ACK. The line answered, the caller hears nothing and hangs up, and the firm has no record that anyone
# called, so the lead is lost and nobody knows to phone back.
import itertools

from test_pipeline_fast import finished

# CLASS-TABLE: an answered call with a late or lost ACK left with no session and no record, for every ACK arrival time against the window
T1S = (0.02, 0.1)  # the stack's T1 the cell runs at: the window is 64 * T1, 1.28 s or 6.4 s
ACK_AT = (0.05, 0.9, 1.4, None)  # when the ACK arrives as a fraction of the window: early, just inside, after it, never
# the cells that fit the time the suite can spend: every one of the short window, and the long one only where the ACK is late enough for the old fixed five second wait
ACK_TIMINGS = [(t1, f) for t1, f in itertools.product(T1S, ACK_AT) if 64 * t1 * (f if f is not None else 1.0) <= 2.0 or (f is not None and 5.0 < 64 * t1 * f < 6.0)]


async def test_class_an_ack_inside_the_window_starts_the_call_and_one_after_it_or_never_leaves_a_record_with_a_callback():
    wrong = []
    for n, (t1, fraction) in enumerate(ACK_TIMINGS):
        window = 64 * t1
        caller = f"+1415555019{n}"
        async with rig(["yes"]) as r:
            r.server.stack.t1 = t1
            c = await Caller(r, caller).start()
            problems = []
            if fraction is not None:
                await asyncio.sleep(max(0.0, window * fraction - (time.monotonic() - c.t0)))  # the cell fixes the moment the ACK arrives
                c.send("ACK")
            if fraction is not None and fraction < 1:
                if not await until(lambda: r.server.sessions, 3):
                    problems.append("an ACK inside the window did not start the call")
                elif c.count(b"BYE"):
                    problems.append("the line hung up on a call whose ACK came in time")
                c.send("BYE", cseq=2)
                try:
                    res = await finished(r, caller, 4)
                    if res.record["outcome"] != "caller_hung_up":
                        problems.append(f"outcome {res.record['outcome']}")
                except AssertionError:
                    problems.append("a call that started left no record when it ended")
            else:
                if not await until(lambda: c.count(b"BYE") >= 1, window + 3):
                    problems.append("the line never hung up on the dialog it gave up on")
                else:
                    c.t.sendto(answer_to_bye(next(d for _, d in c.p.log if d.startswith(b"BYE"))), ("127.0.0.1", c.sport))
                if not await until(lambda: r.server.counters.get("no_ack", 0) == 1 and r.store.list_calls("ashgrovepell"), window + 3):
                    problems.append("no record of a call whose ACK never came in time")
                else:
                    (call_id,) = r.store.list_calls("ashgrovepell")
                    rec = r.store.read(Actor("sam", "ashgrovepell", "intake_staff"), "ashgrovepell", call_id, "record")
                    if not (rec["outcome"] == "dropped_no_ack" and rec["caller_number"] == caller and rec["tasks"] and rec["tasks"][0]["kind"] == "callback"):
                        problems.append(f"record {rec['outcome']}, tasks {rec['tasks']}")
                    if sorted(e.target for e in r.outbox.entries.values() if e.key == rec["idempotency_key"]) != ["bridge", "console"]:
                        problems.append("the record did not reach both receivers")
                if fraction is not None:
                    c.send("ACK")  # a stray ACK after the line gave up changes nothing
                    await asyncio.sleep(0.2)
                    if r.server.sessions or len(r.store.list_calls("ashgrovepell")) != 1:
                        problems.append("a late ACK started a call or a second record")
            c.close()
        if problems:
            wrong.append((t1, fraction, problems))
    assert not wrong, f"{len(wrong)} of {len(ACK_TIMINGS)} ACK timings left a call without a session or a record: {wrong}"
