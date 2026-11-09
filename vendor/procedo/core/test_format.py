"""Which version of a test runs: its procedure text or its OpenHTF code.

A test folder holds a text version (``procedure_text.md``, compiled to ``procedure.json`` +
``test.py`` at its root) and/or a code version (``openhtf/test.py`` and its helpers). Its
``test.json`` says which one is active: ``{"active": "text"}`` or ``{"active": "code"}``.
Without one, a folder with ``procedure_text.md`` is a text test, else one with
``openhtf/test.py`` is a code test (every test made before code tests stays a text test).
``test.json`` may also hold the number the test was stamped with before releases existed
(``"version"``, and ``"version_sha256"``, the fingerprint it had then: :func:`read_version`); nothing
writes it any more, ``test_release`` reads it as a release whose files were not kept.

``test.json`` also holds the test's hidden id, ``"test_id"`` (:data:`TEST_ID`): a lower-case
UUID the app mints when the test is made (``starter_test``) and, for a test without one, when its
project is opened (``project_seeding.ensure_test_ids``); a rename keeps it, the user never sees it
(``docs/plans/data_format.md`` §2, §4.3). It is NOT the test's name: an identifier holding the
name is called ``test_key`` (``starter_test.sanitize_test_key``, ``bench_claims``,
``session_policies``, Studio's ``core/test_key``). Studio's Duplicate Test gives the copy its
own id (:func:`replace_test_id`); a folder copied outside the apps holds its source's id until the
project's open gives it its own (``test_id_duplicates``, :func:`replace_test_id` too).

A folder of ``tests/`` whose name starts with a dot is no test (:func:`is_test_folder`): Studio
copies a test into ``.<name>.copying`` and sets one it deletes aside as ``.<name>.deleting``;
every list of the tests leaves them out, and the project sync never carries those two
(``project_never_travels.exclusion``).

The ONE place that decides a test's format and reads or writes its ``test.json``: Bench, Studio
and their tools ask :func:`active_format`. Engine-free and Qt-free.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Collection
from pathlib import Path

from . import atomic_write, file_cache
from .file_cache import FileCache

logger = logging.getLogger(__name__)

TEXT = "text"
CODE = "code"
TEST_JSON = "test.json"
#: The code version's folder in a test folder: its ``test.py`` and helper files.
CODE_DIR = "openhtf"
#: The code version's script, in its test folder.
CODE_SCRIPT = f"{CODE_DIR}/test.py"
#: ``test.json``'s keys for the number stamped before releases: the number (1, 2…) and the
#: fingerprint (``run_provenance.definition_sha256``) the test had when it got that number.
VERSION = "version"
VERSION_SHA256 = "version_sha256"
#: ``test.json``'s key for the test's hidden id (a lower-case UUID, never its name).
TEST_ID = "test_id"

#: The OpenHTF a code test runs on: a bundle ships it when its Python packages say this line.
OPENHTF_PIN = "openhtf==1.6.3"

#: The starter template's import of ``substep`` (Studio "New test → Code"): a test without the
#: ``procedo-htf`` package still runs, under plain OpenHTF, its sub-steps doing nothing.
SUBSTEP_IMPORT = """\
try:
    from procedo_htf import substep
except ImportError:
    def substep(text): pass
