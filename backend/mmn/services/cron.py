"""Standard crontab expressions for APScheduler.

APScheduler's ``CronTrigger.from_crontab`` counts a numeric day of week from Monday = 0, so
``0 4 * * 0`` would run on Monday and ``* * * * 6`` on Sunday. Everywhere else (crontab, the
hint texts in the UI) 0 and 7 mean Sunday and 6 means Saturday. ``cron_trigger`` rewrites the
numeric weekday items of the fifth field to day names before handing the expression to
APScheduler, so the schedule runs on the day the user meant. Names (``mon-fri``, ``sat``) are
passed on unchanged.
"""

from __future__ import annotations

import re

from apscheduler.triggers.cron import CronTrigger

_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_ITEM = re.compile(r"^(?:(\*)|(\d+)(?:-(\d+))?)(?:/(\d+))?$")


def _expand(item: str) -> list[int] | None:
    """Standard-cron weekday numbers (0-6, Sunday = 0) for one numeric list item, None if not numeric."""
    m = _ITEM.match(item)
    if not m:
        return None
    star, lo, hi, step = m.groups()
    if star:
        first, last = 0, 6
    else:
        first = int(lo)
        last = int(hi) if hi is not None else (7 if step else first)
    stride = int(step) if step else 1
    if not (0 <= first <= 7 and 0 <= last <= 7) or stride < 1:
        raise ValueError(f"Invalid day of week {item!r}")
    if first > last:
        raise ValueError(f"Invalid day of week range {item!r}")
    return sorted({d % 7 for d in range(first, last + 1, stride)})


def standard_dow(field: str) -> str:
    """Rewrite a crontab day-of-week field to APScheduler day names (``1-5`` -> ``mon,tue,...``)."""
    if field in ("*", "?"):
        return "*"
    days: set[int] = set()
    rest: list[str] = []
    for item in field.lower().split(","):
        nums = _expand(item.strip())
        if nums is None:
            rest.append(item.strip())
        else:
            days.update(nums)
    if len(days) == 7 and not rest:
        return "*"
    return ",".join([_NAMES[d] for d in sorted(days)] + rest)


def standard_crontab(expr: str) -> str:
    """The expression with its weekday field in names; other fields are left alone."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Wrong number of fields; got {len(parts)}, expected 5")
    parts[4] = standard_dow(parts[4])
    return " ".join(parts)


def cron_trigger(expr: str, timezone: str = "UTC") -> CronTrigger:
    """``CronTrigger.from_crontab`` with standard crontab weekdays (0/7 = Sunday, 6 = Saturday)."""
    return CronTrigger.from_crontab(standard_crontab(expr), timezone=timezone)
