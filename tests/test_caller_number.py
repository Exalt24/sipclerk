"""Who is calling is read one way: for the stored caller number, the block list and the one call per number rule.

The first version took the raw user part of the From header. A blocked number a carrier sent as ten digits was answered while the `+1` form got 603, and a caller
id with a parameter (`+14155550142;cpc=ordinary`, `;npdi`, `;rn=`) was stored as an empty number, so it escaped the block list and the one call per number rule.
Now `caller_number` reads a From header like a dialled number is read (parameters dropped, `sip`, `sips` and `tel`, plus, `00`, visual separators, ten digits
national) and `match_number` compares two readings with the national number rule the dialled number follows."""
import types

import pytest

from conftest import ROOT
from sipclerk import guard
from sipclerk.server import ClerkServer
from sipclerk.sip import message as sm
from test_pipeline_fast import ASH, finished, place, rig

NUMBER = "+14155550142"
# Every way a carrier or a PBX writes the same caller in a From header: (header value, what it reads as)
SHAPES = [
    ("<sip:+14155550142@carrier.example>;tag=1", NUMBER),
    ('"Jo Caller" <sip:+14155550142@carrier.example>;tag=1', NUMBER),
    ("sip:+14155550142@carrier.example;tag=1", NUMBER),
    ("<sips:+14155550142@carrier.example>;tag=1", NUMBER),
    ("<sip:+14155550142;cpc=ordinary@carrier.example>;tag=1", NUMBER),
    ("<sip:+14155550142;npdi;rn=+14155550000@carrier.example;user=phone>;tag=1", NUMBER),
    ("<sip:+14155550142;phone-context=+1@carrier.example>;tag=1", NUMBER),
    ("<sip:14155550142@carrier.example>;tag=1", NUMBER),
    ("<sip:%2B14155550142@carrier.example>;tag=1", NUMBER),
    ("<sip:0014155550142@carrier.example>;tag=1", NUMBER),
    ("<sip:+1-415-555-0142@carrier.example>;tag=1", NUMBER),
    ("<sip:+1.415.555.0142@carrier.example>;tag=1", NUMBER),
    ("<sip:+1(415)555-0142@carrier.example>;tag=1", NUMBER),
    ("<tel:+14155550142>;tag=1", NUMBER),
    ("<tel:+1-415-555-0142>;tag=1", NUMBER),
    ("<tel:+14155550142;isub=12>;tag=1", NUMBER),
    ("<tel:415-555-0142;phone-context=+1>;tag=1", NUMBER),
    ("<sip:4155550142;phone-context=+1@carrier.example;user=phone>;tag=1", NUMBER),
    ("<sip:4155550142@carrier.example>;tag=1", "4155550142"),  # ten digits and nothing says the country: it stays national, as a dialled number does
    ("<sip:415-555-0142@carrier.example>;tag=1", "4155550142"),
    ("<sip:4155550142;cpc=ordinary@carrier.example>;tag=1", "4155550142"),
    ("<tel:4155550142>;tag=1", "4155550142"),
]
# Callers that say nothing about who they are: every one of them is nobody in particular
WITHHELD = ["<sip:anonymous@anonymous.invalid>;tag=1", '"Anonymous" <sip:anonymous@anonymous.invalid;transport=udp>;tag=1', "<sip:Restricted@carrier.example>;tag=1",
            "<sip:unavailable;cpc=ordinary@carrier.example>;tag=1", "<sip:+266696687@carrier.example>;tag=1", "<sip:266696687;cpc=x@carrier.example>;tag=1",
            "<sip:0000000000;cpc=ordinary@carrier.example>;tag=1", "<sip:trunk01@carrier.example>;tag=1", "<sip:alice@pbx.example>;tag=1", "<sip:carrier.example>;tag=1", "<tel:>;tag=1"]
BLOCK_ENTRIES = ["+14155550142", "+1 (415) 555-0142", "14155550142", "0014155550142", "tel:+14155550142", "sip:+14155550142@carrier.example", "+14155550142;note=spam"]


def tenant(blocked):
    return types.SimpleNamespace(guard=types.SimpleNamespace(blocked_callers=list(blocked)))


def server(open_calls):
    s = ClerkServer.__new__(ClerkServer)
    s._answering = {}
    s.sessions = {f"s{i}": types.SimpleNamespace(caller=c, cfg=types.SimpleNamespace(tenant_id=t)) for i, (c, t) in enumerate(open_calls)}
    return s


@pytest.mark.parametrize("header,expected", SHAPES)
def test_every_shape_of_a_caller_id_reads_as_the_same_number(header, expected):
    assert sm.caller_number(header) == expected


