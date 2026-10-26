"""The cases of the blind corpus of output round 3, written as functions that drive the real code (machine, parsers, guard, SIP parser, outbox).

They are the adversary's probes with one change: the code under test is this repository, not a copy. `tests/test_corpus_blind_3.py` replays every row of
`tests/corpus/sipclerk_corpus_blind_3.jsonl` through the function of the same id and checks it against what the row says should happen.
"""
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

from sipclerk import delivery, guard
from sipclerk.intake import parsers
from sipclerk.intake.machine import IntakeMachine, Say, StartTransfer
from sipclerk.sip import message as sm
from sipclerk.tenants import TenantRegistry

ROOT = Path(__file__).resolve().parent.parent

REG = TenantRegistry.from_dir(ROOT / "tenants")
ASH, TIDE = REG.by_id("ashgrovepell"), REG.by_id("tidewaterquint")
IN_HOURS = datetime(2026, 10, 27, 17, 30)  # Tue 10:30 LA, 13:30 Miami (same instant the repo's tests use)
AFTER = datetime(2026, 11, 1, 3, 0)


def says(acts):
    return [a.text for a in acts if isinstance(a, Say)]


def drive(tenant, utterances, now=IN_HOURS):
    m = IntakeMachine(tenant, "probe1", "+14155550142", now)
    out = says(m.begin())
    for u in utterances:
        if isinstance(u, tuple) and u[0] == "dtmf":
            for d in u[1]:
                out += [f"<{x}>" for x in says(m.on_dtmf(d))]
        elif u == "<silence>":
            out += [f"<{x}>" for x in says(m.on_silence_timeout())]
        else:
            acts = m.handle_text(u)
            out += says(acts)
            if any(isinstance(a, StartTransfer) for a in acts):
                out.append("<StartTransfer>")
    return m, out


ASH_HEAD = ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no"]
TIDE_HEAD = ["Jordan Reyes", "yes", "three oh five five five five oh one four two", "yes"]


def fields(m):
    return {k: v.value for k, v in m.fields.items()}


CASES = {}


