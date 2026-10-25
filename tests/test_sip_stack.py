"""The SIP and RTP legs over real UDP sockets on loopback: INVITE, SDP negotiation, audio, DTMF, BYE."""
import asyncio

import pytest

from sipclerk import audio
from sipclerk.sip import message as sm
from sipclerk.sip.phone import PhoneCall, SoftPhone, StaffEndpoint, goertzel_power, tone
from sipclerk.sip.rtp import RtpSession, build_packet, dtmf_payload, parse_packet
from sipclerk.sip.stack import SipStack

INVITE = (
    "INVITE sip:+14155550101@127.0.0.1:5060 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 10.0.0.5:5070;branch=z9hG4bKabc;rport\r\n"
    "From: <sip:+14155550142@10.0.0.5>;tag=aa\r\nTo: <sip:+14155550101@127.0.0.1>\r\n"
    "Call-ID: c1@10.0.0.5\r\nCSeq: 1 INVITE\r\nContact: <sip:caller@10.0.0.5:5070>\r\n"
    "Content-Type: application/sdp\r\nContent-Length: 0\r\n\r\n"
)


def test_parse_request_and_headers():
    m = sm.parse(INVITE.encode())
    assert m.is_request and m.method == "INVITE"
    assert sm.normalize_did(sm.uri_user(m.request_uri)) == "+14155550101"
    assert m.top_branch == "z9hG4bKabc" and m.tag_of("from") == "aa" and m.cseq_number == 1


def test_parse_rejects_garbage_and_oversize():
    for bad in (b"hello", b"INVITE sip:x SIP/2.0\r\n\r\n", b"\xff\xfe", b"A" * 9000):
        with pytest.raises(sm.SipParseError):
            sm.parse(bad)


def test_compact_header_names_are_expanded():
    m = sm.parse(b"OPTIONS sip:a@b SIP/2.0\r\nv: SIP/2.0/UDP h;branch=z9hG4bK1\r\nf: <sip:a@h>;tag=1\r\nt: <sip:b@b>\r\n"
                 b"i: id1\r\nCSeq: 7 OPTIONS\r\nl: 0\r\n\r\n")
    assert m.call_id == "id1" and m.cseq_number == 7


def test_did_normalisation():
    assert sm.normalize_did("+1 (415) 555-0101") == "+14155550101"
    assert sm.normalize_did("14155550101") == "+14155550101"
    assert sm.normalize_did("tel:+14155550101") == "+14155550101"


def test_sdp_roundtrip_and_codec_negotiation():
    body = sm.build_sdp("10.0.0.5", 4000, [audio.PCMA, audio.PCMU], 96)
    sdp = sm.parse_sdp(body)
    assert sdp.port == 4000 and sdp.dtmf_pt == 96 and sdp.codec_names == ["PCMA", "PCMU"]
    assert sm.negotiate(sdp, [audio.PCMU, audio.PCMA]) == ("PCMU", 0)
    assert sm.negotiate(sdp, [audio.PCMA]) == ("PCMA", 8)
    assert sm.negotiate(sm.parse_sdp(sm.build_sdp("h", 1, [audio.PCMA], None)), [audio.PCMU]) is None


def test_rtp_packet_roundtrip_and_dtmf_payload():
    pkt = build_packet(0, 65535, 4294967295, 7, b"\x01\x02", marker=True)
    pt, seq, ts, ssrc, marker, payload = parse_packet(pkt)
    assert (pt, seq, ts, ssrc, marker, payload) == (0, 65535, 4294967295, 7, True, b"\x01\x02")
    assert parse_packet(b"\x00" * 5) is None
    assert dtmf_payload(5, True, 1280)[1] & 0x80


def test_g711_roundtrip_is_close_and_resample_ratios_hold():
    pcm = tone(440, 0.1, amp=6000)
    for codec in (audio.PCMU, audio.PCMA):
        back = audio.decode(audio.encode(pcm, codec), codec)
        assert len(back) == len(pcm)
        assert audio.measured_snr_db(pcm, back) > 30
    assert len(audio.resample(pcm, 8000, 16000)) == len(pcm) * 2
    assert len(audio.resample(bytes(48000), 24000, 8000)) == 16000


def test_noise_is_added_at_the_requested_snr():
    clean = tone(300, 1.0, amp=6000)
    assert audio.measured_snr_db(clean, audio.add_noise(clean, 10)) == pytest.approx(10, abs=0.6)
    assert audio.measured_snr_db(clean, audio.add_noise(clean, 20)) == pytest.approx(20, abs=0.6)
    assert audio.rms(audio.silence_frame(audio.PCMU)) == 0 or audio.rms(audio.decode(audio.silence_frame(audio.PCMU), audio.PCMU)) < 10


async def _echo_server(stack: SipStack, codecs: list[str], received: dict):
    async def on_invite(inv):
        offer = sm.parse_sdp(inv.offer)
        pick = sm.negotiate(offer, codecs)
        if pick is None:
            inv.reject(488, "Not Acceptable Here")
            return
        codec, _ = pick
        rtp = await RtpSession.open("127.0.0.1", 0, codec, offer.dtmf_pt)
        rtp.set_remote(offer.host, offer.port)
        rtp.on_dtmf = lambda d: received.setdefault("digits", []).append(d)
        rtp.on_frame = lambda pcm: received.setdefault("frames", []).append(pcm)
        d = await inv.answer(sm.build_sdp("127.0.0.1", rtp.local_port, [codec], offer.dtmf_pt))
        rtp.start()
        received.update(rtp=rtp, dialog=d, did=inv.did, caller=inv.caller, codec=codec)

    stack.on_invite = on_invite


