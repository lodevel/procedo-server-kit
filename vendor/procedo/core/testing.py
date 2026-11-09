"""The sandbox both test suites put around every test — a pytest plugin Bench's and Procedo
Studio's conftests register (``register_plugin``). Never imported by the app.

Nothing a test runs, in process or in a child process it starts (an MCP server, a pack parser,
the ODB reader), may read or write the real per-user folders, the real QSettings (on Windows
the registry) or the old build's data (the migration's source). Three layers:

* per test, ``sandbox`` redirects every per-user seam into the test's ``tmp_path``;
* per process, from session start, the per-user environment and the QSettings point into
  pytest's basetemp, so what outlives a test's redirect (a worker thread still running, a child
  process it started) or runs outside a test still lands in a temp folder;
* an audit hook refuses any write under the real per-user folders, whatever resolved the path,
  and the test it happened in fails (the app often logs and skips a failed write); a refusal
  outside any test fails the session.

And at every test boundary the finished test's Qt debris is disposed of on the main thread
(``_dispose_qt_debris``): the windows a test closed sit in Python reference cycles, and left to
an implicit cyclic GC they are destroyed at an arbitrary later allocation — mid-dispatch in a
later test's event loop, while its worker threads run Python — a use-after-free ("Windows fatal
exception: access violation", a crashed xdist worker), flaky because it depends on GC timing.
So the automatic GC is off from collection to the session's end: every cycle is freed there.
The widgets among that debris are deleted by Qt, never from inside the GC: a parentless widget
torn down by its wrapper's dealloc mid-collection can free a nested layout twice (heap
corruption, ``0xc0000374``).

A probe run outside ``tests/`` gets the same plugin with ``PYTEST_PLUGINS=procedo.core.testing``.
"""

from __future__ import annotations

import errno
import gc
import os
import re
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from . import app_identity, bundle_registry, project_local_state, venv_key, venv_pool

#: The environment the per-user folders (and ``Path.home()``) are built from.
_ENV_NAMES = ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")
#: The real per-user folders, named once by the first process that loads the plugin: an xdist
#: worker and a child process inherit them with an environment that is already redirected.
_GUARDED_ENV = "PROCEDO_TEST_GUARDED_FOLDERS"
#: The audit events that write a path, with the positions of the paths each one writes
#: (``os.replace`` is ``os.rename``; ``Path.touch`` and ``os.open`` are ``open``; on Windows
#: ``shutil.copy2`` copies with ``_winapi.CopyFile2`` alone).
_WRITES = {
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.chmod": (0,),
    "os.rename": (0, 1),
    "os.link": (1,),
    "os.symlink": (1,),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "_winapi.CopyFile2": (1,),
}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
#: Where a worker's refusals outside its tests travel to the xdist controller.
_REFUSED_OUTPUT = "procedo_refused_writes"
_REFUSED_KEY = pytest.StashKey[list[str]]()

_guarded: tuple[str, ...] = ()  # empty: the hook (which cannot be removed) is inert
_gc_was_enabled = False  # the automatic GC's state before collection turned it off
_refused: list[str] = []
_hooked = False
_session_patch: pytest.MonkeyPatch | None = None


def register_plugin(config: pytest.Config) -> None:
    """A conftest's ``pytest_configure`` call: once per process, whoever asks first."""
    if not config.pluginmanager.has_plugin(__name__):
        config.pluginmanager.register(sys.modules[__name__], __name__)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_modals: this test shows real static QMessageBox boxes and closes them itself",
    )
    config.addinivalue_line(
        "markers", "real_os_open: this test may open a file or URL with the OS (none should)"
    )


