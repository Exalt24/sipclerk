"""Which firm a call is for is read from what the caller dialled, in every form a carrier or a PBX writes it, and never guessed.

The first version read only the user part of the Request-URI. A trunk that puts its own name there and the number in the To header, a carrier that sends
ten digits, a `tel:` URI, or a `%2B` or `00` form each got a 404, and a `tel:` INVITE got no answer at all because its handler task died unseen."""
import asyncio
import itertools
import socket
import time
from pathlib import Path

import pytest
import yaml

from fake_models import FakeModels
from sipclerk.labrun import LabRig
from sipclerk.server import ClerkServer
from sipclerk.sip import message as sm
from sipclerk.sip.stack import IncomingInvite, SipStack
from sipclerk.tenants import TenantRegistry
from sipclerk.voice.llm import Llm

ROOT = Path(__file__).resolve().parent.parent
ASH, TIDE = "+14155550101", "+13055550102"
SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {port} RTP/AVP 0 101\r\na=rtpmap:101 telephone-event/8000\r\n"


def invite_text(ruri: str, to: str, extra: str = "", frm: str = "<sip:+14155550142@carrier.example>;tag=f1", call_id: str = "c1@x") -> str:
    return (f"INVITE {ruri} SIP/2.0\r\nVia: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bKabc\r\nFrom: {frm}\r\nTo: {to}\r\nCall-ID: {call_id}\r\nCSeq: 1 INVITE\r\n"
            f"{extra}Content-Length: 0\r\n\r\n")


def who(registry, ruri, to="<sip:nobody@carrier.example>", extra=""):
    inv = IncomingInvite(None, sm.parse(invite_text(ruri, to, extra).encode()), ("10.0.0.1", 5060))
    t = registry.resolve(inv.candidates)
    return t.config.tenant_id if t else None


def shapes(did: str) -> list[str]:
    """Every way a Request-URI says this number (the + and digits form `did` is `+1` and ten digits)."""
    d = did[1:]
    national = d[1:]
    return [f"sip:{did}@h", f"sips:{did}@h", f"sip:%2B{d}@h", f"sip:00{d}@h", f"sip:{d}@h", f"sip:{national}@h", f"sip:{national};phone-context=+1@h;user=phone",
            f"sip:{did};npdi=yes@h;user=phone", f"sip:{did}:secret@h:5060", f"tel:{did}", f"tel:+1-{national[:3]}-{national[3:6]}-{national[6:]}",
            f"tel:{national[:3]}-{national[3:6]}-{national[6:]};phone-context=+1", f"tel:{national}", f"sip:{did}@10.1.2.3:5070;transport=udp", f"tel:{did};isub=12"]


@pytest.mark.parametrize("ruri", shapes(ASH))
def test_every_shape_of_the_request_uri_finds_the_first_firm(registry, ruri):
    assert who(registry, ruri) == "ashgrovepell"


@pytest.mark.parametrize("ruri", shapes(TIDE))
def test_every_shape_of_the_request_uri_finds_the_second_firm(registry, ruri):
    assert who(registry, ruri) == "tidewaterquint"


@pytest.mark.parametrize("ruri", ["sip:s@h", "sip:trunk01@h", "sip:anonymous@carrier.example", "sip:h", "sip:", "tel:", "sip:trunk4155550101@h", "tel:+19995550000"])
@pytest.mark.parametrize("to,firm", [(f"<sip:{ASH}@h>", "ashgrovepell"), (f'"Law" <sip:00{TIDE[1:]}@h>;tag=9', "tidewaterquint"), (f"<tel:{ASH}>", "ashgrovepell"),
                                     (f"sip:%2B{TIDE[1:]}@h", "tidewaterquint"), ("<sip:also-nobody@h>", None)])
def test_when_the_request_uri_names_no_firm_the_to_header_does(registry, ruri, to, firm):
    assert who(registry, ruri, to) == firm


def test_the_request_uri_outranks_the_to_header_and_a_number_that_is_nobodys_falls_through_to_the_next(registry):
    assert who(registry, f"sip:{ASH}@h", f"<sip:{TIDE}@h>") == "ashgrovepell"
    assert who(registry, f"sip:{TIDE}@h", f"<sip:{ASH}@h>") == "tidewaterquint"
    assert who(registry, "sip:+19995550000@h", f"<sip:{TIDE}@h>") == "tidewaterquint"


def test_a_hint_a_proxy_adds_is_never_used_to_pick_the_firm(registry):
    extra = f"P-Called-Party-ID: <sip:{ASH}@h>\r\nP-Asserted-Identity: <sip:{TIDE}@h>\r\nDiversion: <sip:{ASH}@h>\r\n"
    assert who(registry, "sip:+19995550000@h", "<sip:+18885550000@h>", extra) is None


