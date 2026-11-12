"""What a well-formed object is (data_format.md §2, §4), checked before anything is written.

Every check refuses with ``400 bad_request`` (kind ``invalid``) and one sentence naming the
field. An optional field may be absent or ``null`` (§2), and is stored without it: an answer
leaves out a field it has no value for. ``""`` is refused where it would mean "not known" (a
board's or a test's name); a board's three names never begin or end with a blank.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timezone

from . import faults
from .errors import bad_request

SHA_HEX = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"sha256:([0-9a-f]{64})")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
TIME_MS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
RUN_ID = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z_[0-9a-f]{16}")
MACHINE_ID = re.compile(r"[0-9a-f]{8}")
PROJECT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TEST_ID = PROJECT_ID  # a test's hidden id: a lower-case UUID too (§2)

VERDICTS = ("PASS", "FAIL", "ERROR", "ABORTED", "PARTIAL", "SKIP")
FILE_ROLES = ("record", "log", "functions", "definition", "attachment")
MEASUREMENT_OUTCOMES = ("PASS", "FAIL", "UNSET", "PARTIALLY_SET", "SKIPPED")
CRITERION_KINDS = (
    "range",
    "max",
    "min",
    "equals",
    "tolerance",
    "one_of",
    "contains",
    "matches",
    "operator_decision",
    "monotonic",
    "aggregate",
    "and",
    "or",
    "expr",
)
ARTIFACT_KINDS = ("report", "export", "draft")
OVERRIDE_VALUES = VERDICTS  # an override sets one of a run's verdicts (§4.9)
RUN_ID_SHAPE = "YYYY-MM-DDTHH-MM-SSZ_<16 hex>"

RUN_FORMAT = 2
ARTIFACT_FORMAT = 2


# ---- Scalars ------------------------------------------------------------------------------


def nfc(name: str) -> str:
    """How a name is stored (§2): its Unicode NFC form."""
    if faults.on("2.4.nfc"):
        return name
    return unicodedata.normalize("NFC", name)


def name_key(name: str) -> str:
    """How two names compare (§2): Unicode NFC, then full case folding."""
    return unicodedata.normalize("NFC", nfc(name).casefold())


def drop_nulls(obj: dict, keep: tuple[str, ...] = ()) -> dict:
    """The object less its ``null`` fields: absent and ``null`` are one in a request (§2), and
    an answer leaves out a field it has no value for, except where ``null`` is a value."""
    return {key: value for key, value in obj.items() if value is not None or key in keep}


def is_time(value: object) -> bool:
    if not isinstance(value, str) or TIME.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def is_time_ms(value: object) -> bool:
    if not isinstance(value, str) or TIME_MS.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_json(data: bytes, what: str) -> dict:
    """A JSON object, strictly: UTF-8, and no NaN or infinity as a number (§2, Numbers)."""

    def refuse_constant(name: str) -> None:
        raise ValueError(f"{name} is not a JSON number")

    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=refuse_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise bad_request(f"The {what} is not valid JSON ({exc}).") from exc
    if not isinstance(obj, dict):
        raise bad_request(f"The {what} must be a JSON object.")
    return obj


def version_hex(value: object, what: str) -> str:
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise bad_request(f"{what} must be a version, sha256:<64 lower-case hex>.")
    return match.group(1)


# ---- Fields -------------------------------------------------------------------------------


def req_str(obj: dict, key: str, where: str, *, empty: bool = True) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise bad_request(f"The {where} needs {key} as a string.")
    if not empty and value == "":
        raise bad_request(f'The {where}\'s {key} is empty; "" is never used for "not known".')
    return value


def opt_str(obj: dict, key: str, where: str, *, empty: bool = True) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise bad_request(f"The {where}'s {key} must be a string.")
    if not empty and value == "":
        raise bad_request(
            f'The {where}\'s {key} is ""; leave it out (or null) for "none" (data_format.md §2).'
        )
    return value


def nullable_str(obj: dict, key: str, where: str) -> str | None:
    """A field that must be present, as a string or ``null``."""
    if key not in obj:
        raise bad_request(f"The {where} needs {key} (a string, or null).")
    value = obj[key]
    if value is not None and not isinstance(value, str):
        raise bad_request(f"The {where}'s {key} must be a string or null.")
    return value


def check_board_name(value: str, what: str) -> str:
    """A board's serial, part number or revision: never "" and no blank at either end (§2)."""
    if value == "" and not faults.on("2.6.empty-value-400"):
        raise bad_request(f'The {what} is ""; leave it out for "none" (data_format.md §2).')
    if value != value.strip() and not faults.on("2.6.blanks-400"):
        raise bad_request(f"The {what} {value!r} begins or ends with a blank (data_format.md §2).")
    return value