def isolate_per_user(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point everything per-user — the environment, the QSettings (INI files, never the
    registry), the data folders, the projects' local state — under *root*, until *monkeypatch*
    is undone. Also what a dev script outside pytest (``scripts/render_windows.py``) runs in."""
    _isolate_environment(monkeypatch, root)
    _isolate_settings(monkeypatch, root)
    _isolate_data_dirs(monkeypatch, root)
    _isolate_project_local_state(monkeypatch, root)
    _forget_memoized()


def sandbox(monkeypatch: pytest.MonkeyPatch, root: Path) -> Iterator[None]:
    """Redirect everything per-user under *root* (the test's ``tmp_path``) for one test; a
    fixture runs it with ``yield from``, so the memoized seams are forgotten afterwards."""
    isolate_per_user(monkeypatch, root)
    try:
        yield
    finally:
        _forget_memoized()


@pytest.fixture(autouse=True)
def _per_user_sandbox(monkeypatch, tmp_path):  # a name no test module reuses
    """The per-user folders, QSettings and environment are this test's; a write it (or what
    it left running) made under the real ones fails it."""
    yield from sandbox(monkeypatch, tmp_path)
    refused = take_refused()
    if refused:
        pytest.fail("wrote under the real per-user folders (refused): " + "; ".join(refused))


# ---------------------------------------------------------------------------
# No test ever blocks on a real modal box
# ---------------------------------------------------------------------------

#: The static QMessageBox calls a test must patch when the code it drives shows one.
MODAL_STATICS = ("warning", "information", "critical", "question", "about")
#: How long a dialog's ``exec()`` may stay open before the guard closes it — far longer
#: than a test that closes its own dialog with a timer ever needs.
MODAL_LIMIT_MS = 15_000


def guard_modals(monkeypatch: pytest.MonkeyPatch, opened: list[str]) -> None:
    """A modal box a test did not ask for can never hang it: an unpatched static
    ``QMessageBox`` call raises at once, and a dialog's ``exec()`` still open after
    :data:`MODAL_LIMIT_MS` is closed. Both are recorded in *opened*, which fails the test.
    A test patching a static call, or a dialog's ``exec``, replaces the guard as usual."""
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog, QMessageBox
    except ImportError:  # a suite without Qt has no modal to guard
        return

    def refused(name: str):
        def call(*args, **kwargs):
            said = " / ".join(a for a in args[1:3] if isinstance(a, str))
            opened.append(f"QMessageBox.{name}: {said}")
            raise AssertionError(f"a modal box opened in a test: QMessageBox.{name}: {said}")

        return staticmethod(call)

    for name in MODAL_STATICS:
        monkeypatch.setattr(QMessageBox, name, refused(name))

    def watched(real):
        def exec_(self, *args, **kwargs):
            def still_open() -> None:
                if self.isVisible():
                    text = self.text() if isinstance(self, QMessageBox) else ""
                    opened.append(f"{type(self).__name__} {self.windowTitle()!r} {text}".rstrip())
                    self.reject()

            QTimer.singleShot(MODAL_LIMIT_MS, self, still_open)
            return real(self, *args, **kwargs)

        return exec_

    for cls in (QDialog, QMessageBox):
        monkeypatch.setattr(cls, "exec", watched(cls.exec))


@pytest.fixture(autouse=True)
def _no_real_modal(request, monkeypatch):  # a name no test module reuses
    """A test never blocks on a modal box it did not patch (:func:`guard_modals`)."""
    opened: list[str] = []
    if request.node.get_closest_marker("real_modals") is None:
        guard_modals(monkeypatch, opened)
    yield
    if opened:
        pytest.fail("a modal box opened in a test: " + "; ".join(opened))


def pytest_collection_finish(session: pytest.Session) -> None:
    """Everything alive now (modules, fixtures machinery) lives until exit: frozen, it is out of
    every later collection's working set, so the per-test ``gc.collect()`` walks only the debris.
    No QApplication or widget exists yet. The automatic GC goes off: no cycle is freed at an
    arbitrary allocation mid-test, every one at a test boundary (``_dispose_qt_debris``)."""
    global _gc_was_enabled
    gc.collect()
    gc.freeze()
    _gc_was_enabled = gc.isenabled()
    gc.disable()


# ---------------------------------------------------------------------------
# No test ever opens a file or URL with the OS
# ---------------------------------------------------------------------------

#: The programs that hand a file to the OS's default app, run directly or by a shell's /c, -c.
_OS_OPENERS = frozenset({"start", "xdg-open", "open", "explorer"})
_SHELLS = frozenset({"cmd", "sh", "bash"})
_os_open_allowed = True  # outside a session, and during a ``real_os_open`` test
_os_opened: list[str] = []  # what the guard refused since the last take_os_opened()


def take_os_opened() -> list[str]:
    """What the OS-open guard refused since the last call, and forget it."""
    opened = list(_os_opened)
    del _os_opened[: len(opened)]
    return opened


def _refuse_os_open(how: str, target: object) -> None:
    _os_opened.append(f"{how}: {target}")
    raise AssertionError(f"a test tried to open {target} with the OS ({how})")


def _os_open_target(event: str, args: tuple) -> str | None:
    """What an audited ``os.startfile``, ``os.system`` or launch would open with the OS (its
    program an opener — ``start``, ``xdg-open``, ``open``, ``explorer`` — run directly or by
    ``cmd /c`` / ``sh -c``); None for any other event or command."""
    if event == "os.startfile":
        return os.fsdecode(args[0])
    if event == "os.system":  # the line runs in the shell
        return _opened_by(*_first_word(os.fsdecode(args[0])))
    if event != "subprocess.Popen":
        return None
    command = args[1]
    if isinstance(command, (str, bytes)):
        return _opened_by(*_first_word(os.fsdecode(command)))
    argv = [os.fsdecode(a) for a in command]
    return _opened_by(argv[0], subprocess.list2cmdline(argv[1:])) if argv else None


def _first_word(line: str) -> tuple[str, str]:
    """A command line's first word (a quoted one whole) and the rest of the line."""
    match = re.match(r'\s*("[^"]*"|\S*)(.*)', line, re.DOTALL)
    return match.group(1).strip('"'), match.group(2)


def _opened_by(program: str, rest: str) -> str | None:
    name = re.split(r"[\\/]", program)[-1].lower().removesuffix(".exe")
    if name in _OS_OPENERS:
        return (
            " ".join(w.strip('"') for w in re.findall(r'"[^"]*"|\S+', rest) if w.strip('"')) or name
        )
    flag, inner = _first_word(rest)
    if name not in _SHELLS or flag.lower() not in ("/c", "-c"):
        return None
    inner = inner.strip()
    if len(inner) > 1 and inner[0] == inner[-1] == '"':  # cmd /c "start "" x"
        inner = inner[1:-1]
    return _opened_by(*_first_word(inner))


def guard_os_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """``webbrowser.open`` and ``QDesktopServices.openUrl`` refused (the audit hook refuses
    ``os.startfile`` and opener launches); a test patching one replaces the guard as usual."""
    import webbrowser

    monkeypatch.setattr(
        webbrowser, "open", lambda url, *a, **k: _refuse_os_open("webbrowser.open", url)
    )
    try:
        from PySide6.QtGui import QDesktopServices
    except ImportError:  # a suite without Qt
        return
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        staticmethod(lambda url: _refuse_os_open("QDesktopServices.openUrl", url.toString())),
    )


