"""What produced a run — Qt-free, one seam for both the manifest writer (at
publish time, which also adds it to the run's ``results.json``) and every reader that asks "is this still the version that ran?"
(the history row, the report warning).

A test's *definition* is ``tests/<test>/procedure.json`` + ``test.py``, hashed
together in that order — a code test's, every file of its ``openhtf/`` folder and the
``functions/`` files they import (:func:`code_files`); the project's *bundle* is whatever
:func:`project_bundle.read_active_bundle` resolves. Neither needs the evidence
store — both are pure functions of the project on disk.

A runner reads them ONCE, at the test's start (:func:`read_start`, :func:`read_code_start`): a
copy of the bytes it read is what runs — its ``test.py`` with this PC's bench values written in
(``bench_values.effective_source``: the addresses and remote/manual of THIS bench) — and the
provenance it records is taken from those same bytes and the bundle the run runs on — whatever
the share holds by the time the run is saved (another PC may regenerate the test or swap the
bundle meanwhile). The definition's hash is the project's bytes: a bench's own address never
makes a run "edited"; the run records the bench values it ran with beside it. The test's release
state (``test_release.state_of``) is read from those same bytes: a release records its number; a
draft the number of the release it comes from, as edited (``State.run_fields``). A run whose
documents no release file holds (a draft) keeps them: ``definition/<name>`` among its files
(:func:`keep_documents`), each with the role ``definition`` in its manifest — inside its record
for a project that keeps a run in one file; a run of a release keeps nothing more, its number and
fingerprint point to the release. Which documents a run reads — the test's files, or a release's
for a run told to run one — is ``run_choice.to_run``'s answer, handed in as *chosen*: a release's
documents run from the start copy as the test's files would.

A run records its test's hidden id (``test_id``, :func:`test_id_of`) beside its name, read from
the test's ``test.json`` at the run's start like the rest, never kept from the project's open.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import logging
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    atomic_write,
    bench_values,
    config_manager,
    paths,
    project_bundle,
    run_record,
    test_format,
)
from .evidence_store import FUNCTIONS, RESULT_FILE
from .paths import plain_file

if TYPE_CHECKING:
    from .run_choice import ToRun

log = logging.getLogger(__name__)

_APP_DIST_NAME = "procedo-bench"


#: The name the copy of a test's ``test.py`` that runs has in its run folder.
SCRIPT_COPY = "test.py"
#: The files a run's save writes itself: never one of its attachments, never kept over.
SAVE_OWN = frozenset({RESULT_FILE, "interactive_log.jsonl"})
#: The folder of a run that holds the documents its draft ran with, and their role in its
#: manifest.
DEFINITION = "definition"
#: Why Bench does not run a test that has no id (:func:`test_id_of`).
NO_TEST_ID = (
    "This test can't be run: its test.json can't be read. Open it in Studio and save it again."
)


@dataclass(frozen=True)
class BundleUsed:
    """The bundle a run runs on, read once: its ref and its ``python_packages``."""

    ref: dict[str, str] | None  # {"id", "version"}, or None with no active bundle
    python_packages: tuple = ()  # bundle.json's entries as they are


@dataclass(frozen=True)
class RunProvenance:
    test: str
    bundle: dict[str, str] | None  # {"id", "version"}, or None with no active bundle
    definition_sha256: str  # "" when the test has no procedure.json or test.py
    app_version: str  # "" when the package carries no installed metadata
    python_packages: tuple = ()  # the bundle's, recorded with the run's functions
    attachments: str = run_record.SEPARATE  # the project's, how the run keeps its files
    #: The bench values the run ran with, ``{constant: value}`` (``bench_values.used``): which
    #: address each instrument was measured on, remote or manual.
    bench_values: dict = field(default_factory=dict)
    #: The test's release the run ran (``test_version``), or the one its draft comes from;
    #: None: a draft of a test never released.
    test_version: int | None = None
    #: The run ran a draft: the files are not release ``test_version``'s.
    test_version_edited: bool = False
    #: The documents a run keeps with it (``test_release.documents`` as read at its start), by
    #: their names: a draft's; ``{}`` when a release file holds them (``State.held``).
    documents: dict = field(default_factory=dict, repr=False)
    #: The test's hidden id (``test_format.TEST_ID``) as its ``test.json`` held it when the run
    #: was read; "" when it held none.
    test_id: str = ""


@dataclass(frozen=True)
class RunStart:
    """What a test runs, read once at its start: its files' bytes, by their names in the run folder
    (``test.py``; a code test's ``openhtf/…`` and ``functions/…``), the one that runs (``entry``),
    and the provenance taken from them."""

    files: dict[str, bytes]
    entry: str
    provenance: RunProvenance

    @property
    def source(self) -> str:
        return self.files[self.entry].decode("utf-8")


def text_digest(procedure: bytes, script: bytes) -> str:
    """sha256 of a text test's definition: its ``procedure.json`` then its ``test.py``."""
    h = hashlib.sha256()
    h.update(procedure)
    h.update(script)
    return h.hexdigest()


def code_digest(files: dict[str, bytes]) -> str:
    """sha256 over a code test's files, each its name and its bytes, by name."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(f"{name}\0{len(files[name])}\0".encode())
        h.update(files[name])
    return h.hexdigest()


def code_files(project_root: Path, test: str) -> dict[str, Path]:
    """A code test's files by their names in its run folder: every file of ``tests/<test>/openhtf/``
    (``openhtf/test.py``, ``openhtf/helpers.py``…), then each ``functions/`` file they import, also
    through another one (``functions/power_phases.py``), read with ``ast`` and never run; never a
    link, a dot-name or ``__pycache__``. ``{}`` without ``openhtf/test.py``."""
    root = Path(project_root)
    code = root / "tests" / test / test_format.CODE_DIR
    if not (code / "test.py").is_file():
        return {}
    found = {f"{test_format.CODE_DIR}/{rel}": path for rel, path in tree_files(code)}
    return {**found, **functions_imported(root, list(found.values()))}


def functions_imported(project_root: Path, sources: list[Path]) -> dict[str, Path]:
    """The ``functions/`` files the Python files of *sources* import, also through another one,
    by their names in a run folder (``functions/power_phases.py``); read with ``ast``, never
    run."""
    functions = Path(project_root) / FUNCTIONS
    found: dict[str, Path] = {}
    todo = [path for path in sources if path.suffix == ".py"]
    while todo:
        for path in _imported(todo.pop(), functions):
            name = f"{FUNCTIONS}/{path.relative_to(functions).as_posix()}"
            if name not in found:
                found[name] = path
                todo.append(path)
    return found


def tree_files(folder: Path) -> list[tuple[str, Path]]:
    """Every file under *folder* by its path under it, never through a link, a dot-name or
    ``__pycache__``."""
    out = []
    for at, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if _kept(Path(at, d))]
        out += [
            (Path(at, n).relative_to(folder).as_posix(), Path(at, n))
            for n in names
            if _kept(Path(at, n))
        ]
    return sorted(out)


def _kept(path: Path) -> bool:
    return not (path.name.startswith(".") or path.name == "__pycache__" or paths.is_link(path))


def _imported(source: Path, functions: Path) -> list[Path]:
    """The ``functions/`` files *source* imports (``import power_phases``, ``from dsp.filters import
    x``: ``dsp/__init__.py`` too); none when it does not parse (its run says why it cannot load)."""
    try:
        tree = ast.parse(source.read_bytes())
    except (SyntaxError, ValueError):
        return []
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules += [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    out = []
    for module in modules:
        parts = module.split(".")
        for k in range(1, len(parts) + 1):
            base = functions.joinpath(*parts[:k])
            out += [
                p
                for p in (base.with_suffix(".py"), base / "__init__.py")
                if p.is_file() and _kept(p)
            ]
    return out


def definition_sha256(project_root: Path, test: str) -> str:
    """sha256 over ``tests/<test>/procedure.json`` + ``test.py`` bytes, in that
    order — a code test's over its :func:`code_files`; ``""`` when a file is missing
    (a code test's ``openhtf/test.py``) — nothing to hash, not an error."""
    test_dir = project_root / "tests" / test
    if test_format.active_format(test_dir) == test_format.CODE:
        files = code_files(project_root, test)
        return (
            code_digest({name: path.read_bytes() for name, path in files.items()}) if files else ""
        )
    proc, script = test_dir / "procedure.json", test_dir / "test.py"
    if not proc.is_file() or not script.is_file():
        return ""
    return text_digest(proc.read_bytes(), script.read_bytes())


def bundle_used(bundle_dir: Path) -> BundleUsed:
    """The bundle in *bundle_dir* (a project's ``bundle/``) as it is now. An unreadable folder
    raises OSError."""
    bundle = project_bundle.read_bundle_at(Path(bundle_dir))
    if bundle is None:
        return BundleUsed(None)
    packages = bundle.manifest.get("python_packages")
    return BundleUsed(
        {"id": bundle.id, "version": bundle.version},
        tuple(packages) if isinstance(packages, list) else (),
    )


def read_start(
    project_root: Path, test: str, script: Path, bundle: BundleUsed, chosen: ToRun | None = None
) -> RunStart:
    """``script`` (the test's ``test.py``) and ``procedure.json``, each read once, now, with
    the provenance of a run of them on *bundle* and how the project keeps a run's files: the
    bytes its documents were read with (:func:`_documents_now`). *chosen* (``run_choice``): the
    documents to run, read already — a release's run instead of the test's files, its
    ``functions/`` files with it. An unreadable file raises OSError."""
    docs, version, kept = _documents_now(project_root, test, chosen)
    folder = Path(project_root) / "tests" / test
    functions: dict[str, bytes] = {}
    if chosen is not None and chosen.release is not None:  # nothing of the test's folder runs
        if "test.py" not in docs:
            raise FileNotFoundError(f"release v{chosen.release} of {test} holds no test.py")
        data, proc = docs["test.py"], docs.get("procedure.json")
        functions = {n: b for n, b in docs.items() if n.split("/", 1)[0] == FUNCTIONS}
    else:
        data = _read_once(docs, "test.py", Path(script), folder)
        proc = _read_once(docs, "procedure.json", folder / "procedure.json", folder)
    definition = text_digest(proc, data) if proc is not None else ""
    runs, bench = _bench_copy(Path(script), data)
    return RunStart(
        {SCRIPT_COPY: runs, **functions},
        SCRIPT_COPY,
        _provenance(project_root, test, bundle, definition, bench, version, kept),
    )


def test_id_of(project_root: Path, test: str) -> str:
    """The hidden id of *test* (``tests/<test>``), read from its ``test.json`` now; "" when it
    holds none (``test_format.read_test``)."""
    return test_format.read_test(Path(project_root) / "tests" / test)[2]


def _read_once(docs: dict[str, bytes], name: str, path: Path, folder: Path) -> bytes | None:
    """*path*'s bytes: the documents' *name* when it is that file of the test's *folder*, else
    read now (None when it is not there and the documents have none)."""
    if name in docs and path == folder / name:
        return docs[name]
    return path.read_bytes() if name == "test.py" or path.is_file() else None


def read_code_start(
    project_root: Path, test: str, bundle: BundleUsed, chosen: ToRun | None = None
) -> RunStart:
    """A code test's :func:`code_files`, each read once, now, with the provenance of a run of them
    on *bundle*; *chosen* (``run_choice``): its files read already — a release's, for a run of
    one. An unreadable file, or no ``openhtf/test.py``, raises OSError."""
    from . import test_release  # it imports this module

    if chosen is not None:
        files = dict(chosen.documents)
    else:
        files = {name: path.read_bytes() for name, path in code_files(project_root, test).items()}
    if test_format.CODE_SCRIPT not in files:
        raise FileNotFoundError(f"tests/{test}/{test_format.CODE_SCRIPT} is missing")
    # A code test's documents are its files: its state is read from the bytes that run.
    now = test_release.state_of(Path(project_root), test, test_format.CODE, files)
    kept = {} if now.held else dict(files)
    definition = code_digest(files)
    script = Path(project_root, "tests", test, test_format.CODE_SCRIPT)
    files[test_format.CODE_SCRIPT], bench = _bench_copy(script, files[test_format.CODE_SCRIPT])
    return RunStart(
        files,
        test_format.CODE_SCRIPT,
        _provenance(project_root, test, bundle, definition, bench, now.run_fields, kept),
    )


def _bench_copy(script: Path, data: bytes) -> tuple[bytes, dict]:
    """The bytes of the test's script that run on this PC — *data* (the project's) with this
    PC's bench values written in — and the bench values they hold. A script that is no UTF-8
    text has none any reader sees: it runs as it is."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, {}
    runs = bench_values.effective_source(script, text)
    return (data if runs == text else runs.encode("utf-8")), bench_values.used(runs)


def _provenance(
    project_root: Path,
    test: str,
    bundle: BundleUsed,
    definition: str,
    bench: dict,
    version: tuple[int | None, bool],
    kept: dict[str, bytes],
) -> RunProvenance:
    """A run of *test* starting now on *bundle*: its definition's hash, how the project keeps
    a run's files, the bench values it runs with, the test's release *version* read with them
    (``(test_version, test_version_edited)``), the documents it keeps (*kept*) and the test's
    hidden id (:func:`test_id_of`)."""
    return RunProvenance(
        test,
        bundle.ref,
        definition,
        app_version(),
        bundle.python_packages,
        config_manager.read_attachments(project_root / "config"),
        bench,
        *version,
        kept,
        test_id_of(project_root, test),
    )


def _documents_now(
    project_root: Path, test: str, chosen: ToRun | None = None
) -> tuple[dict[str, bytes], tuple[int | None, bool], dict[str, bytes]]:
    """*test*'s documents read once, now (``test_release.documents``; *chosen*'s, read already),
    the release fields a run of them records (``(test_version, test_version_edited)``,
    ``State.run_fields``) and the documents it keeps: every one for a draft, none when a release
    file holds them. An unreadable file raises OSError."""
    from . import test_release  # it imports this module

    if chosen is not None:
        fmt, docs = chosen.fmt, chosen.documents
    else:
        fmt, docs = test_release.documents(Path(project_root), test)
    now = test_release.state_of(Path(project_root), test, fmt, docs)
    return docs, now.run_fields, ({} if now.held else docs)


def keep_documents(run_dir: Path, prov: RunProvenance) -> None:
    """The documents *prov* keeps, written into the run as ``definition/<name>`` before it is
    hashed: what a run of a draft ran with, since drafts are not kept. A project that keeps a
    run in one file keeps them inside its record instead (:func:`results_fields`). One that
    cannot be written is logged: the run is saved without it, never lost for it."""
    if prov.attachments == run_record.SINGLE:
        return
    for name, data in prov.documents.items():
        path = Path(run_dir, DEFINITION, *name.split("/"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            log.warning("%s of %s is not kept with its run: %s", name, prov.test, exc)


def role(name: str) -> str:
    """The role a run's file *name* has in its manifest: ``definition`` for the documents its
    draft ran with (:func:`keep_documents`); ``""`` for any other."""
    return DEFINITION if name.split("/", 1)[0] == DEFINITION else ""


def write_copy(run_dir: Path, start: RunStart) -> Path:
    """The copy of *start*'s files that runs, in the run's own folder; the one that runs."""
    for name, data in start.files.items():
        path = Path(run_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return Path(run_dir, start.entry)


def remove_copy(run_dir: Path, start: RunStart) -> None:
    """Take the copy of *start*'s test files out of a run before it is saved, the folders it
    made with them once empty: the evidence holds their hash. The ``functions/`` files stay, as
    a text test's functions snapshot does (the run records each one's hash)."""
    folders = set()
    for name in start.files:
        if name.split("/")[0] != FUNCTIONS:
            Path(run_dir, name).unlink(missing_ok=True)
            folders.update(Path(run_dir, name).parents)
    for folder in sorted(
        folders - set(Path(run_dir).parents) - {Path(run_dir)}, key=lambda f: -len(f.parts)
    ):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def app_version() -> str:
    """This package's installed version, or ``""`` without distribution metadata
    (an unpackaged checkout)."""
    try:
        return importlib.metadata.version(_APP_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return ""


def for_run(project_root: Path | None, test: str, chosen: ToRun | None = None) -> RunProvenance:
    """What a run of ``test`` is produced by, right now — for a run saved with no
    :func:`read_start` behind it (a guided run's save); *chosen* (``run_choice``): the documents
    it runs, read already (a release's for a run of one). ``project_root=None`` (a store with no
    project, e.g. today's FTPS backend) carries no bundle, no definition hash and no test id;
    the app version is independent of the project and is always attempted.

    Never fails the commit it is part of: a project folder that cannot be read at
    this moment (a share that flickers) leaves the bundle and the definition hash
    out, as for no project, and says so in the log."""
    bundle, definition, attachments = BundleUsed(None), "", run_record.SEPARATE
    bench: dict = {}
    version: tuple[int | None, bool] = (None, False)
    kept: dict[str, bytes] = {}
    test_id = ""
    if project_root:
        test_id = test_id_of(project_root, test)
        try:
            bundle = bundle_used(project_bundle.project_bundle_path(project_root))
            _docs, version, kept = _documents_now(project_root, test, chosen)
            definition = (
                _fingerprint(chosen)
                if chosen is not None
                else definition_sha256(project_root, test)
            )
            script = test_format.script(Path(project_root) / "tests" / test)
            if script.is_file():
                bench = _bench_copy(script, script.read_bytes())[1]
        except OSError as exc:
            log.warning(
                "Provenance of %s not recorded, the project folder did not answer: %s", test, exc
            )
            bundle, definition, bench, version, kept = BundleUsed(None), "", {}, (None, False), {}
        attachments = config_manager.read_attachments(Path(project_root) / "config")
    return RunProvenance(
        test,
        bundle.ref,
        definition,
        app_version(),
        bundle.python_packages,
        attachments,
        bench,
        *version,
        kept,
        test_id,
    )


def _fingerprint(chosen: ToRun) -> str:
    """What *chosen* runs, hashed as :func:`definition_sha256` hashes the test's files."""
    from . import test_release  # it imports this module

    return test_release.fingerprint_of(chosen.fmt, chosen.documents)


def results_fields(prov: RunProvenance, operator: str = "") -> dict:
    """What the run's ``results.json`` keeps of ``prov`` (its ``metadata.procedo``): the test's
    hidden id (``test_id``), ``bundle`` (``{"id", "version"}``), ``definition_sha256``, the
    ``test_version`` and
    ``test_version_edited`` (true only), ``app_version``, the ``operator`` and the
    ``bench_values`` it ran with (``{constant: value}``), each only when known — the manifest's
    values, so the result file alone says what produced it, and on which addresses. A project
    that keeps a run in one file also keeps there the documents a draft ran with
    (``definition``: ``{name: text}``, ``{"base64": …}`` for one that is no UTF-8 text)."""
    from . import test_release  # it imports this module

    inside = prov.documents if prov.attachments == run_record.SINGLE else {}
    fields = (
        ("test_id", prov.test_id),
        ("bundle", prov.bundle),
        ("definition_sha256", prov.definition_sha256),
        ("test_version", prov.test_version),
        ("test_version_edited", prov.test_version_edited),
        ("app_version", prov.app_version),
        ("operator", operator),
        ("bench_values", prov.bench_values),
        (DEFINITION, {name: test_release.encoded(data) for name, data in inside.items()}),
    )
    return {k: v for k, v in fields if v}


#: What produced a run, as its manifest and its record's ``metadata.procedo`` both keep it.
MADE = ("test_id", "bundle", "definition_sha256", "test_version", "test_version_edited")
#: A run's version that is not the test's version now, and one whose bytes are not its number's.
OLDER = "older version"
EDITED = "edited"


def made(manifest: dict | None, record: object) -> dict:
    """The :data:`MADE` fields of a run, its record's (``metadata.procedo``) over its manifest's:
    an older run recorded them in its manifest alone."""
    found = {k: v for k, v in (manifest or {}).items() if k in MADE}
    if isinstance(record, dict):
        found.update((k, v) for k, v in run_record.procedo(record).items() if k in MADE)
    return found


def _number(fields: dict) -> int | None:
    number = fields.get("test_version")
    return number if type(number) is int and number >= 1 else None


def version_flags(fields: dict, current: int | None) -> list[str]:
    """Why a run's version needs a word: :data:`EDITED` (the bytes that ran are not that
    release's: a draft from it, or a hand edit before releases), :data:`OLDER` (the test is at a
    later number *current* now)."""
    number = _number(fields)
    if number is None:
        return []
    flags = [EDITED] if fields.get("test_version_edited") else []
    if current is not None and number < current:
        flags.append(OLDER)
    return flags


def report_fields(result: dict | None, current: int | None = None) -> dict[str, str]:
    """What a run's record (``metadata.procedo``) recorded, as the report template's per-test
    keys (``bundle_name``, ``bundle_version``, ``procedure_fingerprint`` — the first 8
    characters of ``procedure_fingerprint_full``, the definition's sha256 —, ``test_version``,
    the test's version number, and ``test_version_note``, ``older version`` and/or ``edited``
    with *current* the test's number now); each ``""`` for a run that recorded none of it
    (OpenHTF's own JSON, a test with no result)."""
    found = run_record.procedo(result or {})
    bundle = found.get("bundle") or {}
    digest = found.get("definition_sha256") or ""
    return {
        "bundle_name": bundle.get("id", ""),
        "bundle_version": bundle.get("version", ""),
        "procedure_fingerprint": digest[:8],
        "procedure_fingerprint_full": digest,
        "test_version": str(found.get("test_version") or ""),
        "test_version_note": ", ".join(version_flags(found, current)),
    }


def record_in_results(
    run_dir: Path,
    what: str,
    fields: Callable[[], dict],
    header: dict | None = None,
    inline: Collection[str] = (),
) -> None:
    """Adds ``fields()`` to the ``metadata.procedo`` of ``<run_dir>/results.json`` and ``header``
    (OpenHTF's own run keys: ``dut_id``, ``station_id``) to the record, and puts each of its
    attachments whose file is one of ``inline`` (the run's files, by their ``/`` path in it)
    inside it (``run_record.inlined``), that file then removed — the one rewrite of a result
    file after its run. Nothing without that file or with nothing to add; a results file (or a
    field, or a file to put inside) that cannot be read or written is logged as ``what`` not
    recorded and left as it is, its files with it."""
    run_dir = Path(run_dir)
    results = run_dir / RESULT_FILE
    if not results.is_file():
        return
    embedded: list[str] = []
    try:
        added, header = fields(), {k: v for k, v in (header or {}).items() if v}
        if added or header or inline:
            data = json.loads(results.read_text(encoding="utf-8"))
            record = {**(run_record.with_procedo(data, added) if added else data), **header}
            if inline:
                record, embedded = run_record.inlined(
                    record, lambda f: _run_file(run_dir, f) if f in inline else None
                )
            if record != data:  # a record that holds them already is left byte for byte
                atomic_write.write_json(results, record)
    except (OSError, ValueError, TypeError, AttributeError) as exc:  # results.json holds no object
        log.warning("%s not recorded in %s: %s", what, results, exc)
        return
    for name in embedded:  # inside the record now: never kept twice
        path = plain_file(run_dir, name)  # proved again right before the removal
        try:
            if path is not None:
                path.unlink(missing_ok=True)
            elif os.path.lexists(run_dir / name):
                raise OSError("it is no longer a plain file of the run")
        except OSError as exc:
            log.warning("%s is inside %s and stays beside it too: %s", name, results, exc)


def _run_file(run_dir: Path, name: str) -> bytes | None:
    """The bytes of the run's own file *name* to put inside its record; None (it stays beside
    the record, logged) for one that is no plain file of the run or cannot be read."""
    path = plain_file(run_dir, name)
    try:
        if path is None:
            raise OSError("no plain file of the run (a link, or outside it)")
        return path.read_bytes()
    except (OSError, MemoryError) as exc:
        log.warning("%s of %s stays beside its record: %s", name, run_dir, exc)
        return None


def definition_changed(recorded_sha256: str, project_root: Path, test: str) -> bool:
    """True iff a run recorded with ``recorded_sha256`` no longer matches the
    CURRENT definition of ``test``. False when there is nothing to compare: no
    recorded hash (an older run), or no current definition (the test's files are
    gone) — a reader never warns from a hash it cannot see both sides of."""
    if not recorded_sha256:
        return False
    current = definition_sha256(project_root, test)
    return bool(current) and current != recorded_sha256