def string_values(obj: dict, key: str, where: str) -> None:
    """``extra``, ``fields``: the project's own fields, each value a string (§2)."""
    value = opt_object(obj, key, where)
    if faults.on("2.8.own-fields-strings"):
        return
    if value is not None and not all(isinstance(item, str) for item in value.values()):
        raise bad_request(f"The {where}'s {key} holds a value that is not a string.")


def req_bool(obj: dict, key: str, where: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise bad_request(f"The {where} needs {key} as true or false.")
    return value


def opt_bool(obj: dict, key: str, where: str) -> bool | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, bool):
        raise bad_request(f"The {where}'s {key} must be true or false.")
    return value


def req_int(obj: dict, key: str, where: str, *, minimum: int = 0) -> int:
    value = obj.get(key)
    if not is_int(value) or value < minimum:
        raise bad_request(f"The {where} needs {key} as an integer of at least {minimum}.")
    return value


def opt_int(obj: dict, key: str, where: str, *, minimum: int = 0) -> int | None:
    value = obj.get(key)
    if value is None:
        return None
    if not is_int(value) or value < minimum:
        raise bad_request(f"The {where}'s {key} must be an integer of at least {minimum}.")
    return value


def opt_number(obj: dict, key: str, where: str) -> None:
    value = obj.get(key)
    if value is not None and not is_number(value):
        raise bad_request(f"The {where}'s {key} must be a finite number.")


