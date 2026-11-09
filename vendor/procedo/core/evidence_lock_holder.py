"""Who holds a store-side lock (:func:`evidence_tree.tree_lock`): the owner record this PC
writes when it claims one, that record read back for a person, and the typed timeout that
names it.

The record (``owner.json`` in the lock folder): ``owner`` (``<machine id>.<pid>``) and
``nonce`` are what the protocol acts on (a dead process of this PC is reclaimed, a holder
releases only its own claim); ``machine_id``, ``pid``, ``station``, ``host``, ``user`` and
``acquired_at`` (UTC, ISO 8601) are for the person who must decide whether that holder is
still there — a PC that was switched off, crashed or was reinstalled never comes back to
release it (:mod:`evidence_lock_break`). A record written before these fields existed
(``owner``, ``acquired_at`` in epoch seconds, maybe a ``nonce``) still reads.

Qt-free.
"""

from __future__ import annotations

import getpass
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePath

from . import app_settings
from .evidence_store import EvidenceError


def _quiet(read) -> str:
    """A name this PC may not be able to say (no profile, no login name): ``""`` then."""
    try:
        return str(read() or "").strip()
    except (OSError, KeyError, ValueError, ImportError):
        return ""


def operator_name() -> str:
    """The person at this PC: the operator chosen in the app (what a run records as its
    operator), else the login name."""
    current = _quiet(lambda: app_settings.load_app_settings().get("current"))
    return current or _quiet(getpass.getuser)


def this_pc() -> dict:
    """``station`` (the station name set in Settings, may be empty), ``host`` and ``user``."""
    return {
        "station": _quiet(app_settings.get_station_name),
        "host": _quiet(socket.gethostname),
        "user": operator_name(),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def holder_record(tag: str) -> dict:
    """The owner record of a claim made now by writer ``tag`` (``<machine id>.<pid>``)."""
    machine, _, pid = tag.rpartition(".")
    record: dict = {"owner": tag, "machine_id": machine if pid.isdigit() else tag}
    if pid.isdigit():
        record["pid"] = int(pid)
    return {
        **record,
        **this_pc(),
        "acquired_at": utc_now(),
        "nonce": secrets.token_hex(8),
    }


def _when(value: object) -> datetime | None:
    """``acquired_at`` as written: ISO 8601 UTC now, epoch seconds before."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), timezone.utc)
        if isinstance(value, str) and value:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def local_time(at: datetime, now: datetime | None = None) -> str:
    """``3 Nov 14:20`` in this PC's time (the year only when it is not this one)."""
    local = at.astimezone()
    now = now or datetime.now().astimezone()
    day = f"{local.day} {local:%b}" + (f" {local.year}" if local.year != now.year else "")
    return f"{day} {local:%H:%M}"


@dataclass(frozen=True)
class LockHolder:
    """A lock's owner record, as a person reads it."""

    record: dict

    @property
    def name(self) -> str:
        """The PC: its station name, else its computer name, else its machine id."""
        r = self.record
        machine = str(r.get("machine_id") or str(r.get("owner", "")).rpartition(".")[0])
        return str(r.get("station") or r.get("host") or machine or "Another PC")

    @property
    def user(self) -> str:
        return str(self.record.get("user") or "")

    @property
    def since(self) -> datetime | None:
        return _when(self.record.get("acquired_at"))

    @property
    def since_text(self) -> str:
        """``3 Nov 14:20`` local, ``""`` when the record does not say."""
        return local_time(self.since) if self.since else ""

    @property
    def who(self) -> str:
        """``BENCH-02 (bob)``."""
        return f"{self.name} ({self.user})" if self.user else self.name

    def summary(self) -> str:
        """``Locked by BENCH-02 (bob) since 3 Nov 14:20``."""
        since = f" since {self.since_text}" if self.since_text else ""
        return f"Locked by {self.who}{since}"


class EvidenceLocked(EvidenceError):
    """A lock wait that ran out on a lock its owner record says somebody holds.

    Kind ``timeout``, so everything that handles a timeout still does (the outbox keeps
    the entry and retries); the holder and the lock's path are carried for the person who
    may break it, and ``str()`` stays the technical sentence the log needs."""

    def __init__(self, message: str, lock: PurePath, record: dict) -> None:
        super().__init__("timeout", message)
        self.lock = lock
        self.record = dict(record)

    @property
    def holder(self) -> LockHolder:
        return LockHolder(self.record)

    @property
    def summary(self) -> str:
        """What a list's "Last error" says: ``Locked by BENCH-02 (bob) since 3 Nov 14:20``."""
        return self.holder.summary()
