"""Standard crontab weekdays (0/7 = Sunday, 6 = Saturday) on top of APScheduler."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mmn.services.cron import cron_trigger, standard_dow
from mmn.services.scheduler import validate_cron

UTC = ZoneInfo("UTC")
# Wednesday 2026-11-25 12:00 UTC
START = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _days(expr: str, n: int = 7) -> list[str]:
    trig = cron_trigger(expr)
    out, prev, now = [], None, START
    for _ in range(n):
        fire = trig.get_next_fire_time(prev, now)
        out.append(fire.strftime("%a"))
        prev, now = fire, fire
    return out


@pytest.mark.parametrize(("field", "expected"), [
    ("*", "*"),
    ("0", "sun"),
    ("7", "sun"),
    ("6", "sat"),
    ("1-5", "mon,tue,wed,thu,fri"),
    ("0,6", "sun,sat"),
    ("6,0", "sun,sat"),
    ("5-7", "sun,fri,sat"),
    ("0-6", "*"),
    ("*/2", "sun,tue,thu,sat"),
    ("1-5/2", "mon,wed,fri"),
    ("sat", "sat"),
    ("mon-fri", "mon-fri"),
    ("1,sat", "mon,sat"),
])
def test_standard_dow(field, expected):
    assert standard_dow(field) == expected


def test_numeric_weekdays_fire_on_the_right_day():
    assert _days("0 4 * * 0", 2) == ["Sun", "Sun"]
    assert _days("0 4 * * 7", 1) == ["Sun"]
    assert _days("* * * * 6", 1) == ["Sat"]
    assert set(_days("0 3 * * 1-6", 6)) == {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}
    assert _days("30 23 * * sat", 1) == ["Sat"]
    assert _days("0 3 * * *", 7) == ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"]


@pytest.mark.parametrize("expr", ["0 3 * * 8", "0 3 * * 5-2", "0 3 * *", "0 3 * * */0"])
def test_invalid(expr):
    with pytest.raises(ValueError):
        validate_cron(expr)
