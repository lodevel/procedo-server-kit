"""A project's files (data_format.md §2, §4.1, §4.2): their paths, the text rule, their parts.

A path is the file's ``/`` path in the project folder. Paths compare as names do (NFC, then
case folding), and a new file takes the spelling of the folders it lands in, the first met, as
on the case-insensitive disks the app writes; every answer gives that spelling. Each of its
names is one every system the app runs on creates as spelled (the app's sync refuses any
other): no control character, none of ``<>:"|?*``, no trailing dot or blank, no Windows device
name, at most 255 characters.

What never travels is the app's sync exclusion list (``project_never_travels.exclusion``), plus the
two folders that travel as objects of their own: ``bundle/`` (the Bundle) and ``releases/``
(the Releases). Both are asked of the app's own module (the one module of the app this server
imports), so the server refuses exactly what the app's sync leaves out, a name added there
included.

The text rule (§2) depends on the path and the bytes only: a file under ``tests/``,
``functions/``, ``config/`` or ``scenarios/`` that is valid UTF-8 travels as ``text``; every
other file is a reference to a storage (``store_id``), its bytes a part of the same body when
the storage is ``api``. A reference there whose ``api`` bytes are UTF-8 is refused: the server
holds those bytes and can tell. A declared storage's bytes are assumed to follow the rule.
"""

from __future__ import annotations

import copy
import hashlib
import re

from procedo.core.project_never_travels import OBJECT_DIRS, exclusion

from . import faults
from .artifacts import part_type
from .errors import bad_request
from .multipart import Part
from .state import API_STORE_ID, State, StoredFile
from .validate import drop_nulls, is_int, name_key, nfc, req_int, version_hex

TEXT_FOLDERS = ("tests", "functions", "config", "scenarios")
WINDOWS_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"{port}{n}" for port in ("COM", "LPT") for n in "123456789"}
)
MAX_NAME = 255
FILE_ANSWER_FIELDS = ("mtime",)


# ---- Paths --------------------------------------------------------------------------------------


def safe_name(name: str) -> bool:
    """One name every system the app runs on creates as spelled (the app's ``safe_component``
    and its sync's Windows rule): not empty, ``.`` or ``..``; no ``/``, ``\\``, control character or
    any of ``<>:"|?*``; no trailing dot or blank; no device name (``CON``, ``com1.txt``…)."""
    return not (
        name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or WINDOWS_ILLEGAL.search(name) is not None
        or name[-1] in ". "
        or (name.split(".")[0].upper() in WINDOWS_DEVICES and not faults.on("2.10.path-names-400"))
        or len(name) > MAX_NAME
    )


def check_path(value: object, what: str, *, folder: bool = False, names: bool = True) -> str:
    """A relative ``/`` path, in NFC, each of its names a safe one (:func:`safe_name`), no
    blank at either end; a folder may end with one ``/``. Else ``400 bad_request``. With
    ``names`` false (a fault only), its names are not checked."""
    if not isinstance(value, str) or value == "":
        raise bad_request(f"{what} must be a / path in the project.")
    path = nfc(value.removesuffix("/") if folder else value)
    if names and (path != path.strip() or not all(safe_name(n) for n in path.split("/"))):
        raise bad_request(
            f"{what} {value!r} is not a / path every PC can hold: each name without control "
            'characters, <>:"|?* or a trailing dot or blank, and not a device name such as CON.'
        )
    return path


def is_text_path(key: str) -> bool:
    """Under one of the four folders whose UTF-8 files travel as text (§2)."""
    return "/" in key and key.split("/", 1)[0] in TEXT_FOLDERS


def never_travels(path: str) -> bool:
    """The path, as spelled, is not a project file (§4.2): under ``bundle/`` or ``releases/``,
    or one the app's sync never carries (its names compared exactly, as the sync compares
    them)."""
    if faults.on("2.4.exact-exceptions"):  # the fault: the names compared by case folding
        path = path.casefold()
    return path.split("/", 1)[0] in OBJECT_DIRS or bool(exclusion(path))


def spell(files: dict[str, StoredFile], path: str) -> str:
    """The spelling a new file takes: the folders it lands in as already spelled (§2)."""
    folders: dict[str, str] = {}
    for stored in files.values():
        segments = stored.path.split("/")
        for end in range(1, len(segments)):
            prefix = "/".join(segments[:end])
            folders.setdefault(name_key(prefix), prefix)
    segments = path.split("/")
    for end in range(len(segments) - 1, 0, -1):
        known = folders.get(name_key("/".join(segments[:end])))
        if known is not None:
            return "/".join([known, *segments[end:]])
    return path


def clashes(files: dict[str, StoredFile], key: str) -> bool:
    """A file at ``key`` would share its name with a folder, or sit inside a file."""
    if any(other.startswith(key + "/") for other in files):
        return True
    segments = key.split("/")
    return any("/".join(segments[:end]) in files for end in range(1, len(segments)))