@pytest.mark.parametrize("header", WITHHELD)
def test_a_caller_id_that_names_nobody_reads_as_nothing_and_is_withheld(header):
    n = sm.caller_number(header)
    assert sm.caller_is_withheld(n), (header, n)


@pytest.mark.parametrize("entry", BLOCK_ENTRIES)
@pytest.mark.parametrize("header,expected", SHAPES)
def test_a_blocked_number_is_blocked_whichever_way_the_firm_wrote_it_and_the_carrier_sends_it(header, expected, entry):
    assert guard.check_caller(sm.caller_number(header), tenant([entry])).action == "blocked_caller", (header, entry)


@pytest.mark.parametrize("header,expected", SHAPES)
def test_another_number_is_not_blocked(header, expected):
    assert guard.check_caller(sm.caller_number(header), tenant(["+14155550143", "+13055550142", "+442071234567"])).action == "allow"


@pytest.mark.parametrize("header", WITHHELD)
def test_a_withheld_caller_is_never_blocked_even_by_an_empty_or_a_name_entry(header):
    assert guard.check_caller(sm.caller_number(header), tenant(["", "anonymous", "+15550000001"])).action == "allow"


@pytest.mark.parametrize("first,second", [(a, b) for a, _ in SHAPES for b, _ in SHAPES])
def test_the_one_call_per_number_rule_sees_through_every_pairing_of_shapes(first, second):
    one, two = dict(SHAPES)[first], dict(SHAPES)[second]
    s = server([(sm.caller_number(first), "ashgrovepell")])
    assert s._already_on_this_line(sm.caller_number(second), "ashgrovepell"), (first, second, one, two)
    assert not s._already_on_this_line(sm.caller_number(second), "tidewaterquint")


@pytest.mark.parametrize("header", WITHHELD)
def test_two_withheld_callers_are_two_calls_whatever_parameters_they_carry(header):
    n = sm.caller_number(header)
    assert not server([(n, "ashgrovepell")])._already_on_this_line(n, "ashgrovepell")


def test_a_ten_digit_number_is_matched_only_when_exactly_one_international_number_ends_with_it():
    assert sm.match_number("4155550142", ["+14155550142"]) == "+14155550142"
    assert sm.match_number("+14155550142", ["4155550142"]) == "4155550142"
    assert sm.match_number("4155550142", ["+14155550142", "+494155550142"]) == ""  # two countries share the ten digits: nobody is picked
    assert sm.match_number("4155550142", ["+14155550143"]) == "" and sm.match_number("", ["+14155550142"]) == ""
    assert sm.match_number("+14155550142", ["+14155550142", "+494155550142"]) == "+14155550142"
    assert sm.match_number("+494155550142", ["4155550142"]) == "4155550142"
    assert sm.match_number("+14155550142", ["+494155550142"]) == ""


def test_the_blocked_number_of_the_shipped_firm_file_is_blocked_in_every_shape(ashgrove):
    for header in ("<sip:+15550000001@h>", "<sip:5550000001@h>", "<sip:+1-555-000-0001@h>", "<sip:0015550000001@h>", "<tel:+1-555-000-0001>",
                   "<sip:+15550000001;cpc=ordinary@h>", "<sip:%2B15550000001@h>", "<sip:555-000-0001;phone-context=+1@h;user=phone>"):
        assert guard.check_caller(sm.caller_number(header), ashgrove.config).action == "blocked_caller", header
    assert guard.check_caller(sm.caller_number("<sip:+15550000002@h>"), ashgrove.config).action == "allow"


# ---- on the wire: a real INVITE through the line

async def test_a_blocked_number_sent_as_ten_digits_is_declined_like_the_international_form():
    async with rig([]) as r:
        for caller in ("+15550000001", "5550000001", "+15550000001;cpc=ordinary", "0015550000001"):
            call, status = await place(r, ASH, caller)
            assert call is None and status == 603, (caller, status)
        assert r.server.counters["blocked_caller"] == 4 and not r.server.sessions


async def test_a_caller_id_with_a_parameter_is_stored_as_the_number_and_counts_as_one_caller():
    async with rig(["yes"]) as r:
        first, s1 = await place(r, ASH, "+14155550195;cpc=ordinary")
        assert s1 == 200
        second, s2 = await place(r, ASH, "+14155550195")
        assert second is None and s2 == 486 and r.server.counters["duplicate_call"] == 1
        third, s3 = await place(r, ASH, "4155550195;npdi")
        assert third is None and s3 == 486 and r.server.counters["duplicate_call"] == 2
        await first.hangup()
        res = await finished(r, "+14155550195")
        assert res.record["caller_number"] == "+14155550195"


def test_the_shipped_tenants_still_load():
    from sipclerk.tenants import TenantRegistry

    assert len(TenantRegistry.from_dir(ROOT / "tenants").all) == 2