"""


def active_format(test_dir: Path, files: FileCache | None = None) -> str:
    """:data:`TEXT` or :data:`CODE`: the version of the test in *test_dir* that runs
    (:func:`read_format`)."""
    return read_format(test_dir, files)[0]


def script(test_dir: Path, files: FileCache | None = None) -> Path:
    """The ``test.py`` of the version of the test in *test_dir* that runs: ``openhtf/test.py``
    for a code test (its root ``test.py`` is the inactive text version's), else the root one."""
    return script_of(test_dir, active_format(test_dir, files))


def script_of(test_dir: Path, active: str) -> Path:
    """:func:`script` of a test whose format (*active*) was read already."""
    return test_dir / CODE_SCRIPT if active == CODE else test_dir / "test.py"


def test_dir_of(script_path: Path) -> Path:
    """The test folder of a script :func:`script` names: ``tests/<test>`` for
    ``tests/<test>/test.py`` and for ``tests/<test>/openhtf/test.py``."""
    folder = script_path.parent
    if folder.name == CODE_DIR and folder.parent.name != "tests":
        return folder.parent
    return folder


def read_format(test_dir: Path, files: FileCache | None = None) -> tuple[str, str]:
    """``(format, problem)`` of the test in *test_dir* (:func:`read_test`)."""
    return read_test(test_dir, files)[:2]


def read_test(test_dir: Path, files: FileCache | None = None) -> tuple[str, str, str]:
    """``(format, problem, test_id)`` of the test in *test_dir*: :data:`TEXT` or :data:`CODE`,
    why its ``test.json`` could not say ("" when it could, or when it has none), and its hidden id
    (:func:`test_id_in`; "" when it has none).

    ``test.json``'s ``"active"`` decides. Without a ``test.json``: code when the folder has
    ``openhtf/test.py`` and no ``procedure_text.md``, else text. A ``test.json`` that cannot be
    read or names neither, or a folder that cannot be looked at, makes the test a code test —
    never its text version run by mistake — and is logged. *files*: the open project's cache."""
    path = test_dir / TEST_JSON
    text = file_cache.read(path, files)
    if isinstance(text, FileNotFoundError):
        try:  # Python 3.13 raises from exists() when a share drops
            code = not file_cache.exists(test_dir / "procedure_text.md", files) and (
                file_cache.exists(script_of(test_dir, CODE), files)
            )
        except OSError as exc:
            return (
                CODE,
                f"Its folder cannot be read ({exc.strerror or exc}): it is taken as a code test.",
                "",
            )
        return (CODE if code else TEXT), "", ""
    test_id = test_id_in(text) if isinstance(text, str) else ""
    active, reason = _named(text)
    if active:
        return active, "", test_id
    logger.warning("%s: %s; the test is taken as a code test", path, reason)
    return CODE, f"Its {TEST_JSON} cannot be read ({reason}): it is taken as a code test.", test_id


def format_of(test_json: str | None, names: Collection[str]) -> str:
    """:func:`read_format`'s rule on a test a storage holds: *test_json* the text of its
    ``test.json`` (``None``: it has none), *names* its files' paths in its folder."""
    if test_json is None:
        return CODE if CODE_SCRIPT in names and "procedure_text.md" not in names else TEXT
    return _named(test_json)[0] or CODE


def _named(text: str | Exception) -> tuple[str, str]:
    """``(version, "")`` that a ``test.json``'s text names, else ``("", why not)`` — *text* may
    be the error reading it."""
    data, reason = _parsed(text)
    if reason:
        return "", reason
    active = data.get("active") if isinstance(data, dict) else None
    if active not in (TEXT, CODE):
        return "", f'it names no active version, "{TEXT}" or "{CODE}"'
    return active, ""


def _parsed(text: str | Exception) -> tuple[object, str]:
    """``(data, "")`` that a ``test.json``'s text holds, else ``(None, why not)`` — *text* may be
    the error reading it."""
    if isinstance(text, OSError):
        return None, text.strerror or str(text)
    if isinstance(text, ValueError):
        return None, "it is not UTF-8 text"
    try:
        return json.loads(text.lstrip("\ufeff")), ""  # a byte-order mark: Windows editors write one
    except ValueError:
        return None, "it is not JSON"


def is_test_folder(name: str) -> bool:
    """Whether the folder of ``tests/`` named *name* is a test: not when its name starts with a
    dot, a hidden folder (a test Studio is copying, ``.<name>.copying``, or deleting,
    ``.<name>.deleting``). Every list of a project's tests asks it."""
    return not name.startswith(".")


def read_test_id(test_dir: Path, files: FileCache | None = None) -> str:
    """The hidden id the test in *test_dir*'s ``test.json`` holds (:func:`test_id_in`); "" when it
    has none, or no ``test.json`` that can be read. *files*: the open project's cache."""
    text = file_cache.read(test_dir / TEST_JSON, files)
    return test_id_in(text) if isinstance(text, str) else ""


def name_of(project_root: Path, test_id: str) -> str:
    """The name of the test of the project at *project_root* whose ``test.json`` holds *test_id*:
    its folder's name in ``tests/``, as spelled there; "" when none does, or ``tests/`` cannot be
    listed. Reads every test's ``test.json``."""
    try:
        folders = sorted(
            p
            for p in (Path(project_root) / "tests").iterdir()
            if is_test_folder(p.name) and p.is_dir()
        )
    except OSError:
        return ""
    return next((p.name for p in folders if read_test_id(p) == test_id), "")


def test_id_in(test_json: str) -> str:
    """The hidden id a ``test.json``'s text *test_json* holds: its ``"test_id"`` when that is a
    lower-case UUID (:func:`is_test_id`), else ""."""
    data = _parsed(test_json)[0]
    value = data.get(TEST_ID) if isinstance(data, dict) else None
    return value if is_test_id(value) else ""


def is_test_id(value: object) -> bool:
    """Whether *value* is a test's hidden id: a UUID written in lower case
    (``3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17``), as :func:`new_test_id` makes them."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def new_test_id() -> str:
    """A new hidden id for a test (a new test, or a copy of one): a random UUID, lower case."""
    return str(uuid.uuid4())


def ensure_test_id(test_dir: Path) -> str:
    """The hidden id of the test in *test_dir* (a UUID, never its name), minted into its
    ``test.json`` when it holds none; "" when it has none and gets none.

    The id is merged into the ``test.json`` as :func:`write_active` merges, the rest it holds
    kept; a test without a ``test.json`` gets one naming the version that runs now
    (:func:`read_format`'s rule), so making the file changes nothing else. Then the file is read
    again and what it holds is the answer: another writer that landed in between wins, as the
    project's id does (``project_identity.ensure_project_id_locked``).

    Never overwritten, and logged: a ``test.json`` that cannot be read or holds no JSON object, or
    whose ``"test_id"`` is not one (written by hand). A folder holding nothing is no test: nothing
    is written in it. OSError when the file cannot be written."""
    path = test_dir / TEST_JSON
    text = file_cache.read(path)
    if isinstance(text, FileNotFoundError):
        data = _new_test_json(test_dir)
        if data is None:
            return ""
    else:
        parsed, reason = _parsed(text)
        if not isinstance(parsed, dict):
            reason = reason or "it holds no JSON object"
            logger.warning("%s cannot be read (%s): no test id given", path, reason)
            return ""
        if TEST_ID in parsed:
            if is_test_id(parsed[TEST_ID]):
                return parsed[TEST_ID]
            logger.warning("%s: its %r is not a test id; left as it is", path, TEST_ID)
            return ""
        data = parsed
    minted = new_test_id()
    write_json(test_dir, {**data, TEST_ID: minted})
    held = read_test(test_dir)[2] or minted
    if held != minted:
        logger.info("%s: test id race, adopting %s over %s", path, held, minted)
    return held


def replace_test_id(test_dir: Path) -> str:
    """A new hidden id written over the one the test in *test_dir*'s ``test.json`` holds, the rest
    it holds kept; the new id. ONLY for a copy of another test, which holds its source's id:
    Studio's Duplicate Test (``procedo.studio.core.test_folder_ops.duplicate_test``, on the copy
    before it gets its name) and a folder copied outside the apps (``test_id_duplicates.settle``,
    under the configuration lock). A test keeps its id for life otherwise (:func:`ensure_test_id`
    never overwrites one). "" and nothing written, logged, when the ``test.json`` cannot be read
    or holds no JSON object. OSError when it cannot be written."""
    data = read_json(test_dir)
    if not data:
        logger.warning("%s cannot be read: no new test id given", test_dir / TEST_JSON)
        return ""
    replaced = new_test_id()
    write_json(test_dir, {**data, TEST_ID: replaced})
    return replaced


def _new_test_json(test_dir: Path) -> dict | None:
    """What a ``test.json`` made for the test in *test_dir*, which has none, starts with: the
    version that runs now; None when the folder holds nothing (no test) or cannot be looked at."""
    try:
        if not any(test_dir.iterdir()):
            return None
    except OSError as exc:
        logger.warning("%s cannot be read (%s): no test id given", test_dir, exc)
        return None
    active, problem = read_format(test_dir)
    if problem:
        logger.warning("%s: %s No test id given", test_dir, problem)
        return None
    return {"active": active}


def read_json(test_dir: Path, files: FileCache | None = None) -> dict | None:
    """What the test in *test_dir*'s ``test.json`` holds: ``{}`` without one, None when it holds
    no JSON object or cannot be read (:func:`read_format` says why). *files*: the open project's
    cache."""
    text = file_cache.read(test_dir / TEST_JSON, files)
    if isinstance(text, FileNotFoundError):
        return {}
    data = _parsed(text)[0]
    return data if isinstance(data, dict) else None


def read_version(test_dir: Path, files: FileCache | None = None) -> tuple[int, str] | None:
    """:func:`version_in` the test in *test_dir*'s ``test.json`` (*files*: the open project's
    cache)."""
    return version_in(read_json(test_dir, files) or {})


def version_in(data: dict) -> tuple[int, str] | None:
    """``(number, fingerprint)`` of the version a ``test.json``'s *data* holds; None when it holds
    none, or one that cannot say (written by hand)."""
    number, sha256 = data.get(VERSION), data.get(VERSION_SHA256)
    if type(number) is not int or number < 1 or not isinstance(sha256, str):
        return None
    return number, sha256


def write_json(test_dir: Path, data: dict) -> None:
    """The test in *test_dir*'s ``test.json`` holds *data*, written in one step."""
    atomic_write.write_text(test_dir / TEST_JSON, json.dumps(data) + "\n")


def write_active(test_dir: Path, active: str) -> None:
    """Make *active* (:data:`TEXT` or :data:`CODE`) the version of the test in *test_dir* that
    runs: its ``test.json`` says so (Studio's Text | Code switch, its New test), the rest it holds
    (its test id, a number stamped before releases) kept — a ``test.json`` that cannot be read is
    replaced."""
    write_json(test_dir, {**(read_json(test_dir) or {}), "active": active})


def ships_openhtf(bundle) -> bool:
    """Whether *bundle* (``project_bundle.read_active_bundle``'s, None for none) ships OpenHTF:
    one of the Python packages it pinned."""
    packages = bundle.manifest.get("python_packages") if bundle is not None else None
    return any(
        isinstance(entry, dict)
        and str(entry.get("resolved", "")).split("==")[0].strip().lower() == "openhtf"
        for entry in (packages if isinstance(packages, list) else [])
    )
