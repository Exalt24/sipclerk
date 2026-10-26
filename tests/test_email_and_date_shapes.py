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
