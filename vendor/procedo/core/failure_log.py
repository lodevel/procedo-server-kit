"""A failure that comes back again and again, said in the log once per outage (:class:`Repeats`).

A server that went away fails every pass and every upload until it is back: logged each time,
the log fills with the same error and the footer counts each one. So, per subject (a project's
sync, the uploads to one server):

* the first failure of a kind and message during an outage is an ERROR — with its traceback
  only when it is unexpected (an expected kind, :data:`EXPECTED`, a refusal of
  :data:`EXPECTED_CODES`, or an operating-system error, says all in its message); every kind and
  message seen is kept until the subject works again, so two failures that alternate are two
  errors, not one per try;
* the same failure again is an INFO, ``still failing, N tries since 14:02``, at most once
  every :data:`EVERY_S`;
* the first success after failures is an INFO, ``works again after N failures``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .evidence_store import EvidenceError

#: The kinds a failure is expected to have: its message says it all, no traceback.
EXPECTED = frozenset({"unreachable", "denied", "timeout", "not_found", "cancelled"})
#: Refusals the app makes itself and words in full (a push while a pull is owed).
EXPECTED_CODES = frozenset({"pull_owed"})
#: How often a failure that goes on is said again (seconds).
EVERY_S = 600.0
_WHERE = re.compile(r"\s*\([^()]*\)$")  # "(listProjects)": the call it met, not the failure


@dataclass
class _Outage:
    signatures: set[tuple[str, str]]
    since: str
    tries: int
    said_at: float


def expected(error: BaseException) -> bool:
    """Whether *error* says all in its message (no traceback wanted)."""
    if isinstance(error, EvidenceError):
        return error.kind in EXPECTED or error.code in EXPECTED_CODES
    return isinstance(error, OSError)


class Repeats:
    """The failures of each subject, as they repeat."""

    def __init__(
        self,
        log: logging.Logger,
        *,
        every_s: float = EVERY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log, self.every_s, self.clock = log, every_s, clock
        self._lock = threading.Lock()
        self._outages: dict[str, _Outage] = {}

    def failed(self, subject: str, error: BaseException, said: str) -> None:
        """*subject* failed with *error*; *said* is the line of a first failure."""
        signature = (str(getattr(error, "kind", type(error).__name__)), _WHERE.sub("", str(error)))
        now = self.clock()
        with self._lock:
            outage = self._outages.get(subject)
            again = None
            if outage is None:
                first = True
                self._outages[subject] = _Outage({signature}, time.strftime("%H:%M"), 1, now)
            else:
                first = signature not in outage.signatures
                outage.signatures.add(signature)
                outage.tries += 1
                if not first and now - outage.said_at >= self.every_s:
                    outage.said_at, again = now, outage
        if first:
            self.log.error("%s", said, exc_info=None if expected(error) else error)
        elif again is not None:
            self.log.info(
                "%s still failing, %d tries since %s: %s",
                subject,
                again.tries,
                again.since,
                signature[1],
            )

    def worked(self, subject: str) -> None:
        """*subject* worked: said when it had failed."""
        with self._lock:
            outage = self._outages.pop(subject, None)
        if outage is not None:
            failures = "failure" if outage.tries == 1 else "failures"
            self.log.info("%s works again after %d %s", subject, outage.tries, failures)
