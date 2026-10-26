"""The clock of a call is the firm's own time zone, daylight saving time included (output round 3, F1).

The office hours used a fixed UTC offset typed into the tenant file, so from the day the clocks went back every weekday the last open hour read as closed
and the hour before opening read as open. The oracle here is written out by hand from the United States rule (daylight time starts at 02:00 standard time
on the second Sunday of March and ends at 02:00 daylight time on the first Sunday of November), so it does not share anything with the code under test.
"""
from datetime import date, datetime, time, timedelta

import pytest

from sipclerk import tenants as t
from sipclerk.intake.machine import IntakeMachine, Say

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
STANDARD_OFFSET = {"ashgrovepell": -8, "tidewaterquint": -5}  # hours from UTC in winter: Los Angeles, Miami


def _sunday(year: int, month: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def _offset(utc: datetime, standard: int) -> int:
    """Hours from UTC at the firm at the instant `utc`, by the US rule."""
    begins = datetime.combine(_sunday(utc.year, 3, 2), time()) + timedelta(hours=2 - standard)
    ends = datetime.combine(_sunday(utc.year, 11, 1), time()) + timedelta(hours=1 - standard)
    return standard + 1 if begins <= utc < ends else standard


def _utc_of(local: datetime, standard: int) -> datetime:
    """The UTC instant of a wall clock time that is not inside the skipped or repeated hour."""
    for off in (standard, standard + 1):
        utc = local - timedelta(hours=off)
        if _offset(utc, standard) == off:
            return utc
    raise AssertionError(f"{local} does not exist")


def _expected_open(weekly: dict, local: datetime) -> bool:
    window = weekly.get(DAYS[local.weekday()])
    return bool(window) and time.fromisoformat(window[0]) <= local.time() < time.fromisoformat(window[1])


def _firms(registry):
    return [(f.config, STANDARD_OFFSET[f.config.tenant_id]) for f in registry.all]


def test_the_oracle_knows_the_four_clock_changes_of_2026_and_2027():
    la = -8
    assert _offset(datetime(2026, 3, 8, 9, 59), la) == -8 and _offset(datetime(2026, 3, 8, 10, 0), la) == -7
    assert _offset(datetime(2026, 11, 1, 8, 59), la) == -7 and _offset(datetime(2026, 11, 1, 9, 0), la) == -8
    assert _offset(datetime(2027, 3, 14, 9, 59), la) == -8 and _offset(datetime(2027, 3, 14, 10, 0), la) == -7
    assert _offset(datetime(2027, 11, 7, 8, 59), la) == -7 and _offset(datetime(2027, 11, 7, 9, 0), la) == -8


def test_the_failing_cases_of_the_round(registry):
    ash, tide = registry.by_id("ashgrovepell").config, registry.by_id("tidewaterquint").config
    assert ash.is_open(datetime(2026, 11, 3, 0, 30))            # Monday 16:30 PST, open until 17:00
    assert not ash.is_open(datetime(2026, 11, 2, 16, 30))       # Monday 08:30 PST, not yet open
    assert tide.is_open(datetime(2026, 11, 2, 22, 30))          # Monday 17:30 EST, open until 18:00
    assert ash.is_open(datetime(2026, 10, 27, 17, 30))          # summer time still works


def test_a_call_in_the_hour_the_fixed_offset_got_wrong_is_taken_in_hours(registry):
    ash = registry.by_id("ashgrovepell")
    m = IntakeMachine(ash, "c1", "+14155550142", datetime(2026, 11, 3, 0, 30))
    assert m.in_hours
    assert not any(isinstance(a, Say) and a.text == ash.config.after_hours_message for a in m.begin())


# CLASS-TABLE: every firm x every day of 2026 and 2027 x the four edges of its window (and two moments on a closed day), the clock read from the firm's zone
def test_every_edge_of_every_day_of_two_years(registry):
    checked = 0
    for cfg, std in _firms(registry):
        day = date(2026, 1, 1)
        while day < date(2028, 1, 1):
            window = cfg.hours.weekly.get(DAYS[day.weekday()])
            if window:
                a, b = (time.fromisoformat(w) for w in window)
                probes = [datetime.combine(day, a) - timedelta(minutes=1), datetime.combine(day, a),
                          datetime.combine(day, b) - timedelta(minutes=1), datetime.combine(day, b)]
            else:
                probes = [datetime.combine(day, time(0, 30)), datetime.combine(day, time(12, 0))]
            for local in probes:
                utc = _utc_of(local, std)
                want = _expected_open(cfg.hours.weekly, local)
                assert cfg.is_open(utc) is want, (cfg.tenant_id, local, utc)
                checked += 1
            day += timedelta(days=1)
    assert checked > 4000


# CLASS-TABLE: every firm x every half hour of 2026 and 2027 in UTC, so every week and both clock changes of each year are inside
def test_every_half_hour_of_two_years(registry):
    for cfg, std in _firms(registry):
        utc = datetime(2026, 1, 1)
        while utc < datetime(2028, 1, 1):
            local = utc + timedelta(hours=_offset(utc, std))
            assert cfg.is_open(utc) is _expected_open(cfg.hours.weekly, local), (cfg.tenant_id, utc)
            utc += timedelta(minutes=30)


# CLASS-TABLE: every firm x every minute of the day each clock change happens (and the day before and after), in both years
@pytest.mark.parametrize("change", [date(2026, 3, 8), date(2026, 11, 1), date(2027, 3, 14), date(2027, 11, 7)])
def test_every_minute_around_each_clock_change(registry, change):
    for cfg, std in _firms(registry):
        utc = datetime.combine(change - timedelta(days=1), time())
        while utc < datetime.combine(change + timedelta(days=2), time()):
            local = utc + timedelta(hours=_offset(utc, std))
            assert cfg.is_open(utc) is _expected_open(cfg.hours.weekly, local), (cfg.tenant_id, utc)
            utc += timedelta(minutes=1)


# CLASS-TABLE: the date the intake files an incident against is the firm's local date, every firm x the hours around local midnight in summer and winter
@pytest.mark.parametrize("utc", [datetime(2026, 7, 15, h, 30) for h in range(0, 24)] + [datetime(2026, 12, 15, h, 30) for h in range(0, 24)])
def test_the_date_of_a_call_is_the_date_at_the_firm(registry, utc):
    for firm in registry.all:
        local = utc + timedelta(hours=_offset(utc, STANDARD_OFFSET[firm.config.tenant_id]))
        m = IntakeMachine(firm, "c1", "+14155550142", utc)
        assert m.today == local.date() and m.in_hours is _expected_open(firm.config.hours.weekly, local)


def _hours(zone, weekly=None):
    return t.Hours(timezone=zone, weekly=weekly or {d: ("09:00", "17:00") for d in DAYS})


# CLASS-TABLE: zones with no clock change, a half hour offset and a clock change of their own, the 09:00 opening as UTC read by hand
@pytest.mark.parametrize("zone,winter_open_utc,summer_open_utc", [
    ("America/Phoenix", time(16, 0), time(16, 0)),
    ("Asia/Kolkata", time(3, 30), time(3, 30)),
    ("America/Los_Angeles", time(17, 0), time(16, 0)),
    ("America/New_York", time(14, 0), time(13, 0)),
    ("Europe/London", time(9, 0), time(8, 0)),
])
def test_other_zones_open_at_their_own_nine_oclock(zone, winter_open_utc, summer_open_utc):
    h = t.TenantConfig.model_construct(hours=_hours(zone))
    for day, open_utc in ((date(2026, 12, 9), winter_open_utc), (date(2026, 7, 8), summer_open_utc)):
        at = datetime.combine(day, open_utc)
        assert h.is_open(at) and not h.is_open(at - timedelta(minutes=1)), (zone, day)


def test_an_offset_typed_into_a_tenant_file_is_refused_and_so_is_an_unknown_zone():
    weekly = {"mon": ("09:00", "17:00")}
    with pytest.raises(Exception):
        t.Hours(timezone="America/Los_Angeles", utc_offset_hours=-7, weekly=weekly)
    with pytest.raises(Exception):
        t.Hours(timezone="Mars/Olympus", weekly=weekly)
    with pytest.raises(Exception):
        t.Hours(weekly=weekly)
