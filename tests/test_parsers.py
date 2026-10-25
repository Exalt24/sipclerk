from datetime import date

import pytest

from sipclerk.intake import parsers as p
from sipclerk.tenants import PracticeArea

TODAY = date(2026, 10, 27)
AREAS = [PracticeArea(id="personal_injury", label="personal injury", keywords=["car accident", "crash", "injured"]),
         PracticeArea(id="employment", label="employment", keywords=["fired", "boss", "wages"])]


@pytest.mark.parametrize("text,value", [
    ("my name is jordan reyes", "Jordan Reyes"),
    ("Hi, this is Maria Del Rio.", "Maria Del Rio"),
    ("It's Sean O'Neil", "Sean O'Neil"),
    ("Dana Whitfield", "Dana Whitfield"),
])
def test_name_ok(text, value):
    r = p.parse_name(text)
    assert r.ok and r.value == value


@pytest.mark.parametrize("text,reason", [
    ("Jordan", "need_last_name"), ("yes", "not_a_name"), ("I was in a car accident last week with my boss", "not_a_name"),
    ("", "empty"), ("one two three", "not_a_name") if False else ("a b c d e f", "not_a_name"),
])
def test_name_rejected(text, reason):
    r = p.parse_name(text)
    assert not r.ok and r.reason == reason


@pytest.mark.parametrize("text,value", [
    ("four one five five five five oh one two three", "+14155550123"),
    ("415-555-0123", "+14155550123"),
    ("one four one five five five five zero one two three", "+14155550123"),
    ("four one five double five five oh one two three", "+14155550123"),
    ("my number is (415) 555 0123", "+14155550123"),
    ("415-555-0123.", "+14155550123"),
    ("Four, one, five. Five, five, five. Zero, one, two, three.", "+14155550123"),
])
def test_phone_ok(text, value):
    r = p.parse_phone(text)
    assert r.ok and r.value == value


@pytest.mark.parametrize("text,reason", [
    ("four one five five five five", "too_short"),
    ("415 555 0123 4567", "too_long"),
    ("012 555 0123", "not_a_phone_number"),
    ("415 155 0123", "not_a_phone_number"),
    ("I do not know", "too_short"),
])
def test_phone_rejected(text, reason):
    assert p.parse_phone(text).reason == reason


def test_phone_digits_from_keypad():
    assert p.validate_phone_digits("4155550123").value == "+14155550123"
    assert p.validate_phone_digits("14155550123").value == "+14155550123"
    assert p.validate_phone_digits("41555").reason == "too_short"


def test_phone_readback_is_spoken_in_groups():
    assert p.speak_digits("+14155550123") == "four one five, five five five, zero one two three"


@pytest.mark.parametrize("text,value", [
    ("jordan dot reyes at example dot com", "jordan.reyes@example.com"),
    ("it's j.reyes@example.org", "j.reyes@example.org"),
    ("sam underscore lee at mail dot test", "sam_lee@mail.test"),
])
def test_email(text, value):
    r = p.parse_email(text)
    assert r.ok and r.value == value


def test_email_rejects_non_address():
    assert p.parse_email("I do not have one").reason == "not_an_email"


@pytest.mark.parametrize("text,iso", [
    ("March 3rd 2026", "2026-03-03"),
    ("it was on 14 February 2026", "2026-02-14"),
    ("about two weeks ago", "2026-10-13"),
    ("yesterday", "2026-10-26"),
    ("three months ago", "2026-07-27"),
    ("last year", "2025-10-27"),
    ("last Tuesday", "2026-10-20"),
    ("in March 2025", "2025-03-01"),
    ("2024", "2024-06-30"),
    ("a couple of days ago", "2026-10-25"),
])
def test_date_ok(text, iso):
    r = p.parse_date(text, TODAY)
    assert r.ok and r.value == iso, r


def test_date_without_year_means_the_latest_such_month_not_in_the_future():
    assert p.parse_date("in December", TODAY).value == "2025-12-01"
    assert p.parse_date("in September", TODAY).value == "2026-09-01"


def test_date_future_and_too_old_and_garbage():
    assert p.parse_date("March 3rd 2027", TODAY).reason == "in_the_future"
    for said in ("next year", "it will be next week", "tomorrow", "next Friday"):
        assert p.parse_date(said, TODAY).reason == "in_the_future", said
    assert p.parse_date("in 1960", TODAY, max_age_days=18250).reason == "too_old"
    assert p.parse_date("purple", TODAY).reason == "not_a_date"
    assert p.parse_date("February 30 2026", TODAY).reason == "not_a_date"


@pytest.mark.parametrize("text,value,remainder", [
    ("yes", True, ""), ("yeah that's right", True, ""), ("Correct.", True, ""),
    ("no", False, ""), ("no it's jordan reyes", False, "jordan reyes"), ("nope, wrong", False, ""),
])
def test_yesno(text, value, remainder):
    r = p.parse_yesno(text)
    assert r.ok and r.value is value
    assert remainder in r.remainder or remainder == ""


def test_yesno_ambiguous_and_unclear():
    assert p.parse_yesno("yes no wait").reason == "ambiguous"
    assert p.parse_yesno("maybe tomorrow").reason == "unclear"
    assert p.parse_yesno("").reason == "empty"


def test_area_by_keyword_and_ambiguity():
    assert p.parse_area("I was in a car accident", AREAS).value == "personal_injury"
    assert p.parse_area("my boss fired me", AREAS).value == "employment"
    assert p.parse_area("my boss caused a crash", AREAS).reason == "ambiguous"
    assert p.parse_area("my landlord", AREAS).reason == "no_match"


def test_option_and_text():
    assert p.parse_option("we live in Florida", ["Florida", "Georgia"]).value == "Florida"
    assert p.parse_option("Ohio", ["Florida"]).reason == "no_match"
    assert p.parse_text("um so the truck hit my car at the light", 5).ok
    assert p.parse_text("it hurts", 5).reason == "too_short"


def test_skip_only_for_optional_questions():
    from sipclerk.tenants import Question

    opt = Question(id="email", type="email", prompt="Give an email or skip.", required=False)
    req = opt.model_copy(update={"required": True})
    assert p.parse_for(opt, "I would rather not", today=TODAY, areas=AREAS).skipped
    assert not p.parse_for(req, "I would rather not", today=TODAY, areas=AREAS).ok


def test_masking_for_the_console_copy():
    t = "Call me on 415-555-0123 or jo@example.com. Or four one five five five five oh one two three."
    m = p.mask_for_transcript(t)
    assert "555" not in m and "example" not in m and "four one five" not in m
    assert "[number]" in m and "[email]" in m


def test_readback_forms():
    from sipclerk.tenants import Question

    d = Question(id="d", type="date", prompt="When did it happen?")
    assert p.readback(d, "2026-03-03") == "March 3, 2026"
    e = Question(id="e", type="email", prompt="Your email address?")
    assert p.readback(e, "a.b@x.com") == "a dot b at x dot com"
