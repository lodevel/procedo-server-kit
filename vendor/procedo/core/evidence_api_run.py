"""A run as the JSON API carries it (``docs/plans/data_format.md`` §4.5–§4.8), both ways.

* **Out:** :func:`run_object` builds the Run a push sends from a run staged in this PC's outbox
  (its manifest, its files and its record): the board from the run's board key
  (:mod:`evidence_board`), each file a reference with its role, its record's phases and
  measurements. Built once per run: the push keeps the bytes it first built, so a replay sends
  exactly them and the server recognises it (§6.1).
* **In:** :func:`manifest_of` is the manifest a materialized run keeps beside its files (what
  :mod:`evidence_missing` checks them against), :func:`run_info` the row a list shows.

Qt-free, I/O-free.
"""

from __future__ import annotations

import math
import mimetypes
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .evidence_board import board_of, key_of
from .evidence_store import RESULT_FILE, RunInfo

#: A role a manifest may already name (a draft's documents: ``definition``).
ROLES = ("record", "log", "functions", "definition", "attachment")
_LOGS = frozenset({"console.log", "interactive_log.jsonl"})
_MEASUREMENT_OUTCOMES = frozenset({"PASS", "FAIL", "UNSET", "PARTIALLY_SET", "SKIPPED"})
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
#: The extensions' types this app ships with, never this PC's registry: the type is part of a
#: run's bytes, which two PCs must build alike.
_TYPES = mimetypes.MimeTypes(filenames=())
_EXACT = 2**53  # the largest integer a double holds exactly


def role(name: str, declared: Any = None) -> str:
    """What a file of a run is (§4.8): its record, a log, the functions it ran with, a draft's
    documents, or an attachment."""
    if declared in ROLES:
        return str(declared)
    if name == RESULT_FILE:
        return "record"
    if name in _LOGS:
        return "log"
    if name.startswith("functions/"):
        return "functions"
    if name.startswith("definition/"):
        return "definition"
    return "attachment"


def mimetype(name: str) -> str:
    """A file's type from its name's extension (§3.3); ``application/octet-stream`` for any
    other."""
    return _TYPES.guess_type(name, strict=True)[0] or "application/octet-stream"


def time_of(value: Any) -> str | None:
    """A time as the contract spells it (UTC, seconds, ``Z``); ``None`` for no time. A time
    written another way is read as ISO 8601, a naive one in this PC's zone."""
    if not isinstance(value, str) or not value:
        return None
    if _TIME.fullmatch(value):
        return value
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_object(
    project_id: str,
    board_key: str,
    manifest: dict[str, Any],
    record: dict[str, Any] | None,
    store_id: int,
) -> dict[str, Any]:
    """The Run of a push: what the manifest and the record say, every file referenced in the
    storage ``store_id``. A value not known is left out (§2: absent, never ``""``)."""
    board = board_of(board_key)
    files = [
        {
            "name": f["name"],
            "role": role(f["name"], f.get("role")),
            "size": f["size"],
            "sha256": f["sha256"],
            "mimetype": mimetype(f["name"]),
            "store_id": store_id,
        }
        for f in manifest.get("files") or []
    ]
    bundle = manifest.get("bundle") if isinstance(manifest.get("bundle"), dict) else {}
    record = record if isinstance(record, dict) else {}
    test_version = manifest.get("test_version")
    found: dict[str, Any] = {
        "format_version": 2,
        "project_id": project_id,
        "run_id": manifest["run_id"],
        "serial": board.serial,
        "part_number": board.part_number,
        "board_revision": board.revision,
        "test": manifest.get("test"),  # the name its test had when it ran: a record
        "test_id": manifest.get("test_id"),  # what it is filed under
        "firmware_version": manifest.get("firmware_version"),
        "extra": manifest.get("extra"),
        "started": time_of(manifest.get("started")),
        "ended": time_of(manifest.get("ended")),
        "verdict": manifest.get("verdict"),
        "complete": bool(manifest.get("complete")),
        "operator": manifest.get("operator"),
        "machine_id": manifest.get("machine_id"),
        "station_id": record.get("station_id"),
        "test_version": test_version if isinstance(test_version, int) and test_version else None,
        "draft": any(f["role"] == "definition" for f in files),
        "definition_sha256": manifest.get("definition_sha256"),
        "bundle_id": bundle.get("id"),
        "bundle_version": bundle.get("version"),
        "app_version": manifest.get("app_version"),
        "phases": phases(record) if manifest.get("complete") else [],
        "files": files,
    }
    return {k: v for k, v in found.items() if v not in (None, "", {})}


