"""A dialog follows the route set of the proxies that asked to stay on its path (RFC 3261 12.1 and 12.2.1.1).

The first version dropped Record-Route from its 200 OK and sent its BYE straight to the caller's Contact, which behind a record-routing carrier proxy
is a private address: the carrier never saw the hang-up and the caller's leg could stay open."""
import asyncio
import socket

import pytest

from sipclerk import audio
from sipclerk.sip import message as sm
from sipclerk.sip.stack import SipStack

SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 45000 RTP/AVP 0 101\r\na=rtpmap:101 telephone-event/8000\r\n"
CONTACT = "sip:caller@10.20.30.40:5060"


def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.setblocking(False)
    return s


async def drain(sock, wait=0.5):
    loop = asyncio.get_running_loop()
    got, end = [], loop.time() + wait
    while loop.time() < end:
        try:
            got.append(sm.parse(sock.recv(8192)))
        except BlockingIOError:
            await asyncio.sleep(0.02)
    return got


def invite(port, record_routes=(), call_id="rr1@x"):
    rr = "".join(f"Record-Route: {v}\r\n" for v in record_routes)
    return (f"INVITE sip:+14155550101@sbc.carrier.example SIP/2.0\r\nVia: SIP/2.0/UDP sbc.carrier.example:5060;branch=z9hG4bKp1\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bKc1\r\n{rr}Max-Forwards: 69\r\nFrom: <sip:+14155550142@c>;tag=ft\r\nTo: <sip:+14155550101@c>\r\n"
            f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\nContact: <{CONTACT}>\r\nContent-Type: application/sdp\r\nContent-Length: {len(SDP)}\r\n\r\n{SDP}")


class Line:
    """Our stack as a UAS that answers at once and keeps its dialog."""

    def __init__(self):
        self.stack = SipStack("127.0.0.1", 0, self.on_invite)
        self.dialog = None

    async def on_invite(self, inv):
        self.dialog = await inv.answer(sm.build_sdp("127.0.0.1", 40001, [audio.PCMU], 101), local_host="127.0.0.1")


async def place(record_routes, sender=None):
    """Send the INVITE from `sender` (a socket) and return (line, sender, the 200 OK)."""
    line = Line()
    await line.stack.start()
    sender = sender or udp()
    sender.sendto(invite(sender.getsockname()[1], record_routes).encode(), ("127.0.0.1", line.stack.port))
    got = await drain(sender, 0.6)
    ok = next(m for m in got if m.status_code == 200)
    return line, sender, ok


async def hang_up(line, *sockets):
    task = asyncio.get_running_loop().create_task(line.dialog.bye(timeout=0.8))
    seen = [(s, await drain(s, 0.5)) for s in sockets]
    await task
    return seen


@pytest.mark.parametrize("values,entries", [
    (["<sip:a.example;lr>"], ["<sip:a.example;lr>"]),
    (["<sip:a;lr>, <sip:b;lr>"], ["<sip:a;lr>", "<sip:b;lr>"]),
    (['"Doe, John" <sip:c;lr>', "<sip:d;lr;x=1,2>"], ['"Doe, John" <sip:c;lr>', "<sip:d;lr;x=1,2>"]),
    ([], []),
])
def test_a_list_header_is_split_into_its_entries_in_order(values, entries):
    assert sm.split_list_header(values) == entries


def test_a_loose_router_is_the_one_with_the_lr_parameter():
    assert sm.is_loose_route("<sip:a.example;lr;ftag=zz>") and sm.is_loose_route("<sip:a;LR>") and sm.is_loose_route("<sip:a;lr=on>")
    assert not sm.is_loose_route("<sip:a.example>") and not sm.is_loose_route("<sip:lr.example>") and not sm.is_loose_route("<sip:a;transport=udp>")


async def test_the_200_ok_echoes_every_record_route_in_order_and_the_bye_follows_them():
    line, sender, ok = await place(["<sip:p2.example;lr;ftag=zz>, <sip:p1.example;lr>", "<sip:p0.example;lr>"])
    try:
        assert sm.split_list_header(ok.get_all("record-route")) == ["<sip:p2.example;lr;ftag=zz>", "<sip:p1.example;lr>", "<sip:p0.example;lr>"]
        assert ok.get_all("record-route") == ["<sip:p2.example;lr;ftag=zz>, <sip:p1.example;lr>", "<sip:p0.example;lr>"]  # copied as received
        [(_s, got)] = await hang_up(line, sender)
        bye = next(m for m in got if m.method == "BYE")
        assert bye.request_uri == CONTACT, "loose routing: the Request-URI stays the peer's Contact"
        assert sm.split_list_header(bye.get_all("route")) == ["<sip:p2.example;lr;ftag=zz>", "<sip:p1.example;lr>", "<sip:p0.example;lr>"]
    finally:
        sender.close()
        await line.stack.stop()


async def test_with_no_record_route_nothing_changes():
    line, sender, ok = await place([])
    try:
        assert ok.get_all("record-route") == []
        [(_s, got)] = await hang_up(line, sender)  # sent to where the INVITE came from, as before
        bye = next(m for m in got if m.method == "BYE")
        assert bye.request_uri == CONTACT and bye.get_all("route") == []
    finally:
        sender.close()
        await line.stack.stop()


async def test_a_strict_router_first_becomes_the_request_uri_and_the_contact_goes_last():
    line, sender, ok = await place(["<sip:strict.example>", "<sip:b.example;lr>"])
    try:
        [(_s, got)] = await hang_up(line, sender)
        bye = next(m for m in got if m.method == "BYE")
        assert bye.request_uri == "sip:strict.example"
        assert sm.split_list_header(bye.get_all("route")) == ["<sip:b.example;lr>", f"<{CONTACT}>"]
    finally:
        sender.close()
        await line.stack.stop()


async def test_the_bye_goes_to_the_first_route_when_that_is_an_address_other_than_the_sender():
    proxy, sender = udp(), udp()
    line, sender, ok = await place([f"<sip:127.0.0.1:{proxy.getsockname()[1]};lr>"], sender)
    try:
        [(_a, to_sender), (_b, to_proxy)] = await hang_up(line, sender, proxy)
        assert {m.method for m in to_proxy} == {"BYE"} and not [m for m in to_sender if m.method == "BYE"]  # (an unanswered BYE is retransmitted)
        assert to_proxy[0].request_uri == CONTACT and to_proxy[0].get("route") == f"<sip:127.0.0.1:{proxy.getsockname()[1]};lr>"
    finally:
        sender.close()
        proxy.close()
        await line.stack.stop()


async def test_a_call_we_place_reads_the_2xx_record_route_backwards_for_the_ack_and_the_bye():
    desk, seen = udp(), []
    stack = SipStack("127.0.0.1", 0)
    await stack.start()

    async def uas():
        loop = asyncio.get_running_loop()
        while True:
            data, addr = await loop.sock_recvfrom(desk, 8192)
            msg = sm.parse(data)
            seen.append(msg)
            if msg.method == "INVITE":
                resp = sm.make_response(msg, 200, "OK", to_tag="d1", contact="sip:desk@10.9.9.9:5060", body=sm.build_sdp("127.0.0.1", 40002, [audio.PCMU]))
                resp.headers.insert(1, ("Record-Route", "<sip:near.example;lr>, <sip:far.example;lr>"))
                desk.sendto(resp.encode(), addr)
            elif msg.method == "BYE":
                desk.sendto(sm.make_response(msg, 200, "OK").encode(), addr)
                return

    task = asyncio.get_running_loop().create_task(uas())
    try:
        res = await stack.invite(f"sip:desk@127.0.0.1:{desk.getsockname()[1]}", "sip:line@127.0.0.1", sm.build_sdp("127.0.0.1", 40003, [audio.PCMU]), timeout=5)
        assert res.dialog is not None and res.dialog.route_set == ["<sip:far.example;lr>", "<sip:near.example;lr>"]
        await asyncio.sleep(0.2)
        await res.dialog.bye(timeout=1.0)
        await asyncio.wait_for(task, 3)
        ack, bye = (next(m for m in seen if m.method == name) for name in ("ACK", "BYE"))
        for m in (ack, bye):
            assert m.request_uri == "sip:desk@10.9.9.9:5060"
            assert sm.split_list_header(m.get_all("route")) == ["<sip:far.example;lr>", "<sip:near.example;lr>"]
    finally:
        task.cancel()
        desk.close()
        await stack.stop()


async def test_only_responses_that_make_a_dialog_carry_the_record_route():
    """100 Trying and a refusal belong to no dialog, so they do not echo it; the 180 and the 200 do."""
    stack = SipStack("127.0.0.1", 0)

    async def refuse(inv):
        inv.provisional(180, "Ringing")
        inv.reject(486, "Busy Here")

    stack.on_invite = refuse
    await stack.start()
    sender = udp()
    try:
        sender.sendto(invite(sender.getsockname()[1], ["<sip:p.example;lr>"], "rr9@x").encode(), ("127.0.0.1", stack.port))
        got = {m.status_code: m for m in await drain(sender, 0.6)}
        assert got[100].get_all("record-route") == [] and got[486].get_all("record-route") == []
        assert got[180].get_all("record-route") == ["<sip:p.example;lr>"]
    finally:
        sender.close()
        await stack.stop()


# ---- the class, enumerated: how many proxies record-routed x whether the nearest one is a loose or a strict router x which side of the call placed it
#
# GROUNDING (docs/DECISIONS.md): "The line echoes the INVITE's Record-Route headers in its provisional and 2xx responses unchanged and in order (RFC 3261 12.1.1), keeps them as
# the dialog's route set, and sends its BYE with a Route header per entry and the peer's Contact as the Request-URI" and "The first version dropped Record-Route and sent its BYE
# straight to a private Contact, so the carrier never saw the hang-up."
# WHY: a firm's carrier puts one, two or three proxies on the path of an ordinary call. The caller hangs up, the line sends its BYE to the private Contact, the carrier never
# sees it and keeps billing the leg and holding the channel open.
import itertools
import re

# CLASS-TABLE: a dialog that ignores the route set of a proxy, for every count of record-routing proxies and either kind of nearest router
DESK_CONTACT = "sip:desk@10.9.9.9:5060"
LOOSE_PROXIES = ["<sip:p1.example;lr>", "<sip:p2.example;lr;ftag=zz>", "<sip:p3.example;lr>"]
STRICT_PROXY = "<sip:strict.example>"
COUNTS = [0, 1, 2, 3]
FIRST_KINDS = ["loose", "strict"]


def route_entries(values) -> list[str]:
    """The entries of Record-Route or Route header values, written here so the table does not lean on the helper under test."""
    return re.findall(r'(?:"[^"]*"\s*)?<[^>]*>', ",".join(values))


def route_cells():
    for count, kind in itertools.product(COUNTS, FIRST_KINDS):
        if count == 0 and kind == "strict":
            continue  # no proxy, no router kind
        yield count, kind


def recorded(count: int, first_kind: str) -> list[str]:
    """The Record-Route entries, the first one being the router the request meets first."""
    entries = list(LOOSE_PROXIES[:count])
    if count and first_kind == "strict":
        entries[0] = STRICT_PROXY
    return entries


async def until_seen(sock, found, timeout=3.0):
    """Read datagrams from `sock` until `found(messages)` is true: no fixed wait, the answer comes the moment it arrives."""
    loop = asyncio.get_running_loop()
    got, end = [], loop.time() + timeout
    while loop.time() < end:
        try:
            got.append(sm.parse(sock.recv(8192)))
        except BlockingIOError:
            await asyncio.sleep(0.01)
            continue
        if found(got):
            break
    return got


def expected_request(route_set: list[str], contact: str) -> tuple[str, list[str]]:
    """RFC 3261 12.2.1.1 stated again by hand: with no route set the Contact; a loose first router keeps the Contact and every route; a strict one takes the Request-URI."""
    if not route_set:
        return contact, []
    if re.search(r";lr(?:[;=>]|$)", route_set[0], re.I):
        return contact, list(route_set)
    return re.search(r"<([^>]*)>", route_set[0]).group(1), [*route_set[1:], f"<{contact}>"]


async def test_class_every_count_of_proxies_and_either_kind_of_router_is_followed_by_the_200_ok_and_the_bye_on_the_answering_side():
    wrong = []
    cells = list(route_cells())
    for count, kind in cells:
        rr = recorded(count, kind)
        line = Line()
        await line.stack.start()
        sender = udp()
        try:
            sender.sendto(invite(sender.getsockname()[1], rr, f"rr-{count}-{kind}@x").encode(), ("127.0.0.1", line.stack.port))
            got = await until_seen(sender, lambda ms: any(m.status_code == 200 for m in ms))
            ok = next((m for m in got if m.status_code == 200), None)
            if ok is None:
                wrong.append((count, kind, "no 200 OK"))
                continue
            if route_entries(ok.get_all("record-route")) != rr:
                wrong.append((count, kind, "200 OK Record-Route", route_entries(ok.get_all("record-route"))))
            bye_task = asyncio.get_running_loop().create_task(line.dialog.bye(timeout=0.8))
            seen = await until_seen(sender, lambda ms: any(m.method == "BYE" for m in ms))
            bye = next((m for m in seen if m.method == "BYE"), None)
            if bye is not None:
                sender.sendto(sm.make_response(bye, 200, "OK").encode(), ("127.0.0.1", line.stack.port))  # the carrier answers, so the BYE ends at once
            await bye_task
            if bye is None:
                wrong.append((count, kind, "no BYE"))
                continue
            want_uri, want_routes = expected_request(rr, CONTACT)
            if (bye.request_uri, route_entries(bye.get_all("route"))) != (want_uri, want_routes):
                wrong.append((count, kind, "BYE", bye.request_uri, route_entries(bye.get_all("route"))))
        finally:
            sender.close()
            await line.stack.stop()
    assert not wrong, f"{len(wrong)} faults in {len(cells)} cells ignored the route set: {wrong}"


async def test_class_every_count_of_proxies_and_either_kind_of_router_is_followed_by_the_ack_and_the_bye_of_a_call_the_line_places():
    wrong = []
    cells = list(route_cells())
    for count, kind in cells:
        in_2xx = recorded(count, kind)[::-1]  # the 2xx lists the proxies in the order the response met them; the line reads it backwards
        route_set = in_2xx[::-1]
        desk, seen = udp(), []
        stack = SipStack("127.0.0.1", 0)
        await stack.start()

        async def uas():
            loop = asyncio.get_running_loop()
            while True:
                data, addr = await loop.sock_recvfrom(desk, 8192)
                msg = sm.parse(data)
                seen.append(msg)
                if msg.method == "INVITE":
                    resp = sm.make_response(msg, 200, "OK", to_tag="d1", contact=DESK_CONTACT, body=sm.build_sdp("127.0.0.1", 40002, [audio.PCMU]))
                    if in_2xx:
                        resp.headers.insert(1, ("Record-Route", ", ".join(in_2xx)))
                    desk.sendto(resp.encode(), addr)
                elif msg.method == "BYE":
                    desk.sendto(sm.make_response(msg, 200, "OK").encode(), addr)
                    return

        task = asyncio.get_running_loop().create_task(uas())
        try:
            res = await stack.invite(f"sip:desk@127.0.0.1:{desk.getsockname()[1]}", "sip:line@127.0.0.1", sm.build_sdp("127.0.0.1", 40003, [audio.PCMU]), timeout=5)
            if res.dialog is None:
                wrong.append((count, kind, "no dialog"))
                continue
            end = asyncio.get_running_loop().time() + 2
            while not any(m.method == "ACK" for m in seen) and asyncio.get_running_loop().time() < end:
                await asyncio.sleep(0.01)
            await res.dialog.bye(timeout=1.0)
            await asyncio.wait_for(task, 3)
            want_uri, want_routes = expected_request(route_set, DESK_CONTACT)
            for name in ("ACK", "BYE"):
                m = next((x for x in seen if x.method == name), None)
                if m is None or (m.request_uri, route_entries(m.get_all("route"))) != (want_uri, want_routes):
                    wrong.append((count, kind, name, None if m is None else (m.request_uri, route_entries(m.get_all("route")))))
        finally:
            task.cancel()
            desk.close()
            await stack.stop()
    assert not wrong, f"{len(wrong)} faults in {len(cells)} cells ignored the route set: {wrong}"
