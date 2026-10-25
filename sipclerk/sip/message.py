"""SIP message parsing and building (RFC 3261 subset), and SDP offer/answer (RFC 4566, RFC 3264).

Only what an inbound intake line needs: requests and responses over UDP, the headers a
dialog depends on, and an SDP body that negotiates G.711 plus RFC 4733 DTMF.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import unquote

CRLF = "\r\n"
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}
MAX_MESSAGE_BYTES = 8192


class SipParseError(ValueError):
    pass


def new_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(6)  # the magic cookie marks an RFC 3261 branch


def new_tag() -> str:
    return secrets.token_hex(4)


def new_call_id(host: str) -> str:
    return f"{secrets.token_hex(8)}@{host}"


@dataclass
class SipMessage:
    start_line: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""

    @property
    def is_request(self) -> bool:
        return not self.start_line.startswith("SIP/2.0")

    @property
    def method(self) -> str:
        if self.is_request:
            return self.start_line.split(" ", 1)[0].upper()
        return self.cseq_method

    @property
    def request_uri(self) -> str:
        return self.start_line.split(" ")[1] if self.is_request else ""

    @property
    def status_code(self) -> int:
        return int(self.start_line.split(" ")[1]) if not self.is_request else 0

    def get(self, name: str, default: str = "") -> str:
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.headers if k.lower() == name]

    @property
    def call_id(self) -> str:
        return self.get("call-id")

    @property
    def cseq_number(self) -> int:
        return int(self.get("cseq").split()[0])

    @property
    def cseq_method(self) -> str:
        return self.get("cseq").split()[1].upper()

    @property
    def top_branch(self) -> str:
        m = re.search(r"branch=([^;,\s]+)", self.get("via"))
        return m.group(1) if m else ""

    def tag_of(self, header: str) -> str:
        m = re.search(r";\s*tag=([^;>\s]+)", self.get(header))
        return m.group(1) if m else ""

    def encode(self) -> bytes:
        body = self.body.encode()
        lines = [self.start_line]
        for k, v in self.headers:
            if k.lower() == "content-length":
                continue
            lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(body)}")
        return (CRLF.join(lines) + CRLF + CRLF).encode() + body


def parse(data: bytes) -> SipMessage:
    if len(data) > MAX_MESSAGE_BYTES:
        raise SipParseError("message too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SipParseError("not utf-8") from e
    head, _, body = text.partition(CRLF + CRLF)
    if not _:
        head, _, body = text.partition("\n\n")
    lines = [ln for ln in re.split(r"\r?\n", head) if ln != ""]
    if not lines:
        raise SipParseError("empty message")
    start = lines[0]
    if not (start.startswith("SIP/2.0 ") or re.match(r"^[A-Z]+ \S+ SIP/2\.0$", start)):
        raise SipParseError(f"bad start line: {start[:40]!r}")
    headers: list[tuple[str, str]] = []
    for ln in lines[1:]:
        if ln[0] in " \t" and headers:  # folded header
            k, v = headers[-1]
            headers[-1] = (k, v + " " + ln.strip())
            continue
        if ":" not in ln:
            raise SipParseError(f"bad header: {ln[:40]!r}")
        k, v = ln.split(":", 1)
        k = k.strip()
        headers.append((COMPACT.get(k.lower(), k), v.strip()))
    msg = SipMessage(start, headers, body)
    clen = msg.get("content-length")
    if clen.isdigit():
        msg.body = body.encode()[: int(clen)].decode(errors="replace")
    for required in ("via", "from", "to", "call-id", "cseq"):
        if not msg.get(required):
            raise SipParseError(f"missing {required}")
    try:
        msg.cseq_number
    except (ValueError, IndexError) as e:
        raise SipParseError("bad cseq") from e
    return msg


def uri_user(uri: str) -> str:
    """The user part of a sip: URI or a header value that holds one, '' if none."""
    m = re.search(r"sips?:([^@;>\s]+)@", uri)
    return m.group(1) if m else ""


def uri_hostport(uri: str) -> tuple[str, int]:
    m = re.search(r"sips?:(?:[^@;>\s]+@)?([^:;>\s?]+)(?::(\d+))?", uri)
    if not m:
        raise SipParseError(f"no host in {uri!r}")
    return m.group(1), int(m.group(2) or 5060)


def contact_uri(value: str) -> str:
    m = re.search(r"<([^>]+)>", value)
    return m.group(1) if m else value.split(";")[0].strip()


def split_list_header(values: list[str]) -> list[str]:
    """Header lines that each hold one or more comma separated entries (`Record-Route: <a>, <b>`), as the entries in order. A comma inside <...> or
    quotes belongs to the entry."""
    out: list[str] = []
    for line in values:
        depth, quoted, start = 0, False, 0
        for i, c in enumerate(line):
            if c == '"':
                quoted = not quoted
            elif not quoted and c == "<":
                depth += 1
            elif not quoted and c == ">":
                depth = max(0, depth - 1)
            elif c == "," and depth == 0 and not quoted:
                out.append(line[start:i].strip())
                start = i + 1
        out.append(line[start:].strip())
    return [e for e in out if e]


def is_loose_route(entry: str) -> bool:
    """RFC 3261 19.1.1: a route URI with the `lr` parameter is a loose router."""
    return bool(re.search(r";\s*lr\s*(?:;|>|=|$)", contact_uri(entry), re.I))


def normalize_did(raw: str) -> str:
    """'+1 (415) 555-0101', '14155550101' and 'tel:+14155550101' all become '+14155550101'."""
    raw = raw.replace("tel:", "")
    plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" if plus or len(digits) >= 11 else "") + digits


def _number_from(number: str, context: str = "") -> str:
    """One dialled number in the one form it is compared in: '+' and digits when it is international, plain digits when it is national.

    `+1 (415) 555-0101`, `%2B14155550101`, `0014155550101`, `14155550101` and `4155550101;phone-context=+1` are all `+14155550101`;
    `4155550101` alone stays national, and `trunk01` or `s` is nothing."""
    n = unquote(number).strip()
    digits = re.sub(r"\D", "", n)
    if not digits or re.search(r"[^\W\d_]", n):  # a name with a digit in it (trunk01) dials nothing
        return ""
    if n.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:] if digits[2:] else ""
    if context.startswith("+") and re.sub(r"\D", "", context):
        return "+" + re.sub(r"\D", "", context) + digits
    return "+" + digits if len(digits) >= 11 else digits


def number_in_uri(uri: str) -> str:
    """The number a sip:, sips: or tel: URI (or a header value holding one) dials, or '' when it dials none (a trunk name, `s`, a host only)."""
    m = re.search(r"\b(sips?|tel):([^>\s]*)", uri, re.I)
    if not m:
        return ""
    scheme, rest = m.group(1).lower(), m.group(2)
    if scheme == "tel":
        user = rest
    else:
        user, at, _host = rest.partition("@")
        if not at:
            return ""
        user = user.split(":")[0]  # sip:user:password@host
    number, _, params = user.partition(";")
    context = re.search(r"(?:^|;)phone-context=([^;]+)", params, re.I)
    return _number_from(number, unquote(context.group(1)) if context else "")


def dialled_numbers(req: "SipMessage") -> list[str]:
    """What the caller dialled, as candidates in the order they are trusted: the Request-URI, then the To header. P-Called-Party-ID and
    P-Asserted-Identity are not used: the first is a hint a proxy may add and the second says who the caller is, not what was dialled."""
    out = []
    for candidate in (number_in_uri(req.request_uri), number_in_uri(req.get("to"))):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


WITHHELD_NUMBERS = {"266696687"}  # ANONYMOUS on a telephone keypad, which some carriers send in place of a number


def caller_is_withheld(caller: str) -> bool:
    """True when a normalised caller id says nothing about who is calling: empty (sip:anonymous@anonymous.invalid, a name, no user), a placeholder
    number, or only zeros. Two such calls are two callers, not one."""
    digits = caller.lstrip("+")
    return not digits or digits in WITHHELD_NUMBERS or set(digits) == {"0"}


def make_response(req: SipMessage, code: int, reason: str, *, to_tag: str = "", contact: str = "",
                  body: str = "", content_type: str = "", extra: list[tuple[str, str]] | None = None) -> SipMessage:
    headers = [("Via", v) for v in req.get_all("via")]
    if req.is_request and req.method == "INVITE" and 101 <= code < 300:  # a response that makes a dialog carries the proxies' Record-Route back, unchanged and in order
        headers.extend(("Record-Route", v) for v in req.get_all("record-route"))
    headers.append(("From", req.get("from")))
    to = req.get("to")
    if to_tag and "tag=" not in to:
        to += f";tag={to_tag}"
    headers.append(("To", to))
    headers.append(("Call-ID", req.call_id))
    headers.append(("CSeq", req.get("cseq")))
    if contact:
        headers.append(("Contact", f"<{contact}>"))
    headers.append(("Server", "sipclerk-lab"))
    headers.extend(extra or [])
    if body:
        headers.append(("Content-Type", content_type or "application/sdp"))
    return SipMessage(f"SIP/2.0 {code} {reason}", headers, body)


# ----------------------------------------------------------------------------- SDP

DTMF_NAME = "telephone-event"


@dataclass
class Sdp:
    host: str
    port: int
    payload_types: list[int]
    rtpmap: dict[int, str]
    dtmf_pt: int | None
    direction: str = "sendrecv"

    @property
    def codec_names(self) -> list[str]:
        names = [self.rtpmap[p].split("/")[0].upper() for p in self.payload_types if p in self.rtpmap]
        return [n for n in names if n != DTMF_NAME.upper()]


STATIC_RTPMAP = {0: "PCMU/8000", 8: "PCMA/8000"}


def parse_sdp(body: str) -> Sdp:
    host = ""
    port = 0
    pts: list[int] = []
    rtpmap: dict[int, str] = {}
    direction = "sendrecv"
    in_audio = False
    for ln in re.split(r"\r?\n", body):
        if ln.startswith("c=") and not in_audio:
            host = ln.split()[-1]
        elif ln.startswith("m="):
            parts = ln[2:].split()
            if parts[0] == "audio":
                in_audio = True
                port = int(parts[1])
                pts = [int(p) for p in parts[3:] if p.isdigit()]
            else:
                in_audio = False
        elif in_audio and ln.startswith("c="):
            host = ln.split()[-1]
        elif in_audio and ln.startswith("a=rtpmap:"):
            num, _, enc = ln[len("a=rtpmap:"):].partition(" ")
            rtpmap[int(num)] = enc.strip()
        elif ln.strip() in ("a=sendonly", "a=recvonly", "a=inactive", "a=sendrecv"):
            direction = ln.strip()[2:]
    if not host or not port:
        raise SipParseError("SDP has no audio media")
    for p in pts:
        if p in STATIC_RTPMAP and p not in rtpmap:
            rtpmap[p] = STATIC_RTPMAP[p]
    dtmf = next((p for p, enc in rtpmap.items() if enc.lower().startswith(DTMF_NAME)), None)
    return Sdp(host, port, pts, rtpmap, dtmf, direction)


def build_sdp(host: str, port: int, codecs: list[str], dtmf_pt: int | None = 101, session_id: int | None = None) -> str:
    from sipclerk.audio import CODEC_PAYLOAD_TYPES

    sid = session_id or int(secrets.token_hex(3), 16)
    pts = [CODEC_PAYLOAD_TYPES[c] for c in codecs]
    fmt = " ".join(str(p) for p in pts + ([dtmf_pt] if dtmf_pt is not None else []))
    lines = [
        "v=0",
        f"o=sipclerk {sid} {sid} IN IP4 {host}",
        "s=sipclerk",
        f"c=IN IP4 {host}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {fmt}",
    ]
    for c, p in zip(codecs, pts):
        lines.append(f"a=rtpmap:{p} {c}/8000")
    if dtmf_pt is not None:
        lines.append(f"a=rtpmap:{dtmf_pt} {DTMF_NAME}/8000")
        lines.append(f"a=fmtp:{dtmf_pt} 0-16")
    lines += ["a=ptime:20", "a=sendrecv"]
    return CRLF.join(lines) + CRLF


def negotiate(offer: Sdp, preference: list[str]) -> tuple[str, int] | None:
    """Pick the codec to answer with: our preference order among what the offer lists.

    Returns (codec name, the payload type the OFFERER used for it), or None when nothing matches
    (the call is then refused with 488).
    """
    offered = {}
    for p in offer.payload_types:
        enc = offer.rtpmap.get(p, "")
        offered.setdefault(enc.split("/")[0].upper(), p)
    for codec in preference:
        if codec in offered:
            return codec, offered[codec]
    return None