def case(fn):
    CASES[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------ hours (fixed UTC offset vs the firm's time zone)
@case
def hours_ash_winter_afternoon():
    # Monday 2026-11-02 16:30 in Los Angeles is PST (UTC-8) = 2026-11-03 00:30 UTC. The firm is open 09:00-17:00.
    now = datetime(2026, 11, 3, 0, 30)
    m, out = drive(ASH, ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "yes"], now)
    return {"is_open": ASH.config.is_open(now), "said": out, "outcome": m.outcome, "tasks": m.tasks}


@case
def hours_ash_winter_morning():
    # Monday 2026-11-02 08:30 PST = 16:30 UTC: the office is not open yet.
    now = datetime(2026, 11, 2, 16, 30)
    return {"is_open": ASH.config.is_open(now)}


@case
def hours_tide_winter_evening():
    # Monday 2026-11-02 17:30 in Miami is EST (UTC-5) = 22:30 UTC. Open 08:00-18:00.
    now = datetime(2026, 11, 2, 22, 30)
    m, out = drive(TIDE, [], now)
    return {"is_open": TIDE.config.is_open(now), "said": out}


@case
def hours_ash_in_hours_today():
    m, out = drive(ASH, [], IN_HOURS)
    return {"is_open": ASH.config.is_open(IN_HOURS), "said": out}


# ------------------------------------------------------------------ email (Tidewater: optional, no read-back)
def tide_email(answer):
    m, out = drive(TIDE, TIDE_HEAD + [answer])
    return {"email": m.fields["email"].value, "skipped": m.fields["email"].skipped, "last_said": out[-1], "confirm": TIDE.config.question("email").confirm}


@case
def email_sure_its():
    return tide_email("Sure, it's jordan.reyes@gmail.com")


@case
def email_yeah_spoken():
    return tide_email("Yeah, jordan dot reyes at gmail dot com")


@case
def email_um():
    return tide_email("Um, jordan reyes at gmail dot com")


@case
def email_plain_ok():
    return tide_email("jordan dot reyes at gmail dot com")


@case
def email_my_email_is_ok():
    return tide_email("My email is jordan.reyes@gmail.com")


@case
def email_skip_ok():
    return tide_email("Skip")


@case
def email_dont_have_ok():
    return tide_email("I don't have one")


@case
def email_passmore():
    return tide_email("jordan dot passmore at gmail dot com")


# ------------------------------------------------------------------ dates (Ashgrove: no read-back on the date)
def ash_date(answer):
    m, out = drive(ASH, ASH_HEAD + ["I was in a car accident", answer])
    return {"incident_date": m.fields["incident_date"].value, "today": m.today.isoformat(), "next_said": out[-1],
            "confirm": ASH.config.question("incident_date").confirm}


@case
def date_march_last_year():
    return ash_date("March of last year")


@case
def date_may_have_been_august():
    return ash_date("It may have been in August")


@case
def date_third_of_march():
    return ash_date("On the third of March")


@case
def date_fifth_of_last_month():
    return ash_date("The 5th of last month")


@case
def date_two_weeks_ago_ok():
    return ash_date("about two weeks ago")


@case
def date_last_friday_ok():
    return ash_date("last Friday")


@case
def date_march_3rd_ok():
    return ash_date("March 3rd")


@case
def date_june_2024_ok():
    return ash_date("June 2024")


# ------------------------------------------------------------------ keypad
@case
def keypad_ten_digits_then_pound_ok():
    m, out = drive(ASH, ASH_HEAD[:3] + [("dtmf", "4155550123#")])
    return {"pending": m.pending, "state": m.state.value, "said": out[-1]}


@case
def keypad_ten_digits_no_pound():
    m, out = drive(ASH, ASH_HEAD[:3] + [("dtmf", "4155550123")])
    after_digits = {"state": m.state.value, "said_after_digits": out[-1], "keypad_len": len(m.keypad)}
    m2, out2 = drive(ASH, ASH_HEAD[:3] + [("dtmf", "4155550123"), "<silence>", ("dtmf", "4155550123#")])
    return {**after_digits, "then_timeout_and_retype": out2[-3:], "retries": m2.fields["callback_number"].retries,
            "value": m2.fields["callback_number"].value, "pending": m2.pending}


@case
def keypad_with_leading_one_ok():
    m, out = drive(ASH, ASH_HEAD[:3] + [("dtmf", "14155550123")])
    return {"pending": m.pending, "said": out[-1]}


# ------------------------------------------------------------------ practice area keywords
def tide_area(answer):
    m, out = drive(TIDE, TIDE_HEAD + ["skip", answer])
    return {"practice_area": m.fields["practice_area"].value, "next_said": out[-1], "outcome": m.outcome}


@case
def area_tide_husband_will_not():
    return tide_area("My husband will not let me see our kids")


@case
def area_tide_will_not_pay_support():
    return tide_area("My ex will not pay the child support he owes")


@case
def area_tide_lose_visa():
    return tide_area("I think I will lose my visa next month")


@case
def area_tide_divorce_ok():
    return tide_area("I want to file for divorce")


@case
def area_tide_dui_declined_ok():
    return tide_area("My son was arrested for a DUI")


@case
def area_ash_car_ok():
    m, out = drive(ASH, ASH_HEAD + ["I was in a car accident"])
    return {"practice_area": m.fields["practice_area"].value, "next_said": out[-1]}


@case
def area_ash_divorce_declined_ok():
    m, out = drive(ASH, ASH_HEAD + ["I need help with my divorce"])
    return {"outcome": m.outcome, "said": out[-1]}


# ------------------------------------------------------------------ recording consent
def consent(answer):
    m, out = drive(ASH, [answer])
    return {"recording": m.recording, "said": out[-2:]}


@case
def consent_no_problem():
    return consent("No problem")


@case
def consent_no_thats_fine():
    return consent("No, that's fine")


@case
def consent_yes_ok():
    return consent("Yes, that's fine")


@case
def consent_no_ok():
    return consent("No, please don't record it")


# ------------------------------------------------------------------ guard on ordinary words
@case
def existing_client_lawyer_elsewhere_after_hours():
    m, out = drive(ASH, ASH_HEAD + ["I was fired from my job", "two months ago",
                                     "My lawyer is not returning my calls so I want a new firm for my wrongful termination"], AFTER)
    return {"outcome": m.outcome, "escalation": m.escalation, "description": m.fields["description"].value, "said": out[-1]}


@case
def emergency_live_ok():
    m, out = drive(ASH, ASH_HEAD + ["My husband is not breathing"])
    return {"escalation": m.escalation, "said": out[-2:]}


@case
def emergency_room_story_ok():
    m, out = drive(ASH, ASH_HEAD + ["I was in a car accident", "last Friday",
                                     "A truck hit me and I was taken to the emergency room with a broken arm"])
    return {"outcome": m.outcome, "escalation": m.escalation}


@case
def legal_advice_once_ok():
    m, out = drive(ASH, ASH_HEAD + ["Do I have a case against my boss?"])
    return {"said": out[-2:], "state": m.state.value, "escalation": m.escalation}


@case
def full_intake_ash_ok():
    m, out = drive(ASH, ASH_HEAD + ["I was in a car accident", "about two weeks ago", "A truck ran a red light and hit my car on Main Street"])
    return {"outcome": m.outcome, "fields": fields(m)}


@case
def full_intake_tide_ok():
    m, out = drive(TIDE, TIDE_HEAD + ["jordan dot reyes at gmail dot com", "I want to file for divorce", "Florida", "no",
                                      "We separated last year and need to split the house"])
    return {"outcome": m.outcome, "fields": fields(m)}


@case
def name_spanish_ok():
    return {"parsed": parsers.parse_name("Me llamo... my name is José García").__dict__}


@case
def name_oconnor_ok():
    return {"parsed": parsers.parse_name("It's Sarah O'Connor").__dict__}


@case
def phone_parens_ok():
    return {"parsed": parsers.parse_phone("(415) 555-0123").__dict__}


@case
def phone_plus_one_ok():
    return {"parsed": parsers.parse_phone("+1 415 555 0123").__dict__}


@case
def name_correction_ok():
    m, out = drive(ASH, ["yes", "Jordan Reyes", "No, it's Jordan Rees", "yes"])
    return {"full_name": m.fields["full_name"].value, "said": out[-2:]}


# ------------------------------------------------------------------ SIP shapes (RFC 3261 section 24 style, RFC 4566 example SDP)
RFC_INVITE = (
    "INVITE sip:+14155550101@line.example.com;user=phone SIP/2.0\r\n"
    "Via: SIP/2.0/UDP pc33.atlanta.com;branch=z9hG4bK776asdhds\r\n"
    "Max-Forwards: 70\r\n"
    "To: <sip:+14155550101@line.example.com;user=phone>\r\n"
    "From: \"Alice\" <sip:+14155550142@atlanta.com;user=phone>;tag=1928301774\r\n"
    "Call-ID: a84b4c76e66710@pc33.atlanta.com\r\n"
    "CSeq: 314159 INVITE\r\n"
    "Contact: <sip:alice@pc33.atlanta.com>\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: {clen}\r\n\r\n{body}")
RFC4566_SDP = ("v=0\r\no=jdoe 2890844526 2890842807 IN IP4 10.47.16.5\r\ns=SDP Seminar\r\nc=IN IP4 224.2.17.12/127\r\n"
               "t=2873397496 2873404696\r\nm=audio 49170 RTP/AVP 0 8 101\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\n")


@case
def sip_rfc_invite_routes_ok():
    raw = RFC_INVITE.format(clen=len(RFC4566_SDP), body=RFC4566_SDP).encode()
    msg = sm.parse(raw)
    sdp = sm.parse_sdp(msg.body)
    t = REG.resolve(sm.dialled_numbers(msg))
    return {"tenant": t.config.tenant_id if t else None, "caller": sm.caller_number(msg.get("from")), "codec": sm.negotiate(sdp, ["PCMU", "PCMA"]),
            "dtmf_pt": sdp.dtmf_pt}


@case
def sip_compact_headers_ok():
    raw = ("INVITE sip:13055550102@10.0.0.5 SIP/2.0\r\nv: SIP/2.0/UDP 10.0.0.9:5060;branch=z9hG4bKx1\r\nt: <sip:13055550102@10.0.0.5>\r\n"
           "f: <sip:4155550142@10.0.0.9>;tag=99\r\ni: abc@10.0.0.9\r\nCSeq: 1 INVITE\r\nl: 0\r\n\r\n").encode()
    msg = sm.parse(raw)
    t = REG.resolve(sm.dialled_numbers(msg))
    return {"tenant": t.config.tenant_id if t else None, "caller": sm.caller_number(msg.get("from"))}


@case
def sip_wrong_number_404_ok():
    raw = RFC_INVITE.replace("+14155550101", "+14155550199").format(clen=0, body="").encode()
    msg = sm.parse(raw)
    return {"tenant": REG.resolve(sm.dialled_numbers(msg))}


@case
def sip_withheld_caller_ok():
    return {"caller": sm.caller_number("\"Anonymous\" <sip:anonymous@anonymous.invalid>;tag=1"),
            "withheld": sm.caller_is_withheld(sm.caller_number("<sip:anonymous@anonymous.invalid>;tag=1"))}


@case
def sip_cpc_param_ok():
    return {"caller": sm.caller_number("<sip:+14155550142;cpc=ordinary@carrier.example;user=phone>;tag=7")}


@case
def blocked_caller_ten_digits_ok():
    return {"verdict": guard.check_caller("<sip:5550000001@carrier>;tag=1", ASH.config).action}


@case
def name_jose_garcia_ok():
    return {"parsed": parsers.parse_name("My name is José García").__dict__}


@case
def existing_client_calling_about_my_case_after_no():
    # the caller has just answered "no" to "Are you already a client of the firm?"
    m, out = drive(ASH, ASH_HEAD + ["I'm calling about my case, I was rear-ended on the freeway last week"], AFTER)
    return {"existing_client_answer": m.fields["existing_client"].value, "outcome": m.outcome, "escalation": m.escalation,
            "practice_area": m.fields["practice_area"].value, "said": out[-1]}


@case
def existing_client_case_manager_in_hours():
    m, out = drive(ASH, ASH_HEAD + ["I was in a car accident", "three weeks ago",
                                     "My case manager at the hospital told me to call a lawyer about the crash"])
    return {"existing_client_answer": m.fields["existing_client"].value, "escalation": m.escalation, "description": m.fields["description"].value,
            "said": out[-2:]}


@case
def existing_client_real_yes_ok():
    m, out = drive(ASH, ASH_HEAD[:5] + ["yes"])
    return {"escalation": m.escalation, "said": out[-2:]}


# ------------------------------------------------------------------ bridge summary
@case
def bridge_summary_complete_intake():
    m, out = drive(ASH, ASH_HEAD + ["I was in a car accident", "about two weeks ago", "A truck ran a red light and hit my car on Main Street"])
    record = m.record()
    key = b"k" * 32
    with tempfile.TemporaryDirectory() as d:
        ob = delivery.Outbox(Path(d) / "outbox.jsonl", {}, keys=lambda t: key)
        # as call.py does: the bridge entry while the closing line is spoken, then file_call after the summary is written
        delivery.queue_call(ob, ASH.config, record, [], 0, only="bridge")
        delivery.queue_call(ob, ASH.config, record, [{"speaker": "CALLER", "text": "x"}], 80,
                            "Jordan Reyes reported a car accident two weeks ago; a truck ran a red light.")
        e = ob.entries["bridge:" + record["idempotency_key"]]
        return {"bridge_summary": e.payload["data"]["summary"], "entries": sorted(ob.entries)}


# ------------------------------------------------------------------ the cases the corpus holds that need no driver
@case
def sdp_g729_only():
    offer = sm.parse_sdp("v=0\r\no=- 1 1 IN IP4 10.0.0.9\r\ns=-\r\nc=IN IP4 10.0.0.9\r\nt=0 0\r\nm=audio 4000 RTP/AVP 18\r\na=rtpmap:18 G729/8000\r\n")
    return {"codec": sm.negotiate(offer, ["PCMU", "PCMA"])}


@case
def sip_missing_callid():
    try:
        sm.parse(b"INVITE sip:+14155550101@x SIP/2.0\r\nVia: SIP/2.0/UDP a;branch=z9hG4bK1\r\nFrom: <sip:a@b>;tag=1\r\nTo: <sip:+14155550101@x>\r\nCSeq: 1 INVITE\r\n\r\n")
    except sm.SipParseError as e:
        return {"error": str(e)}
    return {"error": ""}


@case
def phone_area_code_zero():
    return {"parsed": parsers.parse_phone("oh one five five five five oh one two three").__dict__}


@case
def date_next_tuesday():
    return {"parsed": parsers.parse_date("next Tuesday", date(2026, 10, 27)).__dict__}


@case
def name_need_lawyer():
    return {"parsed": parsers.parse_name("I need a lawyer").__dict__}


@case
def dialled_trunk01():
    return {"tenant": REG.resolve([sm.number_in_uri("sip:trunk01@10.0.0.5")])}


@case
def out_guard_promise():
    return {"verdict": guard.check_output("You will definitely win this case.").__dict__}


@case
def legal_advice_twice():
    m, said = drive(ASH, ASH_HEAD + ["Do I have a case?", "What are my chances?"])
    return {"escalation": m.escalation, "said": said[-2:]}