def req_pattern(obj: dict, key: str, where: str, pattern: re.Pattern, shape: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise bad_request(f"The {where} needs {key} as {shape}.")
    return value


def opt_pattern(obj: dict, key: str, where: str, pattern: re.Pattern, shape: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise bad_request(f"The {where}'s {key} must be {shape}.")
    return value


def req_time(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not is_time(value):
        raise bad_request(f"The {where} needs {key} as a UTC time, YYYY-MM-DDTHH:MM:SSZ.")
    return value


def opt_time(obj: dict, key: str, where: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not is_time(value):
        raise bad_request(f"The {where}'s {key} must be a UTC time, YYYY-MM-DDTHH:MM:SSZ.")
    return value


def opt_time_ms(obj: dict, key: str, where: str) -> None:
    value = obj.get(key)
    if value is not None and not is_time_ms(value):
        raise bad_request(f"The {where}'s {key} must be a UTC time with milliseconds.")


def opt_object(obj: dict, key: str, where: str) -> dict | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, dict):
        raise bad_request(f"The {where}'s {key} must be a JSON object.")
    return value


def opt_enum(obj: dict, key: str, where: str, allowed: tuple[str, ...]) -> str | None:
    value = obj.get(key)
    if value is not None and value not in allowed:
        raise bad_request(f"The {where}'s {key} must be one of {', '.join(allowed)}.")
    return value


def format_version(obj: dict, what: str, known: int) -> int:
    value = obj.get("format_version")
    if not is_int(value):
        raise bad_request(f"The {what} needs format_version as an integer of at least 1.")
    if value < 1 or value > known:
        raise bad_request(
            f"This {what} is format {value}; this server reads format {known} and older.",
            **(
                {}
                if faults.on("2.8.format-version-400")
                else {"format_version": value, "known": known}
            ),
        )
    return value


def import_flag(body: dict) -> bool:
    value = body.get("import")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise bad_request("import must be true or false.")
    return value


# ---- Objects ------------------------------------------------------------------------------


def validate_run(run: dict, *, lot_ok: bool = False) -> int:
    """A Run as E1 receives it (§4.5–§4.8). Returns its format_version. ``lot_ok``: a
    format-2 run may send ``lot``, a fault."""
    where = "run"
    version = format_version(run, "run", RUN_FORMAT)
    req_pattern(run, "run_id", where, RUN_ID, RUN_ID_SHAPE)
    opt_str(run, "project_id", where)
    opt_str(run, "project_name", where)
    check_board_name(req_str(run, "serial", where), "run's serial")
    for key in ("part_number", "board_revision"):
        value = opt_str(run, key, where)
        if value is not None and version >= 2:
            check_board_name(value, f"run's {key}")
        elif value == "":
            raise bad_request(f'The run\'s {key} is ""; leave it out for "none".')
    req_str(run, "test", where, empty=False)
    req_pattern(run, "test_id", where, TEST_ID, "a lower-case UUID")
    opt_str(run, "firmware_version", where)
    if version >= 2 and run.get("lot") is not None and not lot_ok:
        raise bad_request(
            "A format-2 run does not send lot: the lot is the board's (data_format.md §4.5)."
        )
    opt_str(run, "lot", where)
    string_values(run, "extra", where)
    opt_object(run, "bench_values", where)
    opt_time(run, "started", where)
    ended = opt_time(run, "ended", where)
    complete = req_bool(run, "complete", where)
    if complete and ended is None:
        raise bad_request("A complete run needs ended.")
    opt_enum(run, "verdict", where, VERDICTS)
    opt_str(run, "operator", where)
    req_pattern(run, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
    opt_str(run, "station_id", where)
    opt_int(run, "test_version", where, minimum=1)
    req_bool(run, "draft", where)
    opt_pattern(run, "definition_sha256", where, SHA_HEX, "64 lower-case hex digits")
    for key in ("bundle_id", "bundle_version", "app_version"):
        opt_str(run, key, where)
    phases = run.get("phases")
    if not isinstance(phases, list):
        raise bad_request("The run needs phases, a list ([] for an incomplete run).")
    if not complete and phases:
        raise bad_request("An incomplete run has no record, so its phases are [].")
    _validate_phases(phases)
    files = run.get("files")
    if not isinstance(files, list):
        raise bad_request("The run needs files, a list of RunFile.")
    names = set()
    for item in files:
        validate_file_ref(item, "run file")
        if item["name"] in names:
            raise bad_request(f"The run lists the file {item['name']!r} twice.")
        names.add(item["name"])
    return version


def _validate_phases(phases: list) -> None:
    phase_ords = set()
    measurement_ords = set()
    for phase in phases:
        if not isinstance(phase, dict):
            raise bad_request("Each phase must be a JSON object.")
        where = "phase"
        order = req_int(phase, "ord", where, minimum=1)
        if order in phase_ords:
            raise bad_request(f"Two phases have ord {order}.")
        phase_ords.add(order)
        req_str(phase, "name", where)
        req_int(phase, "attempt", where, minimum=1)
        opt_str(phase, "outcome", where)
        opt_time_ms(phase, "started", where)
        opt_time_ms(phase, "ended", where)
        measurements = phase.get("measurements")
        if not isinstance(measurements, list):
            raise bad_request(f"Phase {order} needs measurements, a list ([] for none).")
        names = set()
        for measurement in measurements:
            if not isinstance(measurement, dict):
                raise bad_request("Each measurement must be a JSON object.")
            mwhere = f"measurement of phase {order}"
            name = req_str(measurement, "name", mwhere)
            if name in names:
                raise bad_request(f"Phase {order} holds the measurement {name!r} twice.")
            names.add(name)
            m_ord = req_int(measurement, "ord", mwhere, minimum=1)
            if m_ord in measurement_ords:
                raise bad_request(f"Two measurements have ord {m_ord}.")
            measurement_ords.add(m_ord)
            opt_number(measurement, "value_num", mwhere)
            opt_str(measurement, "value_text", mwhere)
            opt_str(measurement, "units", mwhere)
            opt_enum(measurement, "outcome", mwhere, MEASUREMENT_OUTCOMES)
            validators = measurement.get("validators")
            if validators is not None and (
                not isinstance(validators, list) or not all(isinstance(v, str) for v in validators)
            ):
                raise bad_request(f"The {mwhere}'s validators must be a list of strings.")
            opt_enum(measurement, "criterion_kind", mwhere, CRITERION_KINDS)
            opt_number(measurement, "limit_low", mwhere)
            opt_number(measurement, "limit_high", mwhere)
            criterion = opt_object(measurement, "criterion", mwhere)
            if criterion is not None and not isinstance(criterion.get("kind"), str):
                raise bad_request(f"The {mwhere}'s criterion needs its kind.")


def validate_file_ref(item: object, where: str) -> None:
    """A RunFile (§4.8): a reference to a storage."""
    if not isinstance(item, dict):
        raise bad_request(f"Each {where} must be a JSON object.")
    req_str(item, "name", where, empty=False)
    if item.get("role") not in FILE_ROLES:
        raise bad_request(f"The {where}'s role must be one of {', '.join(FILE_ROLES)}.")
    req_int(item, "size", where)
    req_pattern(item, "sha256", where, SHA_HEX, "64 lower-case hex digits")
    opt_str(item, "mimetype", where)
    req_int(item, "store_id", where)


def validate_artifact(
    art: dict, *, blank_serial_ok: bool = False, info_optional: bool = False
) -> int:
    """An Artifact as E10 receives it (§4.10). Returns its format_version.
    ``blank_serial_ok``: a report or draft may have serial ``""``, a fault; ``info_optional``:
    a report may come without its record, a fault."""
    where = "artifact"
    version = format_version(art, "artifact", ARTIFACT_FORMAT)
    kind = art.get("kind")
    if kind not in ARTIFACT_KINDS:
        raise bad_request(f"The artifact's kind must be one of {', '.join(ARTIFACT_KINDS)}.")
    serial = req_str(art, "serial", where)
    if kind == "export" and serial != "":
        raise bad_request('An export is project-level: its serial is "".')
    if kind != "export" and not (blank_serial_ok and serial == ""):
        check_board_name(serial, f"{kind}'s serial")
    for key in ("part_number", "board_revision"):
        value = opt_str(art, key, where)
        if value is None:
            continue
        if kind == "export":
            raise bad_request("A project-level export has no part number and no revision.")
        check_board_name(value, f"{kind}'s {key}")
    req_str(art, "name", where, empty=False)
    opt_str(art, "work_order", where)
    string_values(art, "fields", where)
    opt_object(art, "info", where)
    if kind == "report" and art.get("info") is None and not info_optional:
        raise bad_request("A report is its record: it carries info, a JSON object (§5.10).")
    opt_time(art, "generated", where)
    opt_str(art, "verdict", where)
    opt_str(art, "operator", where)
    tests = art.get("tests")
    if tests is not None and (
        not isinstance(tests, list) or not all(isinstance(t, str) for t in tests)
    ):
        raise bad_request("The artifact's tests must be a list of test names.")
    req_pattern(art, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
    req_time(art, "updated_at", where)
    req_pattern(art, "sha256", where, SHA_HEX, "64 lower-case hex digits")
    if kind == "draft":
        if not isinstance(art.get("content"), dict):
            raise bad_request("A draft needs its content, a JSON object.")
        if any(art.get(key) is not None for key in ("size", "store_id")):
            raise bad_request("A draft has no file: no size or store_id.")
        opt_pattern(art, "replaces", where, SHA_HEX, "64 lower-case hex digits, or null")
    else:
        req_int(art, "size", where)
        req_int(art, "store_id", where)
    return version


def validate_annotation_entry(entry: object, *, any_value: bool = False) -> None:
    """An AnnotationEntry (§4.9); ``any_value``: an override takes any value, a fault."""
    if not isinstance(entry, dict):
        raise bad_request("Each annotation entry must be a JSON object.")
    where = "annotation entry"
    req_str(entry, "entry_id", where, empty=False)
    kind = entry.get("kind")
    if kind not in ("comment", "override"):
        raise bad_request("An annotation entry's kind is comment or override.")
    req_str(entry, "key", where, empty=False)
    if kind == "comment":
        nullable_str(entry, "text", where)
    else:
        value = nullable_str(entry, "value", where)
        if value is not None and value not in OVERRIDE_VALUES and not any_value:
            raise bad_request(
                f"An override's value is one of {', '.join(OVERRIDE_VALUES)}, or null to clear it."
            )
    nullable_str(entry, "replaces", where)
    req_str(entry, "by", where)
    req_pattern(entry, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
    opt_str(entry, "bench", where)
    req_time(entry, "saved_at", where)


def validate_board_entry(entry: object) -> None:
    """A BoardEntry (§4.11)."""
    if not isinstance(entry, dict):
        raise bad_request("Each board entry must be a JSON object.")
    where = "board entry"
    req_str(entry, "entry_id", where, empty=False)
    kind = entry.get("kind")
    if kind not in ("lot", "field"):
        raise bad_request("A board entry's kind is lot or field.")
    if kind == "field":
        req_str(entry, "name", where, empty=False)
    nullable_str(entry, "value", where)
    nullable_str(entry, "replaces", where)
    req_str(entry, "by", where)
    req_pattern(entry, "machine_id", where, MACHINE_ID, "8 lower-case hex digits")
    opt_str(entry, "bench", where)
    req_time(entry, "saved_at", where)


def entries_of(body: dict, check) -> list[dict]:
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise bad_request("The body needs entries, a list.")
    for entry in entries:
        check(entry)
    return entries
