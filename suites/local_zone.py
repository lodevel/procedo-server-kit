"""This PC's local time zone set for a while: a time recorded in UTC then reads as a person in
that zone reads it, and a day they pick starts at their midnight.

POSIX only (``time.tzset``). Elsewhere :func:`zone` changes nothing and says so: a case that
must hold in any zone computes what it expects from the local time it runs in.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def zone(name: str) -> Iterator[bool]:
    """The local time zone is *name* inside (``Europe/Paris``); yields whether it could be set."""
    if not hasattr(time, "tzset"):
        yield False
        return
    held = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield True
    finally:
        if held is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = held
        time.tzset()
