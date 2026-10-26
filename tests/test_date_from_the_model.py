"""A date the model fallback reads is handed back as a whole ISO date and must be stored as that date: before this, parse_date had no branch for it and kept only the year, so 2026-03-15 was stored as 2026-06-30."""
import itertools
from datetime import date

import pytest

from sipclerk.intake import parsers
from tests.test_processors import Llm, make_ctx  # noqa: F401  (the fixtures of the processor tests)
from sipclerk.voice import processors as vp

TODAY = date(2026, 10, 27)

# CLASS-TABLE: a whole date the model reads, in every month of three years and at three places in the month, stored as that date and not as the year alone
YEARS = (2024, 2025, 2026)
MONTHS = range(1, 13)
DAYS = (1, 15, 28)


def test_class_a_whole_date_from_the_model_is_stored_as_that_date_in_every_month():
    bad = []
    for year, month, day in itertools.product(YEARS, MONTHS, DAYS):
        d = date(year, month, day)
        r = parsers.parse_date(d.isoformat(), TODAY)
        if d > TODAY:
            if r.ok or r.reason != "in_the_future":
                bad.append((d.isoformat(), r.ok, r.value or r.reason))
        elif not (r.ok and r.value == d.isoformat()):
            bad.append((d.isoformat(), r.ok, r.value or r.reason))
    assert not bad, bad


@pytest.mark.parametrize("text,reason", [("2026-02-30", "not_a_date"), ("2026-13-01", "not_a_date"), ("2031-01-01", "in_the_future")])
def test_a_whole_date_that_cannot_stand_is_refused_with_its_reason(text, reason):
    r = parsers.parse_date(text, TODAY)
    assert not r.ok and r.reason == reason


def test_a_whole_date_older_than_the_question_allows_is_too_old():
    r = parsers.parse_date("2020-01-01", TODAY, max_age_days=365)
    assert not r.ok and r.reason == "too_old"


def test_a_year_alone_still_reads_as_the_middle_of_that_year():
    r = parsers.parse_date("it was in 2024", TODAY)
    assert r.ok and r.value == "2024-06-30"


async def test_the_model_fallback_stores_the_date_it_read(ashgrove):
    class PastDate(Llm):
        async def extract_date(self, text, today):
            return "2026-03-15"

    ctx = make_ctx(ashgrove, PastDate())
    m = ctx.machine
    m.begin()
    for u in ["yes", "Jordan Reyes", "yes", "four one five five five five oh one two three", "yes", "no", "car accident"]:
        m.handle_text(u)
    assert m.question.id == "incident_date"
    p = await vp.Intake(ctx)._interpret("back when the kids started at their new school")
    assert p.ok and p.value == "2026-03-15"
