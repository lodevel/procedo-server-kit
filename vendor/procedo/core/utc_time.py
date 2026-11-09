"""Times as Procedo records them (UTC) and as a person reads and picks them (this PC's local
time): the one converter between the two.

* **Recorded in UTC**, to the second, with ``Z`` (``2026-09-20T23:30:00Z``): a report's time
  (its record's ``metadata.generated``), as a run's and a release's are, so every storage holds
  the same instant and a search compares it the same way on each (:func:`recorded`).
* **Shown in local time** (``2026-09-21 01:30:00`` in Paris), wherever a person reads one: the
  report itself, the Report Browser, Search Results (:func:`shown`). A time written another way
  (without its zone) is shown as written.
* **A day a person picks is a day of their calendar**: Search Results' *From 21 Sep* starts at
  local midnight on the 21st, an instant in UTC (:func:`day_start`), so every storage is asked
  the same instants.

Qt-free, stdlib only.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone

#: How a time is recorded (the contract's spelling of a time, §2.7).
RECORDED = "%Y-%m-%dT%H:%M:%SZ"
#: How a person reads it: this PC's local time.
SHOWN = "%Y-%m-%d %H:%M:%S"
_RECORDED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def now() -> datetime:
    """This moment, in UTC (a test moves it)."""
    return datetime.now(timezone.utc)


def recorded(at: datetime) -> str:
    """*at* as Procedo records it: UTC, seconds, ``Z``; a naive *at* is this PC's local time."""
    return at.astimezone(timezone.utc).strftime(RECORDED)


def is_recorded(text: object) -> bool:
    """Whether *text* is a time as Procedo records it (UTC, seconds, ``Z``)."""
    return isinstance(text, str) and _RECORDED.fullmatch(text) is not None


def shown(text: str) -> str:
    """The recorded time *text* as a person reads it, in this PC's local time
    (``YYYY-MM-DD HH:MM:SS``); a time written another way, or no time, as written."""
    if not is_recorded(text):
        return text
    at = datetime.strptime(text, RECORDED).replace(tzinfo=timezone.utc)
    return at.astimezone().strftime(SHOWN)


def day_start(day: date) -> str:
    """When the day *day* of this PC's calendar starts (its local midnight), as recorded."""
    return recorded(datetime.combine(day, time.min))