async def test_call_audio_dtmf_and_bye_over_udp():
    received: dict = {}
    server = SipStack("127.0.0.1", 0)
    client = SipStack("127.0.0.1", 0)
    await server.start()
    await client.start()
    await _echo_server(server, [audio.PCMA, audio.PCMU], received)
    try:
        call, status = await SoftPhone(client).call(f"sip:+14155550101@127.0.0.1:{server.port}", timeout=5)
        assert status == 200 and call is not None
        assert received["did"] == "+14155550101" and received["caller"] == "+14155550142"
        assert call.codec == "PCMA"  # the server's preference won over the caller's offer order
        await call.say_and_wait(tone(1000, 0.4))
        await call.dtmf("5#")
        await asyncio.sleep(0.2)
        assert received["digits"] == ["5", "#"]
        heard = b"".join(received["frames"])
        assert goertzel_power(heard, 1000) > 50 * goertzel_power(heard, 2500)
        await call.hangup()
        await asyncio.wait_for(received["dialog"].terminated.wait(), 2)
        assert received["dialog"].ended_by == "remote"
    finally:
        await server.stop()
        await client.stop()


async def test_unknown_codec_gets_488_and_unrouted_gets_404():
    server = SipStack("127.0.0.1", 0)
    client = SipStack("127.0.0.1", 0)
    await server.start()
    await client.start()
    await _echo_server(server, [audio.PCMU], {})
    try:
        call, status = await SoftPhone(client).call(f"sip:+1@127.0.0.1:{server.port}", codecs=[audio.PCMA], timeout=3)
        assert call is None and status == 488
        server.on_invite = None
        call, status = await SoftPhone(client).call(f"sip:+1@127.0.0.1:{server.port}", timeout=3)
        assert call is None and status == 404
    finally:
        await server.stop()
        await client.stop()


async def test_cancel_before_answer_and_options_ping():
    server = SipStack("127.0.0.1", 0)
    client = SipStack("127.0.0.1", 0)
    await server.start()
    await client.start()
    staff = StaffEndpoint(server, mode="no_answer")
    try:
        call, status = await SoftPhone(client).call(f"sip:desk@127.0.0.1:{server.port}", timeout=1.2)
        assert call is None and status == 408
        assert staff.invites and staff.invites[0].cancelled.is_set()
        req = sm.SipMessage(f"OPTIONS sip:x@127.0.0.1:{server.port} SIP/2.0", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{client.port};rport;branch={sm.new_branch()}"), ("From", "<sip:a@h>;tag=1"),
            ("To", "<sip:b@h>"), ("Call-ID", "opt1"), ("CSeq", "1 OPTIONS")])
        resp = await client.request(req, ("127.0.0.1", server.port), timeout=2)
        assert resp is not None and resp.status_code == 200 and "INVITE" in resp.get("allow")
    finally:
        await server.stop()
        await client.stop()


async def test_a_retransmitted_invite_gets_the_same_answer_not_a_second_call():
    server = SipStack("127.0.0.1", 0)
    calls = []

    async def on_invite(inv):
        calls.append(inv)
        inv.reject(486, "Busy Here")

    server.on_invite = on_invite
    await server.start()
    loop = asyncio.get_running_loop()
    got: list[bytes] = []

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            got.append(data)

    tr, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
    try:
        for _ in range(2):
            tr.sendto(INVITE.encode(), ("127.0.0.1", server.port))
            await asyncio.sleep(0.2)
        assert len(calls) == 1
        assert sum(b.startswith(b"SIP/2.0 486") for b in got) == 2
        assert server.stats["retransmits_absorbed"] >= 1
    finally:
        tr.close()
        await server.stop()


async def test_garbage_datagram_does_not_stop_the_stack():
    server = SipStack("127.0.0.1", 0)
    await server.start()
    loop = asyncio.get_running_loop()
    tr, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    try:
        tr.sendto(b"\x00\x01garbage", ("127.0.0.1", server.port))
        await asyncio.sleep(0.1)
        assert server.stats["malformed"] == 1
    finally:
        tr.close()
        await server.stop()


def test_a_duplicate_or_late_rtp_packet_is_not_decoded_twice():
    session = RtpSession(audio.PCMU, 101)
    pkt = lambda seq: build_packet(0, seq, seq * 160, 5, audio.encode(tone(500, 0.02, amp=4000), audio.PCMU))  # noqa: E731
    for seq in (10, 11, 11, 12, 9, 13):
        session.datagram_received(pkt(seq), ("127.0.0.1", 4000))
    assert len(session.rx_frames) == 4  # 10, 11, 12, 13: the repeat of 11 and the late 9 are dropped


def test_a_short_loss_is_filled_with_silence_so_the_timeline_holds():
    session = RtpSession(audio.PCMU, 101)
    pkt = lambda seq: build_packet(0, seq, seq * 160, 5, audio.encode(tone(500, 0.02, amp=4000), audio.PCMU))  # noqa: E731
    for seq in (1, 2, 5):
        session.datagram_received(pkt(seq), ("127.0.0.1", 4000))
    assert len(session.rx_frames) == 5 and audio.rms(list(session.rx_frames)[2]) == 0


def test_the_media_address_is_the_address_the_caller_dialled_when_it_is_an_ip():
    from sipclerk.sip.stack import IncomingInvite

    stack = SipStack("0.0.0.0", 0)
    msg = sm.parse(INVITE.replace("sip:+14155550101@127.0.0.1:5060", "sip:+14155550101@192.168.65.254:5070").encode())
    assert IncomingInvite(stack, msg, ("127.0.0.1", 40000)).dialled_ip == "192.168.65.254"
    named = sm.parse(INVITE.replace("sip:+14155550101@127.0.0.1:5060", "sip:+14155550101@line.example.test").encode())
    assert IncomingInvite(stack, named, ("127.0.0.1", 40000)).dialled_ip == ""