@pytest.fixture(autouse=True)
def _no_real_os_open(request, monkeypatch):  # a name no test module reuses
    """A test never opens a report, a folder or a page in the owner's apps: an opener it did
    not patch raises at once, and the test fails (the app often logs and skips a failure); one
    refused between tests fails the next test, or the session after its last."""
    global _os_open_allowed
    if request.node.get_closest_marker("real_os_open") is not None:
        _os_open_allowed = True
        try:
            yield
        finally:
            _os_open_allowed = False
        return
    guard_os_open(monkeypatch)
    yield
    opened = take_os_opened()
    if opened:
        pytest.fail("a test tried to open with the OS: " + "; ".join(opened))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    """After the fixtures closed the test's windows — in a ``finally``: a raising finalizer's
    debris is as lethal as a green test's."""
    try:
        return (yield)
    finally:
        _dispose_qt_debris()


def _dispose_qt_debris() -> None:
    """The finished test's cycles freed (the automatic GC is off). With Qt: pending
    ``deleteLater()``s delivered first; the cycles found (kept, not freed), the parentless
    widgets among them deleted by Qt, then the rest freed — all here, on the main thread, no
    event loop running; then the ``deleteLater()``s those destructors queued."""
    qt = sys.modules.get("PySide6.QtCore")
    if qt is None or qt.QCoreApplication.instance() is None:
        gc.collect()
        return
    QCoreApplication, QEvent = qt.QCoreApplication, qt.QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _delete_garbage_widgets()
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _delete_garbage_widgets() -> None:
    """The parentless widgets only cycles still reach, deleted by Qt (their trees with them):
    real widgets alone (the type, not ``__class__``, which a stand-in fakes), parentless by
    Qt's own ``parent`` (a subclass may shadow it), and Python's to delete."""
    if "PySide6.QtWidgets" not in sys.modules:
        return
    import shiboken6
    from PySide6.QtWidgets import QWidget

    debug = gc.get_debug()
    gc.set_debug(debug | gc.DEBUG_SAVEALL)
    try:
        gc.collect()
    finally:
        gc.set_debug(debug)  # before the copy: an allocation must not save more garbage
    garbage = gc.garbage[:]
    gc.garbage.clear()
    for obj in garbage:
        if (
            issubclass(type(obj), QWidget)
            and shiboken6.isValid(obj)
            and shiboken6.ownedByPython(obj)
            and QWidget.parent(obj) is None
        ):
            shiboken6.delete(obj)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Before collection imports anything: the guard on, the environment this process's."""
    global _guarded, _hooked, _session_patch, _os_open_allowed
    _os_open_allowed = False
    folders = os.environ.get(_GUARDED_ENV) or os.pathsep.join(str(f) for f in _real_folders())
    _guarded = tuple(_normalized(f) for f in folders.split(os.pathsep) if f)
    if not _hooked:
        sys.addaudithook(_audit)
        _hooked = True
    _session_patch = pytest.MonkeyPatch()
    _session_patch.setenv(_GUARDED_ENV, folders)
    root = session.config._tmp_path_factory.getbasetemp() / "_per_user"
    _isolate_environment(_session_patch, root)
    _isolate_settings(_session_patch, root)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """A refusal no test teardown reported (during collection, after a worker's last test, at
    exit) fails the run; an xdist worker hands its own to the controller. The heap frozen at
    collection is thawed and the automatic GC back as it was (a long-lived interpreter may run
    another session)."""
    gc.unfreeze()
    if _gc_was_enabled:
        gc.enable()
    refused = take_refused() + take_os_opened()
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_REFUSED_OUTPUT] = refused
        return
    refused += session.config.stash.get(_REFUSED_KEY, [])
    if refused:
        print(
            f"\nPER-USER GUARD: {len(refused)} writes under the real per-user folders or opens "
            f"with the OS refused outside any test: {refused[:20]}"
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    """xdist (controller): collect a finished worker's refusals for the session's verdict."""
    refused = getattr(node, "workeroutput", {}).get(_REFUSED_OUTPUT, [])
    node.config.stash.setdefault(_REFUSED_KEY, []).extend(refused)


def pytest_unconfigure(config: pytest.Config) -> None:
    global _guarded, _session_patch, _os_open_allowed
    _guarded = ()
    _os_open_allowed = True
    if _session_patch is not None:
        _session_patch.undo()
        _session_patch = None


def take_refused() -> list[str]:
    """The writes the guard refused since the last call, and forget them."""
    refused = list(_refused)
    del _refused[: len(refused)]
    return refused


def _real_folders() -> list[Path]:
    """This app's per-user folders, as the environment names them now."""
    return [app_identity.roaming_data_dir(), app_identity.local_data_dir()]


def _normalized(path) -> str:
    return os.path.normcase(os.path.abspath(os.fsdecode(path)))


def _audit(event: str, args: tuple) -> None:
    """Refuse (and note) a write under a guarded folder, or a file opened with the OS, on
    whichever thread it happens."""
    if not _os_open_allowed and (target := _os_open_target(event, args)) is not None:
        _refuse_os_open(event, target)
    if not _guarded:
        return
    if event == "open":
        flags = args[2]  # builtin open's own event says 0; the FileIO event after it says all
        positions = (0,) if isinstance(flags, int) and flags & _WRITE_FLAGS else ()
    else:
        positions = _WRITES.get(event, ())
    for i in positions:
        path = args[i] if i < len(args) else None
        if not isinstance(path, (str, bytes, os.PathLike)):
            continue  # a file descriptor, or no path at all
        text = _normalized(path)
        if any(text == root or text.startswith(root + os.sep) for root in _guarded):
            _refused.append(f"{event} {text} on thread {threading.current_thread().name}")
            # Not PermissionError: tempfile's mkstemp/mkdtemp retry on it up to TMP_MAX times.
            raise OSError(
                errno.EROFS,
                "the test sandbox refuses a write under the real per-user folders",
                text,
            )


def _isolate_environment(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """The per-user roots a child process (or ``Path.home()``) finds are this test's too —
    named, not made: a test that lists its own folder finds only what it wrote there."""
    for name in _ENV_NAMES:
        monkeypatch.setenv(name, str(root / f"_env_{name.lower()}"))


def _isolate_settings(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Every site reaches the QSettings through ``app_identity``: they go to INI files under
    *root* (on Windows a QSettings built from an organization ignores ``setDefaultFormat`` and
    writes the registry, hence the explicit file)."""

    def ini(folder: str):
        def settings(application: str = app_identity.APPLICATION_NAME):
            from PySide6.QtCore import QSettings

            return QSettings(str(root / folder / f"{application}.ini"), QSettings.Format.IniFormat)

        return settings

    monkeypatch.setattr(app_identity, "settings", ini("_qsettings"))


def _isolate_data_dirs(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Every site reaches the data folders through ``app_identity`` (and the bundle store through
    ``bundle_registry``): they go under *root*."""
    for name in ("roaming_data_dir", "local_data_dir"):
        monkeypatch.setattr(app_identity, name, lambda name=name: root / f"_{name}")
    # The bundle store is the checkout's own ``bundle/``: a dialog that lists it made one in the repo.
    store = root / "_bundle_store"
    monkeypatch.setattr(
        bundle_registry, "bundle_store_path", lambda: store.mkdir(exist_ok=True) or store
    )


def _isolate_project_local_state(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """No test may build a venv or a media cache in the real per-user root, nor read — or
    have the pool's limit delete — an environment a bench on this machine runs on. Each seam is
    patched in its own right: the working copies and the environment pool hang BESIDE the local
    root, so patching that root alone would sandbox them by accident of how the paths compose."""
    monkeypatch.setattr(project_local_state, "local_root", lambda: root / "_local_state")
    monkeypatch.setattr(project_local_state, "working_copies_root", lambda: root / "working_copies")
    monkeypatch.setattr(project_local_state, "venvs_root", lambda: root / "_venvs")


def _forget_memoized() -> None:
    """The seams memoize for the life of the PROCESS (a state folder, a bundle's environment
    key, the key cache read off disk, the held environment): one test's answers must not be
    served to the next."""
    project_local_state.forget_resolved()
    venv_key.forget_key_cache()
    venv_pool.hold(None)


# ---------------------------------------------------------------------------
# A test project's evidence, as Bench gives it one
# ---------------------------------------------------------------------------


def evidence_ready(project_root: Path) -> Path:
    """Give a test project what opening it in Bench gives it: a ``project_id``, the
    default evidence entry (``<project>/evidence``, shared layout) and that folder.
    Returns the folder."""
    from . import evidence_config, project_identity

    project_identity.ensure_project_id(Path(project_root))
    evidence_config.ensure_spec(Path(project_root))
    return Path(str(evidence_config.param_value(evidence_config.read_spec(project_root), "root")))


def this_python_stands_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this test's own interpreter stand in for a project's environment: a project with a
    ``bundle/`` folder (:func:`bundle_folder`) answers :data:`sys.executable` as its venv's
    interpreter, one without answers none (it is refused, as in the app). For tests about what
    a run does, not about environments (``test_project_local_state`` and ``test_venv_pool``
    test those for real)."""
    from . import project_model

    monkeypatch.setattr(
        project_model,
        "existing_venv_python",
        lambda root: Path(sys.executable) if (Path(root) / "bundle").is_dir() else None,
    )


def bundle_folder(project_root: Path) -> Path:
    """A test project's ``bundle/`` folder, empty (:func:`this_python_stands_in`). Returns the
    project."""
    (Path(project_root) / "bundle").mkdir(parents=True, exist_ok=True)
    return Path(project_root)


def write_test_run(
    project_root: Path,
    board: str,
    test: str,
    result: dict | None,
    files: dict[str, bytes | str] | None = None,
    *,
    operator: str = "",
) -> str:
    """File one run of ``test`` on the board ``board`` (its key, :mod:`evidence_board`) in the
    project's evidence folder the way a bench files one — staged in this PC's outbox, then
    pushed, its manifest naming the board's serial — and return its run id. ``result`` is its
    ``results.json`` (``None``: a run that produced none). The run is filed under the test's
    hidden id (:func:`id_of_test`)."""
    import json

    from . import evidence_config, evidence_store
    from .evidence_board import board_of
    from .project_identity import read_project_id

    test_id = id_of_test(project_root, test)
    evidence_ready(project_root)
    store = evidence_store.open_project_store(
        evidence_config.read_spec(project_root), project_root=Path(project_root)
    )
    try:
        payload = dict(files or {})
        if result is not None:
            payload["results.json"] = json.dumps(result, indent=2)
        manifest = {"operator": operator, "serial_number": board_of(board).serial}
        info = store.write_run(
            read_project_id(Path(project_root)), board, test_id, manifest, payload
        )
        store.flush_outbox()
        return info.run_id
    finally:
        store.close()


def id_for(test: str) -> str:
    """The hidden id a test project's test named *test* is given here (:func:`id_of_test`): a
    lower-case UUID made from the name, so a test knows it without reading the project."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"procedo-test/{test}"))


def id_of_test(project_root: Path, test: str) -> str:
    """The hidden id of the project's test *test* (``tests/<test>``): the one its ``test.json``
    holds, as Bench reads it; a test holding none is given :func:`id_for` first, its folder and
    its ``test.json`` made when missing, the rest the file holds kept."""
    from . import test_format

    folder = Path(project_root) / "tests" / test
    held = test_format.read_test_id(folder)
    if held:
        return held
    folder.mkdir(parents=True, exist_ok=True)
    data = test_format.read_json(folder) or {}
    filled = any(p.name != test_format.TEST_JSON for p in folder.iterdir())
    data.setdefault("active", test_format.read_format(folder)[0] if filled else test_format.TEXT)
    test_format.write_json(folder, {**data, test_format.TEST_ID: id_for(test)})
    return id_for(test)


def run_record(verdict: str = "PASS", **procedo: object) -> dict:
    """A minimal run record (``results.json``) whose Bench verdict is ``verdict``; ``procedo``
    adds to its ``metadata.procedo``."""
    outcome = {"PARTIAL": "FAIL", "SKIP": "PASS"}.get(verdict, verdict)
    return {
        "outcome": outcome,
        "phases": [],
        "metadata": {"procedo": {"verdict": verdict, **procedo}},
    }


def saved_record(
    verdict: str = "PASS",
    serial: str = "SN1",
    station: str = socket.gethostname(),
    operator: str = "alice",
    **procedo,
) -> dict:
    """:func:`run_record` as a commit leaves it (``evidence_store_fs_shared._local_manifest``): its
    serial, station (a PC with no station name: the computer's name), operator and this app's
    version added — a record that already holds them is
    committed byte for byte."""
    from .run_provenance import app_version

    known = {"app_version": app_version(), "operator": operator, **procedo}
    record = run_record(verdict, **{k: v for k, v in known.items() if v})
    return {**record, **{k: v for k, v in (("dut_id", serial), ("station_id", station)) if v}}


def old_layout_tree(root: Path) -> Path:
    """A project folder holding results in the OLD layout (``evidence_legacy``): a run with
    an archive, a report and an export. Bench leaves these files alone."""
    from .evidence_legacy import trees

    serials, exports = trees(root)
    files = {
        serials / "SN1" / "results" / "t1" / "results.json": '{"overall": "PASS"}',
        serials
        / "SN1"
        / "results"
        / "t1"
        / "prev_2026-01-01_010101"
        / "results.json": '{"overall": "FAIL"}',
        serials / "SN1" / "reports" / "r.html": "<html/>",
        exports / "summary.md": "# summary",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
