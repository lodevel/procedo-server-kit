"""When a push to an evidence folder is tried again — the schedule of the outbox retry.

A destination (the store identity an outbox entry is addressed to) that fails a push
waits longer before the next attempt: 1, 2, 4, 8, 15, 30 min, then 60 min for good. A
push that lands, a new entry, the project being opened or :func:`retry_now` start it
over. Kept in memory per process, keyed by :func:`paths.place_key` (a store spelled two
ways is one destination); one destination's failures never delay another's.
A destination that is merely busy (another window holds the push turn) never fails
here: only a push that raised counts. What failed since the last push that landed (how many
attempts, the last error) is kept beside the schedule, for the Waiting uploads window, and so is
what an entry waits for instead of a retry (:meth:`hold`: its project folder does not answer,
its evidence folder is not confirmed as the project's) — no push is attempted for it, so no
schedule applies; :meth:`release` when it is sent, parked, deleted or routed elsewhere."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import TypeVar

from . import failure_log
from .paths import place_key

log = logging.getLogger(__name__)
_repeats = failure_log.Repeats(log)  # an outage said once, not at each try

STEPS_MIN = (1, 2, 4, 8, 15, 30, 60)

T = TypeVar("T")


class PushBackoff:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float]] = {}  # root -> (failed attempts, next attempt at)
        self._failures: dict[
            str, tuple[int, str]
        ] = {}  # root -> (failed attempts, last error) since one landed
        self._held: dict[str, str] = {}  # entry id -> what it waits for instead of a retry

    def due(self, root: str) -> bool:
        root = place_key(root)
        with self._lock:
            return self._clock() >= self._state.get(root, (0, 0.0))[1]

    def next_in_s(self, root: str) -> float:
        """Seconds until ``root`` is tried again (0: now)."""
        root = place_key(root)
        with self._lock:
            return max(0.0, self._state.get(root, (0, 0.0))[1] - self._clock())

    def failures(self, root: str) -> tuple[int, str]:
        """``(attempts, last error)`` that failed since a push to ``root`` last landed;
        :meth:`reset` (retry now, the project opened) starts the schedule over, not these."""
        root = place_key(root)
        with self._lock:
            return self._failures.get(root, (0, ""))

    def hold(self, entry_id: str, waits_for: str) -> None:
        """The entry waits for ``waits_for`` (``when ...``), not for a retry."""
        with self._lock:
            self._held[entry_id] = waits_for

    def release(self, entry_id: str) -> None:
        with self._lock:
            self._held.pop(entry_id, None)

    def held(self, entry_id: str) -> str:
        """What the entry waits for instead of a retry (``""``: it is on the schedule)."""
        with self._lock:
            return self._held.get(entry_id, "")

    def failed(self, root: str, error: str = "") -> None:
        root = place_key(root)
        with self._lock:
            n = self._state.get(root, (0, 0.0))[0]
            self._state[root] = (
                n + 1,
                self._clock() + 60.0 * STEPS_MIN[min(n, len(STEPS_MIN) - 1)],
            )
            self._failures[root] = (self._failures.get(root, (0, ""))[0] + 1, error)

    def reset(self, root: str | None = None) -> None:
        """Start ``root`` — every destination when ``None`` — over at the first step."""
        with self._lock:
            if root is None:
                self._state.clear()
            else:
                self._state.pop(place_key(root), None)

    def attempt(self, root: str, push: Callable[[], T]) -> T:
        """Run ``push`` (returns the ids it pushed): a raise is a failed attempt (a cancelled one is not), a push
        that sent something starts the schedule over, an empty one (nothing waited, or
        the turn was busy) says nothing."""
        try:
            pushed = push()
        except Exception as exc:
            if (
                getattr(exc, "kind", "") != "cancelled"
            ):  # the caller stopped it: the store did not fail
                # a lock wait names its holder in one line (``EvidenceLocked.summary``)
                why = getattr(exc, "summary", "") or str(exc)
                self.failed(root, why)
                next_try = _in_words(self.next_in_s(root))
                said = f"Uploads to {root} not sent: {why}; next try {next_try}"
                _repeats.failed(f"Uploads to {root}", exc, said)
            raise
        if pushed:
            _repeats.worked(f"Uploads to {root}")
            log.info("%d upload%s sent to %s", len(pushed), "" if len(pushed) == 1 else "s", root)
            self.reset(root)
            with self._lock:
                self._failures.pop(place_key(root), None)
        return pushed


BACKOFF = PushBackoff()


def retry_now(root: str | None = None) -> None:
    """The outbox window's "retry now": the next flush tries ``root`` (all: ``None``)."""
    log.info("Retry now: uploads to %s", root or "every destination")
    BACKOFF.reset(root)


def next_attempt_in(root: str) -> str:
    """``in 4 min`` (whole minutes, rounded up, at least 1), ``in 1 h`` from an hour on."""
    return _in_words(BACKOFF.next_in_s(root))


def _in_words(seconds: float) -> str:
    minutes = max(1, -(-int(seconds) // 60))
    return "in 1 h" if minutes >= 60 else f"in {minutes} min"


def attempt_when(entry_id: str, root: str) -> str:
    """What one entry waits for: ``when its project folder answers`` (:meth:`PushBackoff.hold`),
    else ``in 4 min`` (:func:`next_attempt_in`). The Waiting uploads window and the footer
    say it alike."""
    return BACKOFF.held(entry_id) or next_attempt_in(root)


def next_attempt_text(root: str, entry_ids: Iterable[str] = ()) -> str:
    """``next attempt in 4 min`` — or what the entries ``entry_ids`` of ``root`` wait for
    (:func:`attempt_when`; several different ones: all, comma-separated)."""
    whens = sorted({attempt_when(i, root) for i in entry_ids} or {next_attempt_in(root)})
    return f"next attempt {', '.join(whens)}"
