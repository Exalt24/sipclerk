"""Two classes a last reader found by probing the parsers: words said after a spoken email address were glued onto it (`jordan at example dot com please` was stored as jordan@example.complease, and the address is never read back), and a numeric date (`12/15/2023`) or a compound relative one (`three years and two months ago`) was stored as a year alone or as its last unit."""
import itertools
from datetime import date, timedelta

import pytest

from sipclerk.intake import parsers
from sipclerk.intake.parsers import _add_months

TODAY = date(2026, 10, 27)

# CLASS-TABLE: every way of saying an address, every ending that follows it and every lead-in, stored as the address and nothing after it
LOCALS = ("jordan", "jordan dot reyes", "j reyes", "jordan underscore reyes")
DOMAINS = ("example dot com", "gmail dot com", "firm dot co dot uk", "example dot org")
AFTER = ("", "please", "thanks", "thank you", "that's it", "is my email", "is my email address", "okay", "thank you very much")
LEADS = ("", "my email is", "sure", "it's")


def _expected(local, domain):
    return (local.replace(" dot ", ".").replace(" underscore ", "_").replace(" ", "") + "@" + domain.replace(" dot ", ".")).lower()


def test_class_words_after_a_spoken_email_are_not_part_of_it():
    bad = []
    for local, domain, after, lead in itertools.product(LOCALS, DOMAINS, AFTER, LEADS):
        said = " ".join(w for w in (lead, f"{local} at {domain}", after) if w)
        r = parsers.parse_email(said)
        if not r.ok or r.value != _expected(local, domain):
            bad.append((said, r.value if r.ok else r.reason))
    assert not bad, bad[:8]


@pytest.mark.parametrize("said", ["jordan at example dot c o m", "jordan at example dot c o m please"])
def test_a_spelled_ending_keeps_its_letters(said):
    assert parsers.parse_email(said).value == "jordan@example.com"


# CLASS-TABLE: a numeric date in every month at three places in the month, three years, four ways of writing it and three lead-ins
YEARS = (2024, 2025, 2026)
FORMS = ("{m}/{d}/{y}", "{m:02d}/{d:02d}/{y}", "{m}-{d}-{y2:02d}", "{m}/{d}/{y2:02d}")
WRAPS = ("{x}", "it was on {x}", "{x} I think")


def test_class_a_numeric_date_is_stored_as_that_date():
    bad = []
    for y, m, d, form, wrap in itertools.product(YEARS, range(1, 13), (1, 15, 28), FORMS, WRAPS):
        said = wrap.format(x=form.format(m=m, d=d, y=y, y2=y % 100))
        r = parsers.parse_date(said, TODAY)
        want = date(y, m, d)
        if want > TODAY:
            if r.ok or r.reason != "in_the_future":
                bad.append((said, r.value if r.ok else r.reason))
        elif not r.ok or r.value != want.isoformat():
            bad.append((said, r.value if r.ok else r.reason))
    assert not bad, bad[:8]


@pytest.mark.parametrize("said,reason", [("13/40/2025", "not_a_date"), ("2/30/2025", "not_a_date"), ("12/15/2031", "in_the_future")])
def test_a_numeric_date_that_cannot_be_is_refused(said, reason):
    r = parsers.parse_date(said, TODAY)
    assert not r.ok and r.reason == reason


def test_a_numeric_date_older_than_the_question_allows_is_too_old():
    r = parsers.parse_date("1/1/2020", TODAY, max_age_days=365)
    assert not r.ok and r.reason == "too_old"


# CLASS-TABLE: two units said together, every pair of units in order, against the date that much back
WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}
UNITS = {"year": lambda d, k: _add_months(d, 12 * k), "month": lambda d, k: _add_months(d, k), "week": lambda d, k: d - timedelta(weeks=k), "day": lambda d, k: d - timedelta(days=k)}
ORDER = ["year", "month", "week", "day"]


def test_class_two_units_ago_are_both_counted():
    bad = []
    pairs = [(a, b) for i, a in enumerate(ORDER) for b in ORDER[i + 1:]]
    for (big, small), k1, k2, joiner in itertools.product(pairs, (1, 2, 3), (1, 2, 4), (" and ", " ")):
        said = f"about {WORDS[k1]} {big}{'s' if k1 > 1 else ''}{joiner}{WORDS[k2]} {small}{'s' if k2 > 1 else ''} ago"
        r = parsers.parse_date(said, TODAY)
        want = UNITS[small](UNITS[big](TODAY, k1), k2)
        if not r.ok or r.value != want.isoformat():
            bad.append((said, r.value if r.ok else r.reason, want.isoformat()))
    assert not bad, bad[:6]


# CLASS-TABLE: every way of saying a name, every courtesy that can follow it and every lead-in, stored as the name and nothing after it
NAMES = ("Jordan Mitchell", "Mary Anne Reyes", "Daniel Alexis Cruz", "Siobhan O'Neill")
COURTESY = ("", " please", " thanks", " thank you", " thank you very much", " that's it", " that is all", " okay")
NAME_LEADS = ("", "my name is ", "this is ", "it's ")


def test_class_courtesy_said_after_a_name_is_not_part_of_it():
    bad = []
    for name, courtesy, lead in itertools.product(NAMES, COURTESY, NAME_LEADS):
        said = f"{lead}{name}{courtesy}"
        r = parsers.parse_name(said)
        if not r.ok or r.value != name:
            bad.append((said, r.value if r.ok else r.reason))
    assert not bad, bad[:8]


# CLASS-TABLE: the property behind the three fixes above, over every spoken field: a courtesy said after an answer never changes what is stored
FIELDS = {
    "phone": (parsers.parse_phone, ("415 555 0100", "four one five five five five zero one zero zero", "(415) 555-0100")),
    "name": (parsers.parse_name, ("Jordan Mitchell", "Mary Anne Reyes")),
    "email": (parsers.parse_email, ("jordan at example dot com", "j reyes at gmail dot com")),
    "date": (lambda t: parsers.parse_date(t, TODAY), ("last tuesday", "October 3rd 2025", "12/15/2023", "two years ago", "2025-03-04")),
}


def test_class_a_courtesy_after_any_answer_never_changes_what_is_stored():
    bad = []
    for (field, (parse, answers)), courtesy in itertools.product(FIELDS.items(), COURTESY[1:]):
        for answer in answers:
            plain, said = parse(answer), parse(answer + courtesy)
            if not plain.ok or not said.ok or plain.value != said.value:
                bad.append((field, answer + courtesy, said.value if said.ok else said.reason, plain.value if plain.ok else plain.reason))
    assert not bad, bad[:8]


# CLASS-TABLE: every number from one to ninety nine said in words, with a space or a hyphen, against every unit it can be counted in
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}
_AGO = {"day": lambda d, k: d - timedelta(days=k), "week": lambda d, k: d - timedelta(weeks=k), "month": lambda d, k: _add_months(d, k), "year": lambda d, k: _add_months(d, 12 * k)}


def _words(k, sep):
    if k < 20:
        return _ONES[k]
    return _TENS[k // 10 * 10] + (sep + _ONES[k % 10] if k % 10 else "")


def test_class_a_number_said_in_words_is_that_many_units_ago():
    bad = []
    for k, unit, sep in itertools.product(range(1, 100), _AGO, (" ", "-")):
        said = f"{_words(k, sep)} {unit}{'' if k == 1 else 's'} ago"
        r = parsers.parse_date(said, TODAY)
        want = _AGO[unit](TODAY, k).isoformat()
        if not r.ok or r.value != want:
            bad.append((said, r.value if r.ok else r.reason, want))
    assert not bad, bad[:8]
