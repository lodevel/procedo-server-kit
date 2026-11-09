"""A run's record (``results.json``) as Bench reads it — the ONE reader of a result (Qt-free).

The record is OpenHTF's test record; Procedo only adds keys (``metadata.procedo`` for the run,
a ``procedo`` key on a phase or a measurement, ``file``/``label``/``reading`` on an attachment,
``step``/``then`` on an ``outcome_details`` entry). A text test's record, a code test's and
OpenHTF's own JSON are read by the same rules, a missing Procedo key reads as "not provided":

* a check (a row of the Expected table) is a measurement with validators; the table lists a text
  test's checks in its ``## Expected`` order (``procedo.order``), then any check without one (a
  code test's) in the record's order;
* a reading is a measurement without validators, or a check that judges its own value (no
  ``procedo.refs``: a code test's measurement is both); a text test's check carries its
  ``procedo.refs`` (the readings it judges, maybe none) and is named by its Expected line;
* Bench's verdict is ``metadata.procedo.verdict`` (PARTIAL and SKIP exist only there), else
  OpenHTF's ``outcome``, TIMEOUT shown as ERROR.

A result of Procedo 5.0 or older is no record: :func:`require` refuses it with a message.
"""

from __future__ import annotations

import base64
import hashlib
import html
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: What a result of Procedo 5.0 or older reads as: this build does not read that format.
OLD_FORMAT = (
    "This run was saved by Procedo 5.0 or older; open the project with Procedo 5.0 to see it."
)
#: The phase Procedo closes a record with: the checks whose step never ran.
NOT_REACHED = "Not reached"
_OPENHTF_TRIGGER = "trigger_phase"  # OpenHTF's own first phase: no step of the test

_VERDICT_OF_OUTCOME = {"TIMEOUT": "ERROR"}
_CHECK_VERDICT = {"PASS": "PASS", "FAIL": "FAIL"}  # UNSET, SKIPPED, PARTIALLY_SET: not judged

# A base unit's value is shown with the SI prefix that fits it (1.89e-06 s -> 1.89 µs); a
# prefixed or other unit (mV, %, degC) is shown as recorded. OpenHTF writes the ohm U+2126.
_SI_BASES = frozenset(
    {"V", "A", "s", "Hz", "\u2126", "\u03a9", "Ohm", "ohm", "W", "F", "H", "T", "J", "C"}
)
_SUFFIX_SHOWN = {"pct": "%"}  # OpenHTF's suffix -> what a person reads
_PREFIXES = (
    (1e-12, "p"),
    (1e-9, "n"),
    (1e-6, "µ"),
    (1e-3, "m"),
    (1.0, ""),
    (1e3, "k"),
    (1e6, "M"),
    (1e9, "G"),
)


class NotARecord(ValueError):
    """A result file this build cannot read as a record; its message says why."""


def require(data: Any) -> dict:
    """``data`` when it is a run record; :class:`NotARecord` otherwise (an older Procedo's
    result says so: :data:`OLD_FORMAT`)."""
    if isinstance(data, dict) and isinstance(data.get("phases"), list) and "outcome" in data:
        return data
    if isinstance(data, dict) and "overall" in data:
        raise NotARecord(OLD_FORMAT)
    raise NotARecord("This result is no run record.")


def no_result_record(test: str, description: str) -> dict:
    """The record Bench writes for a code test whose run ended without one (its child died before
    the adapter wrote it): an ERROR saying *description*, in OpenHTF's shape — the engine's
    ``run_record.failed_record`` across the border (the app never imports the engine)."""
    now = int(time.time() * 1000)
    return {
        "dut_id": "",
        "station_id": "",
        "start_time_millis": now,
        "end_time_millis": now,
        "outcome": "ERROR",
        "outcome_details": [{"code": "NO_RESULT", "description": description, "then": "stopped"}],
        "marginal": None,
        "metadata": {
            "test_name": test,
            "test_description": "",
            "procedo": {"verdict": "ERROR", "source": "code", "attachments": "separate"},
        },
        "phases": [],
        "subtests": [],
        "branches": [],
        "diagnosers": [],
        "diagnoses": [],
        "log_records": [],
    }