def phases(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Every phase of an OpenHTF record, in its order, each attempt numbered (§4.6), with its
    measurements numbered across the run (§4.7)."""
    out = []
    attempts: Counter[str] = Counter()
    count = 0
    found = record.get("phases")
    for ord_, phase in enumerate((p for p in found or [] if isinstance(p, dict)), start=1):
        name = str(phase.get("name") or "")
        attempts[name] += 1
        measured = []
        values = phase.get("measurements")
        for key, m in (values if isinstance(values, dict) else {}).items():
            if isinstance(m, dict):
                count += 1
                measured.append(_measurement(m, str(key), count))
        entry = {
            "ord": ord_,
            "name": name,
            "attempt": attempts[name],
            "outcome": phase.get("outcome") if isinstance(phase.get("outcome"), str) else None,
            "started": _millis(phase.get("start_time_millis")),
            "ended": _millis(phase.get("end_time_millis")),
            "measurements": measured,
        }
        out.append({k: v for k, v in entry.items() if v is not None})
    return out


def _measurement(m: dict[str, Any], key: str, ord_: int) -> dict[str, Any]:
    """One measurement (§4.7): its value as a number or a text, its validators and the limits
    the engine recorded (``procedo.lower`` / ``procedo.upper``). The structured criterion is
    left out until the engine writes it into each check (§4.7, last rule)."""
    extra = m.get("procedo") if isinstance(m.get("procedo"), dict) else {}
    units = m.get("units") if isinstance(m.get("units"), dict) else {}
    outcome = m.get("outcome")
    found: dict[str, Any] = {
        "name": str(m.get("name") or key),
        "ord": ord_,
        **_value(m.get("measured_value")),
        "units": units.get("suffix") or None,
        "outcome": outcome if outcome in _MEASUREMENT_OUTCOMES else None,
        "validators": [str(v) for v in m.get("validators") or []] or None,
        "limit_low": _number(extra.get("lower")),
        "limit_high": _number(extra.get("upper")),
    }
    return {k: v for k, v in found.items() if v is not None}


def _value(value: Any) -> dict[str, Any]:
    """``value_num`` for a number, ``value_text`` for a text, the exact digits of an integer
    a double cannot hold and a value JSON cannot (NaN, infinity); neither for anything else."""
    if isinstance(value, bool) or value is None:
        return {}
    if isinstance(value, int):
        if abs(value) <= _EXACT:
            return {"value_num": value}
        return {"value_num": float(value), "value_text": str(value)}
    if isinstance(value, float):
        if math.isfinite(value):
            return {"value_num": value}
        return {
            "value_text": "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        }
    if isinstance(value, str):
        return {"value_text": value}
    return {}


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _millis(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    at = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# In: a Run as the app keeps a run
# ---------------------------------------------------------------------------


def board_key(item: dict[str, Any]) -> str:
    """The board key of what an answer names (a run, an artifact, a board); a format-1 item
    belongs to the "no part number" board whatever it recorded (§4.5)."""
    if item.get("format_version") == 1:
        return key_of("", "", str(item.get("serial") or ""))
    return key_of(
        item.get("part_number"), item.get("board_revision"), str(item.get("serial") or "")
    )


def run_info(run: dict[str, Any]) -> RunInfo:
    """The row of a run (a Run or a RunSummary), as every store lists one."""
    return RunInfo(
        str(run.get("run_id") or ""),
        str(run.get("started") or ""),
        str(run.get("ended") or ""),
        str(run.get("verdict") or ""),
        str(run.get("machine_id") or ""),
        bool(run.get("complete")),
        str(run.get("test") or ""),
        str(run.get("test_id") or ""),
    )


def manifest_of(run: dict[str, Any]) -> dict[str, Any]:
    """The manifest a materialized run keeps beside its files, in the shape a run folder's
    ``manifest.json`` has: its row, its board and its files."""
    found = {
        "schema_version": 1,
        "run_id": run.get("run_id", ""),
        "test": run.get("test", ""),
        "test_id": run.get("test_id"),
        "started": run.get("started", ""),
        "ended": run.get("ended", ""),
        "verdict": run.get("verdict", ""),
        "machine_id": run.get("machine_id", ""),
        "complete": bool(run.get("complete")),
        "operator": run.get("operator", ""),
        "serial_number": run.get("serial", ""),
        "part_number": run.get("part_number"),
        "board_revision": run.get("board_revision"),
        "firmware_version": run.get("firmware_version"),
        "extra": run.get("extra"),
        "definition_sha256": run.get("definition_sha256"),
        "test_version": run.get("test_version"),
        "app_version": run.get("app_version"),
        "files": [
            {"name": f.get("name"), "size": f.get("size"), "sha256": f.get("sha256")}
            | ({"role": "definition"} if f.get("role") == "definition" else {})
            for f in run.get("files") or []
            if isinstance(f, dict)
        ],
    }
    return {k: v for k, v in found.items() if v is not None}
