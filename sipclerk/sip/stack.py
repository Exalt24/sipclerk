"""A small SIP user agent over UDP: enough of RFC 3261 for inbound intake calls and a staff endpoint.

Implemented: INVITE (as server and as client), 100/180/2xx/4xx-6xx, ACK, BYE, CANCEL, OPTIONS, INFO
(application/dtmf-relay), re-INVITE answered with the current SDP, retransmission of a final response
to a repeated INVITE, and 2xx retransmission until the ACK arrives.

Not implemented, on purpose: TCP and TLS, registration, digest authentication, SRTP, forking, session
timers, REFER (the warm transfer here is a bridged second call, see sipclerk.sip.phone and sipclerk.call).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sipclerk.sip import message as sm
from sipclerk.sip.message import SipMessage

log = logging.getLogger("sipclerk.sip")

T1 = 0.5


def _ip_literal(host: str) -> str:
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return ""


def local_ip_for(peer_host: str, peer_port: int = 5060) -> str:
    """The local address the OS would use to reach a peer (a UDP connect sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_host, peer_port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@dataclass
class Dialog:
    stack: SipStack
    call_id: str
    role: str  # "uas" or "uac"
    local_uri: str
    remote_uri: str
    local_tag: str
    remote_tag: str
    remote_target: str  # Contact of the peer
    peer_addr: tuple[str, int]
    local_contact: str
    cseq: int = 1
    state: str = "early"
    terminated: asyncio.Event = field(default_factory=asyncio.Event)
    last_answer_sdp: str = ""
    on_dtmf: Callable[[str], None] | None = None
    ended_by: str = ""
    route_set: list[str] = field(default_factory=list)  # the proxies that asked to stay on the path, nearest the peer first (RFC 3261 12.1)

    def request_target(self) -> tuple[str, list[str]]:
        """The Request-URI and Route headers of a request inside this dialog (RFC 3261 12.2.1.1). With no route set the request goes to the peer's Contact.
        With a loose router first, the Contact stays the Request-URI and every route becomes a Route header. A strict router first takes the Request-URI
        itself, and the Contact goes last in the Route headers."""
        if not self.route_set:
            return self.remote_target, []
        if sm.is_loose_route(self.route_set[0]):
            return self.remote_target, list(self.route_set)
        return sm.contact_uri(self.route_set[0]), [*self.route_set[1:], f"<{self.remote_target}>"]

    def next_hop(self) -> tuple[str, int]:
        """Where a request is sent: the first route when it names an address, else where the dialog's first request came from."""
        if self.route_set:
            try:
                host, port = sm.uri_hostport(sm.contact_uri(self.route_set[0]))
                if _ip_literal(host):
                    return host, port
            except sm.SipParseError:
                pass
        return self.peer_addr

    async def bye(self, timeout: float = 2.0) -> bool:
        """Hang up. Returns True if the peer answered the BYE."""
        if self.terminated.is_set():
            return True
        self.cseq += 1
        req = self.stack.build_in_dialog(self, "BYE")
        ok = await self.stack.request(req, self.next_hop(), timeout=timeout)
        self.ended_by = "local"
        self.terminated.set()
        self.stack.dialogs.pop(self.call_id, None)
        return ok is not None and ok.status_code == 200


class IncomingInvite:
    """An INVITE that has reached us and not been answered yet."""

    def __init__(self, stack: SipStack, req: SipMessage, addr: tuple[str, int]):
        self.stack = stack
        self.req = req
        self.addr = addr
        self.to_tag = sm.new_tag()
        self.acked = asyncio.Event()
        self.ack_lost = asyncio.Event()  # no ACK came while the 2xx was being resent: the stack has hung the dialog up with a BYE
        self.cancelled = asyncio.Event()
        self.final: SipMessage | None = None
        self.dialog: Dialog | None = None
        self.candidates = sm.dialled_numbers(req)  # the number as dialled: Request-URI first, then the To header
        self.did = self.candidates[0] if self.candidates else ""
        self.caller = sm.caller_number(req.get("from"))
        self.offer = req.body
        # The address the caller dialled is the one it can reach us on: behind NAT or a Docker bridge our own interface address is not.
        try:
            self.dialled_ip = _ip_literal(sm.uri_hostport(req.request_uri)[0]) if req.is_request else ""
        except sm.SipParseError:  # a tel: URI names no host
            self.dialled_ip = ""

    def provisional(self, code: int = 180, reason: str = "Ringing") -> None:
        self.stack.send(sm.make_response(self.req, code, reason, to_tag=self.to_tag if code != 100 else ""), self.addr)

    def reject(self, code: int, reason: str) -> None:
        self.final = sm.make_response(self.req, code, reason, to_tag=self.to_tag)
        self.stack.send(self.final, self.addr)
        self.stack.pending_invites.pop(self.req.call_id, None)
        self.stack.remember(self.req, self.final)

    async def answer(self, sdp_body: str, local_host: str | None = None) -> Dialog:
        """Send 200 OK with our SDP and keep resending it until the ACK arrives."""
        host = local_host or self.stack.local_host_for(self.addr)
        contact = f"sip:{host}:{self.stack.port}"
        resp = sm.make_response(self.req, 200, "OK", to_tag=self.to_tag, contact=contact, body=sdp_body)
        self.final = resp
        d = Dialog(self.stack, self.req.call_id, "uas",
                   local_uri=sm.contact_uri(self.req.get("to")), remote_uri=sm.contact_uri(self.req.get("from")),
                   local_tag=self.to_tag, remote_tag=self.req.tag_of("from"),
                   remote_target=sm.contact_uri(self.req.get("contact")) or sm.contact_uri(self.req.get("from")),
                   peer_addr=self.addr, local_contact=contact, state="confirmed", last_answer_sdp=sdp_body,
                   route_set=sm.split_list_header(self.req.get_all("record-route")))  # the UAS keeps the request's Record-Route in order (12.1.1)
        d.cseq = 0
        self.dialog = d
        self.stack.dialogs[d.call_id] = d
        self.stack.pending_invites.pop(self.req.call_id, None)
        self.stack.remember(self.req, resp)
        self.stack.send(resp, self.addr)
        self._resender = asyncio.get_running_loop().create_task(self._retransmit_until_ack(resp))
        return d

    async def wait_for_ack(self) -> str:
        """Wait for the ACK of our 2xx for as long as the stack still accepts one (it resends the 2xx until the ACK arrives or 64*T1 has passed, RFC 3261
        13.3.1.4). Returns "acked", "lost" (no ACK in that time: the stack has sent a BYE and the session is over) or "gone" (the caller hung up first)."""
        waits = [asyncio.ensure_future(e.wait()) for e in (self.acked, self.ack_lost, self.dialog.terminated)]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
        return "acked" if self.acked.is_set() else "lost" if self.ack_lost.is_set() else "gone"

    async def _retransmit_until_ack(self, resp: SipMessage) -> None:
        """RFC 3261 13.3.1.4: the 2xx goes out again after T1, 2*T1 and so on up to T2 until the ACK arrives. After 64*T1 without one the dialog is
        confirmed but the session SHOULD be ended with a BYE: that is sent here, and `ack_lost` tells whoever waits for the ACK."""
        t1, loop, d = self.stack.t1, asyncio.get_running_loop(), self.dialog
        deadline, delay = loop.time() + 64 * t1, t1
        stop = [asyncio.ensure_future(self.acked.wait()), asyncio.ensure_future(d.terminated.wait())]
        try:
            while (remaining := deadline - loop.time()) > 0:
                done, _ = await asyncio.wait(stop, timeout=min(delay, remaining), return_when=asyncio.FIRST_COMPLETED)
                if done:
                    return
                self.stack.send(resp, self.addr)
                delay = min(delay * 2, 8 * t1)  # T2 is 8 * T1
        finally:
            for w in stop:
                w.cancel()
        log.warning("no ACK for call %s in %.0f s: ending the session with a BYE", self.req.call_id, 64 * t1)
        self.ack_lost.set()
        self.stack._acks.pop(self.req.call_id, None)  # the stack no longer takes an ACK for this dialog
        await d.bye()
        d.ended_by = "no-ack"


@dataclass
class InviteResult:
    status: int
    reason: str
    dialog: Dialog | None = None
    answer_sdp: str = ""


class SipStack(asyncio.DatagramProtocol):
    def __init__(self, host: str, port: int, on_invite: Callable[[IncomingInvite], Awaitable[None]] | None = None,
                 advertise_host: str | None = None):
        self.host = host
        self.port = port
        self.advertise_host = advertise_host
        self.t1 = T1  # RFC 3261 T1: the 2xx resend interval starts here and the ACK is waited for 64 * T1; a test shortens it
        self.on_invite = on_invite
        self.transport: asyncio.DatagramTransport | None = None
        self.dialogs: dict[str, Dialog] = {}
        self.pending_invites: dict[str, IncomingInvite] = {}
        self._responses: dict[tuple[str, int, str], asyncio.Queue[SipMessage]] = {}
        self._seen: dict[tuple[str, str, str], SipMessage] = {}
        self.stats = {"requests": 0, "responses": 0, "malformed": 0, "retransmits_absorbed": 0}
        self.log: list[tuple[str, str, float]] = []  # (direction, first line, wall time) for the proof run
        self._acks: dict[str, IncomingInvite] = {}

    # ---- lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(self.host, self.port))
        self.port = self.transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        for d in list(self.dialogs.values()):
            await d.bye(timeout=0.5)
        if self.transport:
            self.transport.close()

    def local_host_for(self, peer: tuple[str, int]) -> str:
        if self.advertise_host:
            return self.advertise_host
        if self.host not in ("0.0.0.0", ""):
            return self.host
        return local_ip_for(peer[0], peer[1])

    # ---- wire

    def send(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        if self.transport is None or self.transport.is_closing():
            return
        if not msg.is_request:
            self._stamp_via(msg, addr)
        self.log.append(("out", msg.start_line, time.time()))
        self.transport.sendto(msg.encode(), addr)

    def remember(self, req: SipMessage, resp: SipMessage) -> None:
        self._seen[(req.top_branch, req.method, req.call_id)] = resp

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = sm.parse(data)
        except sm.SipParseError as e:
            self.stats["malformed"] += 1
            log.debug("malformed datagram from %s: %s", addr, e)
            self._refuse_unreadable_invite(data, addr)
            return
        self.log.append(("in", msg.start_line, time.time()))
        try:
            if msg.is_request:
                self.stats["requests"] += 1
                self._on_request(msg, addr)
            else:
                self.stats["responses"] += 1
                self._on_response(msg)
        except Exception:  # a bad packet must never take the stack down
            log.exception("error handling %s", msg.start_line)

    # ---- server side

    def _refuse_unreadable_invite(self, data: bytes, addr) -> None:
        """An INVITE we cannot parse still deserves an answer, or the caller retransmits it for half a minute. A 400 needs the five headers a response
        copies; if even those cannot be found there is nothing to answer with and the datagram is dropped."""
        text = data[:8192].decode("utf-8", errors="replace")
        head = re.split(r"\r?\n\r?\n", text, maxsplit=1)[0]
        lines = re.split(r"\r?\n", head)
        if not lines or not re.match(r"^INVITE \S+ SIP/2\.0\s*$", lines[0]):
            return
        found: dict[str, list[str]] = {}
        for ln in lines[1:]:
            k, sep, v = ln.partition(":")
            name = sm.COMPACT.get(k.strip().lower(), k.strip().lower())
            if sep and name in ("via", "from", "to", "call-id", "cseq"):
                found.setdefault(name, []).append(v.strip())
        if len(found) < 5:
            return
        to = found["to"][0] + ("" if "tag=" in found["to"][0] else f";tag={sm.new_tag()}")
        headers = [("Via", v) for v in found["via"]] + [("From", found["from"][0]), ("To", to), ("Call-ID", found["call-id"][0]),
                                                           ("CSeq", found["cseq"][0]), ("Server", "sipclerk-lab")]
        self.send(SipMessage("SIP/2.0 400 Bad Request", headers), addr)

    @staticmethod
    def _stamp_via(resp: SipMessage, addr: tuple[str, int]) -> SipMessage:
        """RFC 3581: tell the sender which address and port we saw it on."""
        for i, (k, v) in enumerate(resp.headers):
            if k.lower() == "via":
                v = re.sub(r";rport(?!=)", f";rport={addr[1]}", v)
                if "received=" not in v:
                    v += f";received={addr[0]}"
                resp.headers[i] = (k, v)
                break
        return resp

    def _reply(self, req: SipMessage, addr, code: int, reason: str, **kw) -> SipMessage:
        resp = self._stamp_via(sm.make_response(req, code, reason, **kw), addr)
        self.send(resp, addr)
        return resp

    def _on_request(self, req: SipMessage, addr) -> None:
        method = req.method
        key = (req.top_branch, method, req.call_id)
        if method != "ACK" and key in self._seen:  # retransmitted request: repeat our answer
            self.stats["retransmits_absorbed"] += 1
            self.send(self._seen[key], addr)
            return
        d = self.dialogs.get(req.call_id)
        if method == "ACK":
            inv = self._invite_for(req.call_id)
            if inv is not None:
                inv.acked.set()
            return
        if method == "OPTIONS":
            self.remember(req, self._reply(req, addr, 200, "OK", to_tag=sm.new_tag(),
                                           extra=[("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, INFO")]))
            return
        if method == "INVITE" and d is None:
            asyncio.get_running_loop().create_task(self._handle_new_invite(req, addr))
            return
        if method == "CANCEL":
            inv = self.pending_invites.get(req.call_id)
            if inv is None:
                self._reply(req, addr, 481, "Call/Transaction Does Not Exist")
                return
            self._reply(req, addr, 200, "OK")
            inv.cancelled.set()
            inv.reject(487, "Request Terminated")
            return
        if d is None:
            self._reply(req, addr, 481, "Call/Transaction Does Not Exist")
            return
        if method == "BYE":
            self.remember(req, self._reply(req, addr, 200, "OK"))
            d.ended_by = "remote"
            d.terminated.set()
            self.dialogs.pop(d.call_id, None)
        elif method == "INFO":
            self.remember(req, self._reply(req, addr, 200, "OK"))
            ctype = req.get("content-type").lower()
            if "dtmf" in ctype and d.on_dtmf:
                digit = next((ln.split("=")[1].strip() for ln in req.body.splitlines() if ln.lower().startswith("signal")), "")
                if digit:
                    d.on_dtmf(digit)
        elif method == "INVITE":  # re-INVITE (hold, refresh): keep the same media
            self.remember(req, self._reply(req, addr, 200, "OK", to_tag=d.local_tag, contact=d.local_contact,
                                           body=d.last_answer_sdp))
        else:
            self._reply(req, addr, 501, "Not Implemented")

    def _invite_for(self, call_id: str) -> IncomingInvite | None:
        return self._acks.get(call_id)

    async def _handle_new_invite(self, req: SipMessage, addr) -> None:
        try:
            inv = IncomingInvite(self, req, addr)
        except Exception as exc:  # readable as a message, but not something we can answer: say so instead of going quiet
            log.warning("bad INVITE %s: %s", req.call_id, exc)
            self.remember(req, self._reply(req, addr, 400, "Bad Request", to_tag=sm.new_tag()))
            return
        self.pending_invites[req.call_id] = inv
        self._acks[req.call_id] = inv
        trying = sm.make_response(req, 100, "Trying")
        self.remember(req, trying)
        self.send(trying, addr)
        if self.on_invite is None:
            inv.reject(404, "Not Found")
            return
        try:
            await self.on_invite(inv)
        except Exception:
            log.exception("on_invite failed for %s", req.call_id)
            if inv.final is None:
                inv.reject(500, "Server Internal Error")

    # ---- client side

    def _on_response(self, resp: SipMessage) -> None:
        q = self._responses.get((resp.call_id, resp.cseq_number, resp.cseq_method))
        if q is not None:
            q.put_nowait(resp)

    def build_in_dialog(self, d: Dialog, method: str, body: str = "", ctype: str = "") -> SipMessage:
        host = self.local_host_for(d.peer_addr)
        target, routes = d.request_target()
        headers = [
            ("Via", f"SIP/2.0/UDP {host}:{self.port};rport;branch={sm.new_branch()}"),
            ("Max-Forwards", "70"),
            *[("Route", r) for r in routes],
            ("From", f"<{d.local_uri}>;tag={d.local_tag}"),
            ("To", f"<{d.remote_uri}>;tag={d.remote_tag}"),
            ("Call-ID", d.call_id),
            ("CSeq", f"{d.cseq} {method}"),
            ("Contact", f"<{d.local_contact}>"),
            ("User-Agent", "sipclerk-lab"),
        ]
        if body:
            headers.append(("Content-Type", ctype or "application/sdp"))
        return SipMessage(f"{method} {target} SIP/2.0", headers, body)

    async def request(self, req: SipMessage, addr, timeout: float = 2.0) -> SipMessage | None:
        """Send a non-INVITE request and wait for its final response (retransmits at T1, 2*T1, ...)."""
        key = (req.call_id, req.cseq_number, req.cseq_method)
        q: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._responses[key] = q
        try:
            end = asyncio.get_running_loop().time() + timeout
            delay = T1
            while True:
                self.send(req, addr)
                try:
                    while True:
                        resp = await asyncio.wait_for(q.get(), min(delay, max(0.01, end - asyncio.get_running_loop().time())))
                        if resp.status_code >= 200:
                            return resp
                except asyncio.TimeoutError:
                    pass
                if asyncio.get_running_loop().time() >= end:
                    return None
                delay = min(delay * 2, 4.0)
        finally:
            self._responses.pop(key, None)

    async def invite(self, to_uri: str, from_uri: str, sdp_body: str, *, timeout: float = 30.0,
                     on_ringing: Callable[[], None] | None = None) -> InviteResult:
        """Place a call. Waits up to `timeout` seconds for a final answer, then sends CANCEL."""
        host_part, port = sm.uri_hostport(to_uri)
        addr = (socket.gethostbyname(host_part), port)
        local_host = self.local_host_for(addr)
        call_id = sm.new_call_id(local_host)
        tag = sm.new_tag()
        contact = f"sip:{sm.uri_user(from_uri) or 'sipclerk'}@{local_host}:{self.port}"
        branch = sm.new_branch()
        headers = [
            ("Via", f"SIP/2.0/UDP {local_host}:{self.port};rport;branch={branch}"),
            ("Max-Forwards", "70"),
            ("From", f"<{from_uri}>;tag={tag}"),
            ("To", f"<{to_uri}>"),
            ("Call-ID", call_id),
            ("CSeq", "1 INVITE"),
            ("Contact", f"<{contact}>"),
            ("User-Agent", "sipclerk-lab"),
            ("Content-Type", "application/sdp"),
        ]
        req = SipMessage(f"INVITE {to_uri} SIP/2.0", headers, sdp_body)
        q: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._responses[(call_id, 1, "INVITE")] = q
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        delay = T1
        got_provisional = False

        def make_dialog(resp: SipMessage) -> Dialog:
            d = Dialog(self, call_id, "uac", local_uri=from_uri, remote_uri=to_uri, local_tag=tag, remote_tag=resp.tag_of("to"),
                       remote_target=sm.contact_uri(resp.get("contact")) or to_uri, peer_addr=addr, local_contact=contact, state="confirmed", cseq=1,
                       route_set=list(reversed(sm.split_list_header(resp.get_all("record-route")))))  # the UAC reads the 2xx's Record-Route backwards (12.1.2)
            self.dialogs[call_id] = d
            return d

        try:
            self.send(req, addr)
            while True:
                remaining = end - loop.time()
                if remaining <= 0:
                    await self._cancel(req, addr, q, make_dialog)
                    return InviteResult(408, "Request Timeout")
                try:
                    resp = await asyncio.wait_for(q.get(), min(delay, remaining))
                except asyncio.TimeoutError:
                    if not got_provisional:
                        self.send(req, addr)
                        delay = min(delay * 2, 4.0)
                    continue
                if resp.status_code < 200:
                    got_provisional = True
                    if resp.status_code == 180 and on_ringing:
                        on_ringing()
                    continue
                if resp.status_code >= 300:
                    self._send_ack(req, resp, addr)
                    return InviteResult(resp.status_code, resp.start_line.split(" ", 2)[2])
                d = make_dialog(resp)
                self._send_ack(req, resp, addr, dialog=d)
                self._absorb_2xx_retransmits(call_id, q, req, resp, addr, d)
                return InviteResult(200, "OK", d, resp.body)
        except asyncio.CancelledError:  # whoever placed this call no longer wants it: CANCEL it, or hang up if it was answered as we cancelled
            await self._cancel(req, addr, q, make_dialog)
            raise

    def _send_ack(self, inv: SipMessage, resp: SipMessage, addr, dialog: Dialog | None = None) -> None:
        routes: list[str] = []
        if dialog:
            target, routes = dialog.request_target()
            addr = dialog.next_hop()
            branch = sm.new_branch()  # ACK for a 2xx is its own transaction
        else:
            target = inv.request_uri
            branch = inv.top_branch
        local_host = self.local_host_for(addr)
        headers = [
            ("Via", f"SIP/2.0/UDP {local_host}:{self.port};rport;branch={branch}"),
            ("Max-Forwards", "70"),
            *[("Route", r) for r in routes],
            ("From", inv.get("from")),
            ("To", resp.get("to")),
            ("Call-ID", inv.call_id),
            ("CSeq", f"{inv.cseq_number} ACK"),
        ]
        self.send(SipMessage(f"ACK {target} SIP/2.0", headers), addr)

    def _absorb_2xx_retransmits(self, call_id, q, inv, resp, addr, d) -> None:
        async def run() -> None:
            loop = asyncio.get_running_loop()
            end = loop.time() + 10
            try:
                while loop.time() < end and not d.terminated.is_set():  # the task ends with the dialog, not ten seconds later
                    getter = asyncio.ensure_future(q.get())
                    gone = asyncio.ensure_future(d.terminated.wait())
                    done, pending = await asyncio.wait({getter, gone}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                    for p in pending:
                        p.cancel()
                    if getter in done and getter.result().status_code == 200:
                        self._send_ack(inv, getter.result(), addr, dialog=d)  # the 200 was resent because our ACK was lost
            finally:
                self._responses.pop((call_id, 1, "INVITE"), None)

        asyncio.get_running_loop().create_task(run())

    async def _cancel(self, inv: SipMessage, addr, q: asyncio.Queue, make_dialog=None) -> None:
        headers = [
            ("Via", inv.get("via")),
            ("Max-Forwards", "70"),
            ("From", inv.get("from")),
            ("To", inv.get("to")),
            ("Call-ID", inv.call_id),
            ("CSeq", "1 CANCEL"),
        ]
        self.send(SipMessage(f"CANCEL {inv.request_uri} SIP/2.0", headers), addr)
        try:
            while True:
                r = await asyncio.wait_for(q.get(), 2.0)
                if r.status_code >= 300:
                    self._send_ack(inv, r, addr)
                    break
                if r.status_code >= 200 and make_dialog is not None:  # the far end answered as our CANCEL crossed it: take that call down properly
                    d = make_dialog(r)
                    self._send_ack(inv, r, addr, dialog=d)
                    await d.bye(timeout=1.0)
                    break
        except asyncio.TimeoutError:
            pass
        finally:
            self._responses.pop((inv.call_id, 1, "INVITE"), None)
