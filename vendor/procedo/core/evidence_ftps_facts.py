"""What one FTPS connection can be asked about a path: its listing and its facts.

Carved out of :mod:`evidence_ftps` with the connection layer, so the tree keeps the
decisions the contract is about (absent vs refused, cached vs re-listed) and this
module keeps the wire format. Every function takes a leased connection and a path
string — it never leases or caches.

Failures leave as they arrive: ``ftplib.Error`` for a reply the server made (three
different answers hide behind ``550`` — unsupported, absent, refused — and only the
caller has the context to tell them apart), and a translated ``OSError`` for anything
the transport itself raised. Stdlib only. Qt-free.
"""

from __future__ import annotations

import calendar
import ftplib
import time
from pathlib import PurePosixPath
from typing import Any

from .evidence_ftps_conn import MISSING_CODES, clean_reply, reply_code, translate, unsupported

Facts = dict[str, tuple[str, int, float]]  # name -> (type, size, mtime)


def listing(conn: Any, path: str) -> Facts:
    """One ``MLSD`` of ``path`` as ``{name: (type, size, mtime)}``.

    Raises ``ftplib.Error`` for the caller to interpret; a transport failure is
    already translated.
    """
    try:
        found: Facts = {}
        for name, facts in conn.mlsd(path, facts=["type", "size", "modify"]):
            kind = facts.get("type", "")
            if name in (".", "..") or kind in ("cdir", "pdir"):
                continue
            found[name] = (kind, int(facts.get("size") or 0), epoch(facts.get("modify")))
        return found
    except ftplib.Error:
        raise
    except (OSError, EOFError) as exc:
        raise translate(exc, path) from exc


def probe(conn: Any, path: str) -> str:
    """The entry's kind, or confirmed absence. Ambiguous refusals require a listing."""
    try:
        reply = conn.sendcmd(f"MLST {path}")
    except ftplib.Error as exc:
        if unsupported(exc):
            try:
                return probe_by_cwd(conn, path)
            except (OSError, EOFError) as dead:
                raise translate(dead, path) from dead
        if reply_code(exc) in MISSING_CODES:
            return probe_by_listing(conn, path)
        raise translate(exc, path) from exc
    except (OSError, EOFError) as exc:
        raise translate(exc, path) from exc
    return "dir" if mlst_facts(reply).get("type") == "dir" else "file"


def probe_by_cwd(conn: Any, path: str) -> str:
    """Fallback for a server without ``MLST``: a folder can be entered, a file has a
    size. Refusals still need a parent listing to prove absence."""
    try:
        conn.cwd(path)
        return "dir"
    except ftplib.Error as exc:
        if reply_code(exc) not in MISSING_CODES:
            raise translate(exc, path) from exc
    try:
        conn.voidcmd("TYPE I")
        conn.size(path)
        return "file"
    except ftplib.Error as exc:
        if reply_code(exc) not in MISSING_CODES and not unsupported(exc):
            raise translate(exc, path) from exc
        return probe_by_listing(conn, path)


def probe_by_listing(conn: Any, path: str) -> str:
    """A successful parent listing can prove absence; another 550 cannot.

    Walk upwards when the parent itself is missing or inaccessible. If an ancestor
    listing shows that parent exists, its refused listing must remain an error.
    """
    wanted = PurePosixPath(path)
    child = wanted
    while child != child.parent:
        try:
            found = listing(conn, child.parent.as_posix())
        except ftplib.Error as exc:
            if unsupported(exc):
                message = f"cannot determine whether {path} exists: the server has no MLSD"
                raise clean_reply(PermissionError(message)) from exc
            if reply_code(exc) not in MISSING_CODES:
                raise translate(exc, path) from exc
            child = child.parent
            continue
        entry = found.get(child.name)
        if entry is None or (child != wanted and entry[0] != "dir"):
            return "absent"
        if child == wanted:
            return "dir" if entry[0] == "dir" else "file"
        break
    raise clean_reply(PermissionError(f"cannot determine whether {path} exists: listing refused"))


def epoch(stamp: str | None) -> float:
    """An ``MLSD`` ``modify`` fact (``YYYYMMDDHHMMSS``, UTC) as epoch seconds; one
    second of resolution is all the sweep and the clock-skew probe need.

    ``timegm`` and not ``mktime``: the fact is UTC, while ``mktime`` reads a time
    struct as LOCAL and applies whatever DST is in force, which put the clock-skew
    probe out by exactly one hour half the year.
    """
    if not stamp:
        return 0.0
    try:
        return float(calendar.timegm(time.strptime(stamp[:14], "%Y%m%d%H%M%S")))
    except (ValueError, OverflowError):
        return 0.0


def mlst_facts(reply: str) -> dict[str, str]:
    """The fact line of an ``MLST`` reply as ``{fact: value}`` (lower-cased keys)."""
    for line in reply.splitlines():
        if ";" not in line:
            continue
        facts = {}
        for item in line.strip().split(";"):
            if "=" in item:
                key, _sep, value = item.partition("=")
                facts[key.strip().lower()] = value.strip()
        if facts:
            return facts
    return {}