def procedo(record: dict) -> dict:
    """The run's Procedo additions (``metadata.procedo``); ``{}`` for OpenHTF's own JSON."""
    return _dict(_dict(record.get("metadata")).get("procedo"))


def with_procedo(record: dict, fields: dict) -> dict:
    """``record`` with ``fields`` added to its ``metadata.procedo`` (a new dict; the run's
    provenance, functions and packages, added after the run)."""
    metadata = dict(_dict(record.get("metadata")))
    metadata["procedo"] = {**procedo(record), **fields}
    return {**record, "metadata": metadata}


def verdict(record: dict) -> str:
    """Bench's verdict of the run (see the module rules); ``""`` when the record says none."""
    found = str(procedo(record).get("verdict") or record.get("outcome") or "")
    return _VERDICT_OF_OUTCOME.get(found, found)


def verdict_text(record: dict) -> str:
    """The verdict as a report shows it: ``PASS (marginal)`` for OpenHTF's marginal pass."""
    found = verdict(record)
    return f"{found} (marginal)" if found == "PASS" and record.get("marginal") is True else found


def ended(record: dict) -> datetime | None:
    """When the run ended, local time; ``None`` when not recorded (or no time)."""
    millis = record.get("end_time_millis")
    if isinstance(millis, bool) or not isinstance(millis, int | float):
        return None
    try:
        return datetime.fromtimestamp(millis / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def problems(record: dict) -> list[dict]:
    """What went wrong (``outcome_details``: OpenHTF's code and description, with Procedo's
    step and what the run did then)."""
    return [d for d in _list(record.get("outcome_details")) if isinstance(d, dict)]


def _dict(found: Any) -> dict:
    """``found`` when it is a dict, else ``{}``: a malformed record reads as one without that
    part, never a raise (a badge read must not fail on it)."""
    return found if isinstance(found, dict) else {}


def _list(found: Any) -> list:
    """``found`` when it is a list, else ``[]`` (see :func:`_dict`)."""
    return found if isinstance(found, list) else []


def _phases(record: dict) -> list[dict]:
    return [p for p in _list(record.get("phases")) if isinstance(p, dict)]


@dataclass(frozen=True)
class Measurement:
    """One measurement of the record, a reading or a check (see the module rules)."""

    name: str
    outcome: str  # OpenHTF's: PASS, FAIL, UNSET, PARTIALLY_SET, SKIPPED
    measured: bool  # whether the record holds a value
    value: Any
    units: str  # the unit's suffix, "" for none
    validators: tuple[str, ...]
    refs: tuple[str, ...] | None  # a text check: the readings it judges; None: not given
    reason: str  # why it was not computed, "" when not said
    order: int | None  # a text check's position in ``## Expected``; None: not given
    key: str  # the criterion's stable key (``procedo.key``), else the name: its notes' key
    dimensions: tuple[str, ...]  # a dimensioned measurement's axis names
    phase: str
    step: Any  # Procedo's step number, None when not provided

    @property
    def is_check(self) -> bool:
        return bool(self.validators)

    @property
    def is_reading(self) -> bool:
        return not self.validators or self.refs is None

    @property
    def verdict(self) -> str:
        """The check's verdict in Bench's words: PASS, FAIL, else SKIP (not judged)."""
        return _CHECK_VERDICT.get(self.outcome, "SKIP")

    @property
    def expression(self) -> str:
        """The Expected row's expression: a text check is named by it, a reading judged by its
        own validators shows them (``rise_time: x <= 5e-06``)."""
        if self.refs is not None:
            return self.name
        return f"{self.name}: {'; '.join(self.validators)}"

    def value_html(self) -> str:
        """The value as a report cell (escaped); ``N/A`` when not measured."""
        if not self.measured:
            return "N/A"
        if self.dimensions and isinstance(self.value, list):
            return _table_html(self.dimensions, self.value, self.units)
        return html.escape(value_text(self.value, self.units))


def measurements(record: dict) -> list[Measurement]:
    """Every measurement of the record, in the order it was recorded."""
    out = []
    for phase in _phases(record):
        step = _dict(phase.get("procedo")).get("step")
        for name, m in _dict(phase.get("measurements")).items():
            if not isinstance(m, dict):
                continue
            extra = _dict(m.get("procedo"))
            order, suffix = extra.get("order"), str(_dict(m.get("units")).get("suffix") or "")
            out.append(
                Measurement(
                    name=str(m.get("name", name)),
                    outcome=str(m.get("outcome") or ""),
                    measured=m.get("measured_value") is not None,
                    value=m.get("measured_value"),
                    units=_SUFFIX_SHOWN.get(suffix, suffix),
                    validators=tuple(str(v) for v in _list(m.get("validators"))),
                    refs=tuple(str(r) for r in extra["refs"])
                    if isinstance(extra.get("refs"), list)
                    else None,
                    reason=str(extra.get("reason") or ""),
                    order=order if isinstance(order, int) and not isinstance(order, bool) else None,
                    key=str(extra.get("key") or m.get("name", name)),
                    dimensions=tuple(
                        _dimension_name(d, i) for i, d in enumerate(_list(m.get("dimensions")))
                    ),
                    phase=str(phase.get("name") or ""),
                    step=extra.get("step", step),
                )
            )
    return out


def checks(record: dict) -> list[Measurement]:
    """The Expected table's rows: by ``procedo.order``, then the checks without one in the
    record's order (a stable sort)."""
    found = [m for m in measurements(record) if m.is_check]
    return sorted(found, key=lambda m: (m.order is None, m.order or 0))


def readings(record: dict) -> list[Measurement]:
    """Every reading, in the order recorded: a name measured in two phases (a loop's) is two."""
    return [m for m in measurements(record) if m.is_reading]


def by_name(found: list[Measurement]) -> dict[str, Measurement]:
    """``found`` by name, the last one recorded under a name winning: the reading a check's
    ``procedo.refs`` and an attachment's ``reading`` name."""
    return {m.name: m for m in found}


def check_measured_html(check: Measurement, found: dict[str, Measurement]) -> str:
    """The Expected row's Measured cell (escaped): each reading the check judges
    (``{Vout} = 4.96 V``, or why it was not computed: the reading's reason, else the check's),
    else the check's own value; a check not computed and with none of its readings says why."""
    if check.refs is None:
        return (
            html.escape(check.reason) if check.reason and not check.measured else check.value_html()
        )
    if check.reason and not check.measured and not any(r in found for r in check.refs):
        return html.escape(check.reason)
    parts = []
    for ref in check.refs:
        reading = found.get(ref)
        why = (reading.reason or check.reason) if reading and not reading.measured else ""
        if why:
            parts.append(html.escape(why))
        else:
            parts.append(f"{{{html.escape(ref)}}} = {reading.value_html() if reading else 'N/A'}")
    return "<br>".join(parts) or "N/A"


@dataclass(frozen=True)
class Attachment:
    """A file the run kept: beside the record (``file``) or inside it (``data``, base64)."""

    name: str
    label: str
    file: str  # the file in the run folder, "" when inline
    data: str  # base64, "" when beside the record
    mimetype: str
    reading: str  # the reading it shows, "" for none


def attachments(record: dict) -> list[Attachment]:
    """Every attachment of the record, in the order of its phases."""
    return [
        Attachment(
            name=str(name),
            label=str(a.get("label") or name),
            file=str(a.get("file") or ""),
            data=str(a.get("data") or ""),
            mimetype=str(a.get("mimetype") or ""),
            reading=str(a.get("reading") or ""),
        )
        for phase in _phases(record)
        for name, a in _dict(phase.get("attachments")).items()
        if isinstance(a, dict)
    ]


#: How a run keeps its pictures and files (``metadata.procedo.attachments``): beside its record,
#: or inside it as OpenHTF writes them (base64 ``data``).
SEPARATE, SINGLE = "separate", "single"


def inlined(record: dict, read: Callable[[str], bytes | None]) -> tuple[dict, list[str]]:
    """``record`` with each attachment kept beside it (``file``) inside it instead, as OpenHTF
    writes one (``mimetype``, ``sha1``, base64 ``data``; its ``label`` and ``reading`` kept), and
    ``metadata.procedo.attachments`` "single"; and the files so embedded, each once.
    ``read(file)``: the file's bytes, None to leave that attachment beside the record (so is one
    too big to hold). A new dict; no key the record did not have is added but that one."""
    embedded: list[str] = []
    phases = []
    for phase in _list(record.get("phases")):
        if isinstance(phase, dict) and _dict(phase.get("attachments")):
            phase = {
                **phase,
                "attachments": {
                    name: _inside(att, read, embedded) for name, att in phase["attachments"].items()
                },
            }
        phases.append(phase)
    found = {**record, "phases": phases} if "phases" in record else record
    return with_procedo(found, {"attachments": SINGLE}), embedded


def _inside(att: Any, read: Callable[[str], bytes | None], embedded: list[str]) -> Any:
    """One attachment of :func:`inlined`: inside the record, its file added to *embedded*; as it
    was when it names no file, or its file is not read or too big to hold."""
    file = str(att.get("file") or "") if isinstance(att, dict) else ""
    if not file:
        return att
    try:
        data = read(file)
        if data is None:
            return att
        inside = {
            **{k: v for k, v in att.items() if k != "file"},
            "sha1": hashlib.sha1(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        }
    except MemoryError:
        return att
    if file not in embedded:
        embedded.append(file)
    return inside


def calls(record: dict) -> list[dict]:
    """The ``Call`` steps' records, in the order they ran."""
    return [
        c
        for phase in _phases(record)
        for c in _list(_dict(phase.get("procedo")).get("calls"))
        if isinstance(c, dict)
    ]


def steps(record: dict) -> list[dict]:
    """The steps that ran, as the procedure's steps are listed (``{"n", "text"}``): what a
    test without a procedure (a code test) shows."""
    return [
        {
            "n": _dict(p.get("procedo")).get("step", i),
            "text": _dict(p.get("procedo")).get("text") or p.get("name", ""),
        }
        for i, p in enumerate(
            (p for p in _phases(record) if p.get("name") not in (_OPENHTF_TRIGGER, NOT_REACHED)), 1
        )
    ]


def log_lines(record: dict) -> list:
    """The run's log as the report lists it: each message (info and above) as text, each
    instrument exchange as ``{"cmd", "resp"}``."""
    out: list = []
    for entry in _list(record.get("log_records")):
        if not isinstance(entry, dict):
            continue
        exchange, level = entry.get("procedo"), entry.get("level") or 0
        if isinstance(exchange, dict) and "cmd" in exchange:
            out.append(exchange)
        elif not isinstance(level, int | float) or level >= 20:  # a level that is no number: shown
            out.append(str(entry.get("message", "")))
    return out


def value_text(value: Any, units: str) -> str:
    """A value with its unit, a base unit's number with the SI prefix that fits it
    (``1.89 µs``); another integer, or one a float cannot hold exactly, keeps its exact digits
    (a bus reading); a text stays text."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    number = isinstance(value, float) or (isinstance(value, int) and abs(value) <= 2**53)
    if number and value and units in _SI_BASES:
        scale, prefix = max(
            (p for p in _PREFIXES if abs(value) >= p[0] * 0.9999), default=_PREFIXES[0]
        )
        return f"{value / scale:.6g} {prefix}{units}"
    if isinstance(value, float):
        return f"{value:.6g} {units}".strip()
    return f"{value} {units}".strip()


def _dimension_name(dimension: Any, i: int) -> str:
    if isinstance(dimension, dict):
        return str(
            dimension.get("name")
            or dimension.get("description")
            or dimension.get("uom_suffix")
            or f"dim {i + 1}"
        )
    return f"dim {i + 1}"


def _table_html(dimensions: tuple[str, ...], rows: list, units: str) -> str:
    """A dimensioned measurement as a small table: one column per dimension, then the value."""
    head = "".join(f"<th>{html.escape(d)}</th>" for d in (*dimensions, f"value {units}".strip()))
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value_text(c, ''))}</td>" for c in row) + "</tr>"
        for row in rows
        if isinstance(row, list | tuple)
    )
    return f'<table class="dimensioned"><tr>{head}</tr>{body}</table>'
