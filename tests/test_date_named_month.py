"""A month and a day the caller named win over a relative word, and `may` is a month only where a month goes (output round 3, F3).

`March of last year` was stored as 2025-10-27 (last year's today) and `It may have been in August` as May 1, both without a read-back.
"""
import itertools
from datetime import date

import pytest

from sipclerk.intake import parsers as p

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
            "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth", "twentieth", "twenty first", "twenty second", "twenty third",
            "twenty fourth", "twenty fifth", "twenty sixth", "twenty seventh", "twenty eighth"]
TODAYS = [date(2026, 10, 27), date(2026, 1, 15), date(2026, 3, 1), date(2027, 5, 5)]


def _suffix(d: int) -> str:
    return "th" if 10 <= d % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")


def _expect(today: date, year: int | None, month: int, day: int) -> tuple[bool, str]:
    """What the parser must say for a month (and maybe a year) named: the most recent such month when no year was said."""
    if year is None:
        year = today.year if date(today.year, month, 1) <= today else today.year - 1
    found = date(year, month, day)
    return (False, "in_the_future") if found > today else (True, found.isoformat())


def _check(text: str, today: date, want: tuple[bool, str]) -> None:
    r = p.parse_date(text, today)
    got = (True, r.value) if r.ok else (False, r.reason)
    assert got == want, (text, today, got, want)


# CLASS-TABLE: every month x every shape of "month, then a relative year" x four dates
@pytest.mark.parametrize("today,month", list(itertools.product(TODAYS, range(1, 13))))
def test_a_named_month_with_a_relative_year(today, month):
    name = MONTHS[month - 1]
    for text, back in [(f"{name} of last year", 1), (f"{name} last year", 1), (f"last year in {name}", 1), (f"It was {name} last year", 1),
                       (f"{name} two years ago", 2), (f"two years ago in {name}", 2), (f"{name} a year ago", 1), (f"about 2 years ago, in {name}", 2),
                       (f"{name} this year", 0), (f"{name} of this year", 0)]:
        _check(text, today, _expect(today, today.year - back, month, 1))


# CLASS-TABLE: every month x every day of the month (1 to 28) x the ways a day is said beside a month, digits and words
@pytest.mark.parametrize("today,month", list(itertools.product(TODAYS, range(1, 13))))
def test_a_named_month_and_day(today, month):
    name = MONTHS[month - 1]
    for day in range(1, 29):
        want = _expect(today, None, month, day)
        said = [f"{name} {day}", f"{name} {day}{_suffix(day)}", f"on {name} {day}{_suffix(day)}", f"{day} {name}", f"the {day}{_suffix(day)} of {name}",
                f"around 5 pm on {name} {day}{_suffix(day)}", f"the {ORDINALS[day - 1]} of {name}", f"{name} {ORDINALS[day - 1]}",
                f"On the {ORDINALS[day - 1]} of {name}, about a month ago"]
        for text in said:
            _check(text, today, want)
        back_year = today.year - 1
        for text in (f"{name} {day}{_suffix(day)} last year", f"the {ORDINALS[day - 1]} of {name} last year"):
            _check(text, today, _expect(today, back_year, month, day))


# CLASS-TABLE: every day (1 to 28) x last month and this month, digits and words
@pytest.mark.parametrize("today", TODAYS)
def test_a_day_of_last_month_or_this_month(today):
    last = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
    for day in range(1, 29):
        for text in (f"the {day}{_suffix(day)} of last month", f"{day} of last month", f"the {ORDINALS[day - 1]} of last month"):
            _check(text, today, (True, date(last.year, last.month, day).isoformat()))
        for text in (f"the {day}{_suffix(day)} of this month", f"the {ORDINALS[day - 1]} of this month"):
            this = date(today.year, today.month, day)
            _check(text, today, (False, "in_the_future") if this > today else (True, this.isoformat()))


# CLASS-TABLE: every month x every sentence where "may" is the verb: the month is the other one, and a sentence with no other month is not a date
@pytest.mark.parametrize("today,month", [(t, m) for t in TODAYS for m in range(1, 13) if m != 5])
def test_may_the_verb_is_not_the_month(today, month):
    name = MONTHS[month - 1]
    for text in (f"It may have been in {name}", f"it may have been {name}", f"I may be wrong but I think {name}", f"may have been {name} sometime",
                 f"It may have happened in {name}", f"That may not be right, {name}"):
        _check(text, today, _expect(today, None, month, 1))
    for text in ("It may have been a while", "I may not remember", "that may be wrong", "it may have been recent"):
        assert p.parse_date(text, today).reason == "not_a_date", text


# CLASS-TABLE: every date x every place the month May is said
@pytest.mark.parametrize("today", TODAYS)
def test_may_the_month_is_read(today):
    for text, day, back in [("May", 1, None), ("in May", 1, None), ("It was May", 1, None), ("sometime in May", 1, None), ("early May", 1, None),
                            ("since May", 1, None), ("last May", 1, None), ("May 5th", 5, None), ("May fifth", 5, None), ("May of last year", 1, 1),
                            ("the 3rd of May", 3, None), ("the third of May", 3, None), ("May 2025", 1, "2025"), ("around may", 1, None),
                            ("May of 2024", 1, "2024")]:
        year = None if back is None else (int(back) if isinstance(back, str) else today.year - back)
        _check(text, today, _expect(today, year, 5, day))


# What the parser did before and must still do: the relative answers with no month in them.
@pytest.mark.parametrize("text,iso", [
    ("about two weeks ago", "2026-10-13"), ("yesterday", "2026-10-26"), ("three months ago", "2026-07-27"), ("last year", "2025-10-27"),
    ("last Tuesday", "2026-10-20"), ("a couple of days ago", "2026-10-25"), ("last month", "2026-09-27"), ("last week", "2026-10-20"),
    ("two years ago", "2024-10-27"), ("1 year ago", "2025-10-27"), ("2024", "2024-06-30"), ("March 3rd 2026", "2026-03-03"),
    ("it was on 14 February 2026", "2026-02-14"), ("in March 2025", "2025-03-01"), ("in December", "2025-12-01"), ("last Friday", "2026-10-23"),
])
def test_relative_answers_without_a_month_are_unchanged(text, iso):
    r = p.parse_date(text, date(2026, 10, 27))
    assert r.ok and r.value == iso, (text, r)
