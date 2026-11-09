"""This PC's bench values: each test's instrument addresses and remote/manual, kept on this PC.

A test's ``test.py`` on the shared project holds its constants (``PSU1_VISA = "…"``,
``PSU1_REMOTE = True``); two benches with different equipment cannot share them. So a bench's
own values never go into that file: they are kept in ``bench_values.json`` in the project's
per-PC folder (``project_local_state.bench_state_file``), per test,
``{"<test>": {"PSU1_VISA": "TCPIP0::10.0.0.5::INSTR", "PSU1_REMOTE": true}}``. A constant this
PC never set keeps the project's value.

The one seam, two operations:

* :func:`effective_source` — a test's ``test.py`` text with this PC's values written into their
  constants: what every reader of a bench value reads (the Equipment window through
  :func:`read_text`, the session's claims, the run's copy…), never the file;
* :func:`set_values` — what every writer of a bench value calls, never the file.

Which constants are a bench's: :func:`is_bench_name` (addresses and remote/manual; timeouts,
channels and the rest stay the project's, the same for everyone).
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from . import (
    atomic_write,
    equipment_profiles,
    file_cache,
    project_local_state,
    test_format,
)
from .file_cache import FileCache

_FILE = "bench_values.json"
logger = logging.getLogger(__name__)
_damaged: set[Path] = set()  # each damaged file logged once
#: An instrument's address (``_VISA``, ``_PORT``, ``_ADDRESS`` — ``bench_identity.ADDRESS_FIELDS``
#: — and a serial port's ``_BAUD``) and its remote flag; the profile-declared manual override
#: (``override_suffix``) is one too (:func:`is_bench_name`).
_SUFFIXES = ("_VISA", "_PORT", "_ADDRESS", "_BAUD", "_REMOTE")

_lock = threading.Lock()  # a write is read-change-write: the Equipment window's worker and the GUI


def is_bench_name(name: str) -> bool:
    """Whether the constant *name* is a bench value — this PC's own: an address
    (``<EID>_VISA``, ``_PORT``, ``_ADDRESS``, ``_BAUD``) or remote/manual (``_REMOTE``, the
    profile's ``override_suffix`` such as ``_MANUAL_OVERRIDE``)."""
    return name.endswith(_suffixes())


def _suffixes() -> tuple[str, ...]:
    overrides = tuple(suffix for _pattern, suffix in equipment_profiles.get_all_override_info())
    return _SUFFIXES + overrides


def effective_source(script: Path, source: str) -> str:
    """*source* — the text of the test's ``test.py`` at *script* (``tests/<test>/test.py``, a
    code test's ``openhtf/test.py``) — with this PC's values written into their constants. A
    constant the text no longer has is skipped."""
    for name, value in values_of(script).items():
        source = rewritten(source, name, *_token(value)) or source
    return source


def read_text(script: Path, files: FileCache | None = None) -> str | None:
    """The text of the test script *script* as it runs on this PC (:func:`effective_source`) —
    what the equipment scanners read every constant from; None when it cannot be read. *files*:
    the open project's cache, kept until the test's folder is said to change (as
    :func:`set_values` says)."""
    if files is None:
        return _effective_text(script, None)
    return files.get_or_compute("bench text", script, lambda p: _effective_text(p, files))


def values_of(script: Path) -> dict[str, Any]:
    """This PC's values for the test of *script*, ``{constant: value}``; {} for none."""
    where = _where(script)
    path = _store(where[0]) if where else None
    own = _read(path).get(where[1]) if path else None
    return own if isinstance(own, dict) else {}


def set_values(script: Path, values: dict[str, Any]) -> bool:
    """Keep *values* (``{constant: value}``: a str or None for an address, a bool for
    remote/manual, a number for a numeric one) as this PC's for the test of *script*; its
    ``test.py`` is never written. A kept value whose constant the test no longer has goes.
    True (written).

    Raises:
        ValueError: a constant that is not a bench value (:func:`is_bench_name`).
        OSError: the project has no id (``NO_PROJECT_ID``), its test or the store cannot be
            read or written.
    """
    if not all(is_bench_name(name) for name in values):
        raise ValueError(f"not bench values: {sorted(values)}")
    where = _where(script)
    path = _store(where[0]) if where else None
    if path is None:
        raise OSError(project_local_state.NO_PROJECT_ID)
    source = Path(script).read_text(encoding="utf-8")
    test = where[1]
    with _lock:
        data = _read_to_change(path)
        own = {**(data.get(test) or {}), **values}
        data[test] = {n: v for n, v in own.items() if rewritten(source, n, *_token(v)) is not None}
        atomic_write.write_json(path, data)
    file_cache.written(Path(script))  # what was read of its constants is stale
    return True


def rename_test(project_root: Path, old: str, new: str) -> None:
    """The test *old* is now *new*: its values follow."""
    path = _store(project_root)
    if path is None:
        return
    with _lock:
        data = _read_to_change(path)
        if old in data:
            data[new] = data.pop(old)
            atomic_write.write_json(path, data)


def copy_test(project_root: Path, source: str, new: str) -> None:
    """The test *new* is a copy of *source* (Studio's Duplicate Test): it gets *source*'s values,
    so it runs on the same instruments."""
    path = _store(project_root)
    if path is None:
        return
    with _lock:
        data = _read_to_change(path)
        if source in data:
            data[new] = copy.deepcopy(data[source])
            atomic_write.write_json(path, data)


def forget_test(project_root: Path, test: str) -> None:
    """The test *test* was deleted: its values go."""
    path = _store(project_root)
    if path is None:
        return
    with _lock:
        data = _read_to_change(path)
        if data.pop(test, None) is not None:
            atomic_write.write_json(path, data)


def used(source: str) -> dict[str, Any]:
    """The bench values *source* (a ``test.py`` as it runs) holds, ``{constant: value}``: what a
    run records it was measured on. {} when it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found, suffixes = {}, _suffixes()
    for node in tree.body:  # module level: where codegen writes them
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith(suffixes)
        ):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass  # not a literal: no value to record
    return found


def rewritten(
    content: str,
    var_name: str,
    new_value: str | None,
    is_bool: bool = False,
    is_number: bool = False,
    is_list: bool = False,
) -> str | None:
    """*content* with the literal of the module constant *var_name* replaced by *new_value*;
    None when it holds no such constant to replace with it. Only that literal changes, every
    other character stays (LF or CRLF line endings alike).

    *new_value*: ``None`` or a string for an address (written quoted), ``"True"``/``"False"``
    (*is_bool*), the ``str()`` of an int/float (*is_number*: a bare numeric literal), a flat list
    literal like ``"[1, 2]"`` (*is_list*). The flags are mutually exclusive.
    """
    if is_list:
        # Flat list literal RHS ([ints/whitespace/commas], no nesting). The
        # new value must itself be a bracketed literal so a failed caller can
        # never half-write prose into code.
        pattern = re.compile(
            rf"^({re.escape(var_name)})\s*=\s*\[[^\]#\n]*\](?=\s*(?:#.*)?$)",
            re.MULTILINE,
        )
        if not (
            isinstance(new_value, str) and new_value.startswith("[") and new_value.endswith("]")
        ):
            return None
        replacement = lambda m: f"{m.group(1)} = {new_value}"
    elif is_bool:
        pattern = re.compile(rf"^({re.escape(var_name)})\s*=\s*(True|False)", re.MULTILINE)
        if new_value not in ("True", "False"):
            return None
        replacement = rf"\1 = {new_value}"
    elif is_number:
        # Bare numeric literal (int / float / exponent), written without quotes.
        # The RHS starts with a digit, so it is mutually exclusive with the
        # quoted/None (VISA) and True/False (remote) patterns. The trailing
        # lookahead pins the match to a COMPLETE numeric RHS (only whitespace /
        # comment / EOL may follow), so the leading ``0`` of a hex literal like
        # ``0x1F`` can't be half-replaced into ``99x1F``.
        pattern = re.compile(
            rf"^({re.escape(var_name)})\s*=\s*[-+]?[0-9][0-9_]*\.?[0-9]*(?:[eE][-+]?[0-9]+)?(?=\s*(?:#.*)?$)",
            re.MULTILINE,
        )
        if new_value is None:
            return None
        # Callable replacement: avoids re interpreting a stray backslash / group
        # reference inside ``new_value`` (defensive — numeric input has none).
        replacement = lambda m: f"{m.group(1)} = {new_value}"
    else:  # an address: None or a quoted string
        pattern = re.compile(
            rf'^({re.escape(var_name)})\s*=\s*(None|"[^"]*"|\'[^\']*\')', re.MULTILINE
        )
        if new_value is None:
            replacement = r"\1 = None"
        else:
            escaped = new_value.replace('"', '\\"')
            replacement = rf'\1 = "{escaped}"'
    new_content, count = pattern.subn(replacement, content)
    return new_content if count else None


def _token(value: Any) -> tuple[str | None, bool, bool]:
    """``(new_value, is_bool, is_number)`` of a kept *value* for :func:`rewritten`."""
    if isinstance(value, bool):
        return str(value), True, False
    if isinstance(value, (int, float)):
        return str(value), False, True
    return value, False, False


def _effective_text(script: Path, files: FileCache | None) -> str | None:
    text = file_cache.read_text(script, files)
    return None if text is None else effective_source(script, text)


def _where(script: Path) -> tuple[Path, str] | None:
    """``(project root, test)`` of a test's script, None for a script outside a ``tests/``."""
    test_dir = test_format.test_dir_of(Path(script))
    if test_dir.parent.name != "tests":
        return None
    return test_dir.parent.parent, test_dir.name


def _store(project_root: Path) -> Path | None:
    """This PC's ``bench_values.json`` for the project; None without a project id."""
    return project_local_state.bench_state_file(project_root, _FILE)


def _read(path: Path) -> dict:
    """The kept values, ``{test: {constant: value}}``, to use them: {} when none, and when the
    file is damaged (logged; the project's values are used meanwhile)."""
    try:
        return _read_to_change(path)
    except OSError as e:
        if path not in _damaged:
            _damaged.add(path)
            logger.warning("%s", e)
        return {}


def _read_to_change(path: Path) -> dict:
    """The kept values, to change them: {} when none. Raises OSError, so nothing overwrites
    them, when the file is there but cannot be read or holds no values."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        data = e
    if not isinstance(data, dict):
        raise OSError(
            f"This PC's equipment values cannot be read ({path}: {data}). Fix or delete that "
            "file; until then the project's values are used."
        )
    return data