@pytest.mark.parametrize("ruri", ["sip:4085550199@h", "sip:+4155550101@h", "sip:+141555501011@h", "sip:155550101@h", "tel:+1415555010", "sip:14155550102@h", "sip:+13055550101@h"])
def test_a_number_that_is_not_exactly_a_firms_is_nobodys(registry, ruri):
    assert who(registry, ruri) is None


# ---- never the wrong firm: two firms whose numbers end in the same ten digits

def two_look_alike_firms(tmp_path):
    src = yaml.safe_load((ROOT / "tenants" / "ashgrovepell.yaml").read_text(encoding="utf-8"))
    for tenant_id, did in (("alphafirm", "+14155550101"), ("betafirm", "+494155550101")):
        raw = dict(src, tenant_id=tenant_id, did=did)
        (tmp_path / f"{tenant_id}.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return TenantRegistry.from_dir(tmp_path)


def test_a_national_number_two_firms_share_is_refused_but_each_full_number_finds_its_own(tmp_path):
    reg = two_look_alike_firms(tmp_path)
    assert who(reg, "sip:4155550101@h") is None and who(reg, "tel:4155550101") is None
    assert who(reg, "sip:+14155550101@h") == "alphafirm" and who(reg, "sip:+494155550101@h") == "betafirm"
    assert who(reg, "sip:4155550101@h", "<sip:+494155550101@h>") == "betafirm"  # the ambiguous one is skipped, the exact one is taken
    assert who(reg, "sip:00494155550101@h") == "betafirm" and who(reg, "sip:14155550101@h") == "alphafirm"


def test_class_no_combination_of_shapes_ever_loads_a_firm_the_number_does_not_belong_to(registry, tmp_path):
    """Every shape of every number, in the Request-URI and the To header, against the real pair and the look-alike pair."""
    numbers = {ASH: "ashgrovepell", TIDE: "tidewaterquint", "+19995550000": None, "+18885550000": None}
    for reg, extra_numbers in ((registry, {}), (two_look_alike_firms(tmp_path), {"+494155550101": "betafirm", "+14155550101": "alphafirm"})):
        by_number = {**{n: (t if reg.by_id(t) else None) for n, t in numbers.items()}, **extra_numbers}
        owner = {did: t.config.tenant_id for t in reg.all for did in [t.config.did]}
        for (n1, _), (n2, _) in itertools.product(by_number.items(), repeat=2):
            for r in shapes(n1)[:9]:
                for to in (f"<sip:{n2}@h>", f"<tel:{n2}>"):
                    got = who(reg, r, to)
                    if got is not None:  # whatever it picked, the number it matched is that firm's own, in full or as its only national match
                        t = reg.by_id(got)
                        cands = sm.dialled_numbers(sm.parse(invite_text(r, to).encode()))
                        assert any(c == t.config.did or (len(c) == 10 and t.config.did.endswith(c) and [d for d in owner if d.endswith(c)] == [t.config.did]) for c in cands)


# ---- on the wire

async def read_responses(sock, wait=1.0):
    loop = asyncio.get_running_loop()
    got, end = [], loop.time() + wait
    while loop.time() < end:
        try:
            got.append(sock.recv(8192).decode())
        except BlockingIOError:
            await asyncio.sleep(0.02)
    return got


def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.setblocking(False)
    return s


async def test_a_tel_request_uri_is_answered_like_any_other_instead_of_dying_unseen():
    seen = []
    stack = SipStack("127.0.0.1", 0)

    async def on_invite(inv):
        seen.append(inv)
        inv.reject(486, "Busy Here")

    stack.on_invite = on_invite
    await stack.start()
    sock = udp()
    try:
        sock.sendto(invite_text("tel:+14155550101", "<sip:x@h>").encode(), ("127.0.0.1", stack.port))
        replies = await read_responses(sock, 1.0)
        assert [r.split("\r\n")[0] for r in replies] == ["SIP/2.0 100 Trying", "SIP/2.0 486 Busy Here"]
        assert seen and seen[0].candidates == ["+14155550101"] and seen[0].dialled_ip == ""
    finally:
        sock.close()
        await stack.stop()


async def test_an_invite_the_stack_cannot_parse_gets_a_400_and_garbage_is_still_dropped():
    stack = SipStack("127.0.0.1", 0)
    await stack.start()
    sock = udp()
    try:
        bad = invite_text("sip:+14155550101@h", "<sip:+14155550101@h>").replace("CSeq: 1 INVITE", "CSeq: abc INVITE")
        sock.sendto(bad.encode(), ("127.0.0.1", stack.port))
        replies = await read_responses(sock, 0.6)
        assert len(replies) == 1 and replies[0].startswith("SIP/2.0 400 Bad Request")
        assert "Call-ID: c1@x" in replies[0] and "CSeq: abc INVITE" in replies[0] and "tag=" in replies[0].split("To:")[1].split("\r\n")[0]
        for junk in (b"\x00\x01garbage", b"INVITE sip:x SIP/2.0\r\n\r\n", b"OPTIONS sip:x SIP/2.0\r\nVia: SIP/2.0/UDP h;branch=z9hG4bKx\r\nFrom: <sip:a@h>;tag=1\r\nTo: <sip:b@h>\r\nCall-ID: o1\r\nCSeq: x OPTIONS\r\n\r\n"):
            sock.sendto(junk, ("127.0.0.1", stack.port))
        assert await read_responses(sock, 0.5) == []  # nothing to answer with, or not an INVITE: dropped as before
        assert stack.stats["malformed"] == 4
    finally:
        sock.close()
        await stack.stop()


async def test_an_invite_that_reads_but_cannot_be_made_sense_of_gets_a_400(monkeypatch):
    stack = SipStack("127.0.0.1", 0)
    stack.on_invite = lambda inv: asyncio.sleep(0)
    await stack.start()
    sock = udp()

    def broken(req):
        raise ValueError("cannot make sense of this")

    monkeypatch.setattr(sm, "dialled_numbers", broken)
    try:
        sock.sendto(invite_text("sip:+14155550101@h", "<sip:x@h>").encode(), ("127.0.0.1", stack.port))
        replies = await read_responses(sock, 0.6)
        assert [r.split("\r\n")[0] for r in replies] == ["SIP/2.0 400 Bad Request"]
    finally:
        sock.close()
        await stack.stop()


# ---- through the whole line

@pytest.mark.parametrize("ruri,to,firm,did", [
    ("tel:+14155550101", "<tel:+14155550101>", "ashgrovepell", ASH),
    ("sip:trunk01@127.0.0.1", f"<sip:{TIDE}@127.0.0.1>", "tidewaterquint", TIDE),
    ("sip:4155550101@127.0.0.1", "<sip:x@127.0.0.1>", "ashgrovepell", ASH),
    ("sip:%2B13055550102@127.0.0.1", "<sip:x@127.0.0.1>", "tidewaterquint", TIDE),
])
async def test_the_line_answers_each_shape_and_loads_that_firms_file(ruri, to, firm, did):
    async with LabRig(FakeModels([]), llm_factory=lambda: Llm(fail=True), timeout_scale=0.4, master_key=bytes(range(32)) * 2) as r:
        sip, rtp = udp(), udp()
        try:
            frm = "<sip:+14155550166@carrier.example>;tag=f1"
            msg = invite_text(ruri.replace("127.0.0.1", f"127.0.0.1:{r.server.port}"), to, frm=frm, call_id="shape1@x")
            msg = msg.replace("10.0.0.1:5060", f"127.0.0.1:{sip.getsockname()[1]}").replace("Content-Length: 0", f"Content-Type: application/sdp\r\nContent-Length: {len(SDP.format(port=rtp.getsockname()[1]))}")
            sock_target = ("127.0.0.1", r.server.port)
            sip.sendto((msg + SDP.format(port=rtp.getsockname()[1])).encode(), sock_target)
            replies = await read_responses(sip, 2.0)
            ok = [x for x in replies if x.startswith("SIP/2.0 200")]
            assert ok, [x.split("\r\n")[0] for x in replies]
            to_tag = [ln for ln in ok[0].split("\r\n") if ln.lower().startswith("to:")][0].split("tag=")[1]
            ack = (f"ACK sip:{did}@127.0.0.1:{r.server.port} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{sip.getsockname()[1]};branch=z9hG4bKack\r\nFrom: {frm}\r\nTo: <sip:{did}@127.0.0.1>;tag={to_tag}\r\n"
                   "Call-ID: shape1@x\r\nCSeq: 1 ACK\r\nContent-Length: 0\r\n\r\n")
            sip.sendto(ack.encode(), sock_target)
            await asyncio.sleep(0.6)
            bye = ack.replace("ACK ", "BYE ").replace("1 ACK", "2 BYE").replace("branch=z9hG4bKack", "branch=z9hG4bKbye")
            sip.sendto(bye.encode(), sock_target)
            end = time.monotonic() + 15
            while not r.results and time.monotonic() < end:
                await asyncio.sleep(0.1)
            assert r.results and r.results[0].record["tenant_id"] == firm and r.results[0].record["did"] == did
        finally:
            sip.close()
            rtp.close()