def under(files: dict[str, StoredFile], folder_key: str) -> dict[str, StoredFile]:
    """The files under a folder, by their path key relative to it."""
    prefix = folder_key + "/"
    return {key[len(prefix) :]: stored for key, stored in files.items() if key.startswith(prefix)}


def file_map(stored: list[StoredFile]) -> dict[str, dict]:
    """Files by their path, in path order (folded), as an answer gives them."""
    return {
        item.path: copy.deepcopy(item.obj)
        for item in sorted(stored, key=lambda item: name_key(item.path))
    }


# ---- Files --------------------------------------------------------------------------------------


def text_bytes(text: str, what: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise bad_request(f"The {what}'s text is not valid Unicode (a lone surrogate).") from exc


def is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def text_file(text: str, what: str, mtime: str, extra: dict | None = None) -> dict:
    """The File of a text document: its version and size are those of its UTF-8 bytes."""
    data = text_bytes(text, what)
    obj = {key: value for key, value in (extra or {}).items() if key not in FILE_ANSWER_FIELDS}
    obj.update(
        {
            "version": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mtime": mtime,
            "text": text,
        }
    )
    return obj


def check_file(obj: object, what: str, *, text_ok: bool) -> dict:
    """A File as sent (§4.1): its ``version``, ``size`` and ``text`` or ``store_id``, one of the
    two. A text's version and size must be its UTF-8 bytes'. Returns it without ``mtime``."""
    if not isinstance(obj, dict):
        raise bad_request(f"The {what} must be a File object.")
    version = version_hex(obj.get("version"), f"The {what}'s version")
    size = req_int(obj, "size", what)
    text, store_id = obj.get("text"), obj.get("store_id")
    if (text is None) == (store_id is None):
        raise bad_request(f"The {what} holds its text or its store_id, one of the two.")
    if text is not None:
        if not text_ok and not faults.on("2.9.wrong-form-400"):
            raise bad_request(
                f"The {what} is not a text document (data_format.md §2): it is a reference "
                "to a storage, with its store_id."
            )
        if not isinstance(text, str):
            raise bad_request(f"The {what}'s text must be a string.")
        data = text_bytes(text, what)
        wrong = len(data) != size or hashlib.sha256(data).hexdigest() != version
        if wrong and not faults.on("2.9.text-size-version-400"):
            raise bad_request(f"The {what}'s text is not the version and size it states.")
    elif not is_int(store_id):
        raise bad_request(f"The {what}'s store_id must be an integer.")
    return drop_nulls(
        {key: copy.deepcopy(value) for key, value in obj.items() if key not in FILE_ANSWER_FIELDS}
    )


def ref_of(obj: dict) -> tuple[str, int, int]:
    """A stored file's reference, as ``check_refs`` takes it."""
    return obj["version"].removeprefix("sha256:"), obj["size"], obj["store_id"]


def check_form(
    state: State, owner: str, key: str, obj: dict, parts: dict[str, bytes], what: str
) -> None:
    """§2: under the text folders a UTF-8 file travels as text, so a reference there whose bytes
    this server holds (a part, an ``api`` blob) and finds UTF-8 is refused."""
    if obj.get("store_id") is None or not is_text_path(key):
        return
    storage = state.storage_of(obj["store_id"])
    if storage is None or storage["backend"] != "api":
        return
    sha, size, _ = ref_of(obj)
    data = parts.get(sha)
    if data is None:
        data, _status = state.blobs.read(owner, sha, size)
    if data is not None and is_utf8(data):
        raise bad_request(
            f"The {what} is valid UTF-8 under {key.split('/', 1)[0]}/: it travels as text "
            "(data_format.md §2)."
        )


def missing_of(state: State, owner: str, items: list[tuple[str, dict]]) -> dict[str, str]:
    """The ``api``-stored files the server cannot give, by name: ``not_found`` or ``damaged``
    (rule 11; §4.1). A large file counts as one a part would carry."""
    missing: dict[str, str] = {}
    if state.broken("6.2.small-without-part-missing"):
        return missing
    for name, obj in items:
        if obj.get("store_id") != API_STORE_ID:
            continue
        sha, size, _ = ref_of(obj)
        status = state.blobs.check(owner, sha, size)
        if status is not None:
            missing[name] = status
    return missing


def file_parts(
    state: State, owner: str, items: list[tuple[str, dict]], have: set[str], sent: set[str]
) -> list[Part]:
    """The parts of the ``api``-stored files not in ``have`` nor sent already. A blob gone or
    damaged is left out; the JSON still lists its file (rule 11)."""
    parts = []
    for name, obj in items:
        if obj.get("store_id") != API_STORE_ID:
            continue
        sha, size, _ = ref_of(obj)
        if sha in have or sha in sent:
            continue
        data, _status = state.blobs.read(owner, sha, size)
        if data is None:
            continue
        filename = name.rsplit("/", 1)[-1]
        parts.append(Part(sha, filename, part_type(filename), data))
        sent.add(sha)
    return parts
