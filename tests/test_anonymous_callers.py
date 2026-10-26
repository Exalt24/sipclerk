"""A withheld caller id is nobody in particular: two anonymous callers on one firm's line are two calls, while one real number on the line twice is one
caller who is already in a call. The first version compared caller ids as plain strings, and `sip:anonymous@anonymous.invalid` is the empty string, so
the second anonymous caller was turned away with 486 Busy Here while the line had room."""
import types

import pytest

from sipclerk.server import ClerkServer
from sipclerk.sip import message as sm

ASH, TIDE = "ashgrovepell", "tidewaterquint"


def server(registry, open_calls):
    s = ClerkServer.__new__(ClerkServer)
    s._answering = {}  # the rule needs only the open sessions
    s.sessions = {f"s{i}": types.SimpleNamespace(caller=caller, cfg=types.SimpleNamespace(tenant_id=tenant)) for i, (caller, tenant) in enumerate(open_calls)}
    return s


@pytest.mark.parametrize("caller", ["", "+266696687", "266696687", "0", "+0000000", "0000000000"])
def test_a_withheld_caller_id_is_never_a_duplicate_of_another_withheld_one(registry, caller):
    assert sm.caller_is_withheld(caller)
    assert not server(registry, [(caller, ASH)])._already_on_this_line(caller, ASH)


@pytest.mark.parametrize("caller", ["+14155550192", "+442071234567", "4155550192", "+12665550001"])
def test_a_real_number_already_in_a_call_on_the_same_line_is_a_duplicate(registry, caller):
    assert not sm.caller_is_withheld(caller)
    s = server(registry, [(caller, ASH)])
    assert s._already_on_this_line(caller, ASH)
    assert not s._already_on_this_line(caller, TIDE), "the same number on the other firm's line is a different call"
    assert not s._already_on_this_line("+14155550193", ASH)


def test_the_withheld_forms_a_carrier_sends_all_read_as_withheld():
    def caller_of(frm):
        req = sm.parse(f"INVITE sip:+14155550101@h SIP/2.0\r\nVia: SIP/2.0/UDP h;branch=z9hG4bKa\r\nFrom: {frm};tag=1\r\nTo: <sip:+14155550101@h>\r\nCall-ID: a@h\r\nCSeq: 1 INVITE\r\n\r\n".encode())
        return sm.caller_number(req.get("from"))

    for frm in ('"Anonymous" <sip:anonymous@anonymous.invalid>', "<sip:Restricted@carrier.example>", "<sip:unavailable@h>", "<sip:+266696687@h>", "<sip:alice@pbx.example>",
                '"Private" <sip:private@h>'):
        assert sm.caller_is_withheld(caller_of(frm)), frm
    assert not sm.caller_is_withheld(caller_of("<sip:+14155550142@h>"))


# ---- the class, enumerated: every way a carrier withholds the caller id x every way the callers already on the line did x how many are already on it
#
# GROUNDING (docs/DECISIONS.md): "a withheld caller id is nobody in particular: an empty one (`sip:anonymous@anonymous.invalid`), the keypad placeholder `266696687` and a
# number of only zeros never count as a duplicate, so two anonymous callers are two calls." and "One caller number holds one call at a time on a firm's line".
# WHY: two people phone the firm within the same hour with their number hidden, which is the ordinary case for a person ringing a law firm about a private matter. The second
# one hears 486 Busy Here while the line has room, and a person with a hidden number is the one who cannot be called back.
import itertools

WITHHELD_FROM_HEADERS = ['"Anonymous" <sip:anonymous@anonymous.invalid>', "<sip:Restricted@carrier.example>", "<sip:unavailable@h>", "<sip:+266696687@h>",
                         "<sip:266696687@carrier.example;user=phone>", "<sip:0000000000@h>", "<sip:anonymous@anonymous.invalid;transport=udp>", '"Private" <sip:private@h>',
                         "<sip:alice@pbx.example>", "<sip:carrier.example>"]
OPEN_COUNTS = [1, 2, 5]  # how many callers are already on the line
REAL_HEADERS = ["<sip:+14155550192@carrier.example>", "<sip:4155550192@carrier.example>", "<sip:+1-415-555-0192@h>", "<tel:+14155550192>"]
OTHERS_ON_THE_LINE = [0, 1, 2, 5]  # withheld callers already on the line next to the one real number


def from_header_number(frm: str) -> str:
    return sm.caller_number(f"{frm};tag=1")


# CLASS-TABLE: a withheld caller id read as one caller, for every spelling of the new call, every spelling already on the line and every count
def test_class_a_withheld_caller_is_never_a_duplicate_however_it_is_spelled_and_however_many_already_hold_the_line_while_a_real_number_always_is(registry):
    wrong = []
    for new, existing, count in itertools.product(WITHHELD_FROM_HEADERS, WITHHELD_FROM_HEADERS, OPEN_COUNTS):
        s = server(registry, [(from_header_number(existing), ASH)] * count)
        if s._already_on_this_line(from_header_number(new), ASH):
            wrong.append(("withheld", new, existing, count))
    # the rows that must pass: a real number is one caller in whatever written form it comes back in, next to any number of withheld callers
    for first, again, others in itertools.product(REAL_HEADERS, REAL_HEADERS, OTHERS_ON_THE_LINE):
        anonymous = [(from_header_number(WITHHELD_FROM_HEADERS[i % len(WITHHELD_FROM_HEADERS)]), ASH) for i in range(others)]
        s = server(registry, anonymous + [(from_header_number(first), ASH)])
        if not s._already_on_this_line(from_header_number(again), ASH):  # every REAL_HEADERS entry is the same caller, the ten digit one by the national reading
            wrong.append(("real number twice", first, again, others))
        if s._already_on_this_line(from_header_number(again), TIDE):
            wrong.append(("same number on the other firm", first, again, others))
    kinds = {k: sum(1 for w in wrong if w[0] == k) for k in {w[0] for w in wrong}}
    assert not wrong, f"{len(wrong)} cells read the caller id the wrong way {kinds}: {wrong[:6]}"
