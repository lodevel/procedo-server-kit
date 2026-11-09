"""Where a project's MACHINE-LOCAL state lives — the one seam that answers it.

A project folder is about to be a network share opened in place by several
benches, so nothing belonging to *this machine* may live inside it. Two things
did: the virtual environment (``<project>/.venv``) and the media cache
(``<project>/.media_cache``). On SMB a venv is slow, lock-unreliable and bakes
the share path into its own scripts — and two benches would be sharing one.

The venv then left the per-project folder too. It holds exactly what a bundle
installs and nothing of the project, so projects on one bundle were keeping
identical copies of the same 300 MB tree: it lives in a shared, bounded pool
keyed by bundle CONTENT instead (:mod:`venv_pool`, which owns everything inside a
pool folder). This module still answers *where* — ``venv_dir``, ``venv_python``,
``existing_venv_python`` and ``install_record`` are unchanged to every caller —
and still owns the app-data roots both pools hang under.

The media cache lives under the per-user application-data root, in one folder per
project named ``<readable name>__<id8>``. This folder is APP-MANAGED and never
user-chosen — nobody browses it, and venv trees are deep enough to risk the
260-character path limit — so it keeps that name although the evidence store gave
its own up in 2026-09-19: an evidence folder is the path the user typed, and the
marker inside it says whose it is. The key is the ``project_id`` from
``config/config.json``, never the path — a project that is moved, or opened
through another drive mapping or UNC spelling, keeps the venv it already built.
Eight hex characters are a *label*, not the key: the folder carries a marker
holding the id in full, and a folder whose marker says otherwise is somebody
else's (:func:`state_dir`).

**A project with no ``project_id`` has no local state.** The id is minted on first
open and is absent only when ``config/config.json`` could not be written at all.
The alternative — keying on a hash of the path — is the very bug this module
exists to remove: it would give one project two caches under two spellings, and
orphan both the moment an id appeared. The evidence store already refuses an empty
``project_id``, so such a project is storeless already; a second, weaker key would
make one failure look like two. So these functions answer ``None`` and the callers
say why (:data:`NO_PROJECT_ID`). An app-data root that cannot be listed or created
answers ``None`` the same way and logs.

The *venv* is no longer among them: it is keyed by the bundle, not by the project,
so it needs no identity at all. A project with no bundle has no environment of its
own and says so in :data:`venv_pool.NO_BUNDLE`.

The answer is memoized per project root for the life of the process. It is not
only a saving — resolving it costs a ``config/config.json`` read ON THE SHARE,
and ``project_identity`` reads an unreadable file as "no id", so without the
memo a cache sitting on the local disk would disappear mid-session on a dropped
SMB connection. Only a resolved answer is kept, so a project whose id is minted a
moment later is not remembered as having none.

There is no compatibility path: an in-project ``.venv`` is not read and not
migrated. The per-project ``.venv`` this layout itself used before the pool is not
read either — the start-up sweep simply deletes it (:func:`remove_legacy_venvs`),
because it is app-owned, re-creatable, and the disk it occupies is the point.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

from . import app_identity, evidence_store_fs
from .app_settings import get_app_settings_file
from .paths import is_link
from .project_identity import read_project_id

logger = logging.getLogger(__name__)

_VENV_DIR_NAME = ".venv"
_MEDIA_CACHE_DIR_NAME = ".media_cache"
_INSTALL_RECORD_NAME = ".bundle_installed.json"
#: Where the sync keeps a file it removed from the working copy (:func:`local_trash_dir`).
_TRASH_DIR_NAME = "trash"
#: This PC's copy of a project's evidence index shards on a server (:mod:`evidence_shard_cache`).
_INDEX_CACHE_DIR_NAME = "evidence_index"
#: THIS MACHINE's persistent UNSAFE latch (:func:`bench_unsafe_file`) — one file,
#: not one per project: the bench's instruments are unsafe whatever is open.
_BENCH_UNSAFE_NAME = "bench_unsafe.json"
#: One bench's own working state that used to live in the project folder and travel
#: with it (:func:`bench_state_file`): the preflight's last answers, the
#: last-seen instrument identities and the tests' instrument addresses and remote/manual
#: (``bench_values``), all of which describe the hardware on THIS PC; the board details
#: last typed in its header (``run_fields``), the board on THIS bench; and the tests this PC
#: last exported (``export_choice``).
_BENCH_STATE_NAMES = frozenset(
    {
        "bench_preflight.json",
        "bench_fingerprints.json",
        "bench_values.json",
        "run_fields.json",
        "export_choice.json",
    }
)

#: Written into a state folder when it is created, holding the project_id IN
#: FULL. The folder name carries only the first eight characters, and two ids
#: can share those — a suffix match is a candidate, this marker is the proof.
_MARKER_NAME = ".project_id"

#: What Windows refuses in a path component. Kept here, at the foot of the import
#: graph: ``media_panel``, ``venv_manager`` and ``report_export`` reach this module,
#: and pulling the evidence stack in behind it would be the wrong way round —
#: :func:`readable_name` is what the Evidence tab imports instead.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Why a project can have no local state, in the words a user gets to read.
NO_PROJECT_ID = (
    "This project has no project_id, which means its config/config.json could not be "
    "written (a read-only folder or share). Machine-local state — the virtual "
    "environment and the media cache — is keyed by that id and has nowhere to live "
    "without it. Make the project folder writable and open it again."
)

#: What a project folder may hold that belongs to THIS machine and must never
#: travel with the project — the two folders slice 1 moved out, the record that
#: describes the local venv, and the marker a state folder carries. Named here
#: because the names are this module's, and because the guard in
#: ``tests/test_project_local_state.py`` rightly refuses them anywhere else in
#: ``src/``. Exposed as a PREDICATE: a caller can ask whether an entry is machine
#: state and still cannot compose an in-project path from the answer. The names
#: themselves go to one reader only, the sync's exclusion list
#: (``project_never_travels.EXCLUSIONS``), so a check outside the app (the server
#: conformance kit) can enumerate what never travels (:func:`machine_state_names`).
_MACHINE_STATE = frozenset(
    {_VENV_DIR_NAME, _MEDIA_CACHE_DIR_NAME, _INSTALL_RECORD_NAME, _MARKER_NAME}
)

#: Resolved state folders, keyed by the project root as this process spelled it.
_RESOLVED: dict[str, Path] = {}
#: Moves on every :func:`forget_resolved`: what was remembered about a project's bundle
#: before it may be stale (:func:`resolution_generation`).
_generation = 0


def is_machine_state(name: str) -> bool:
    """Whether an entry of that name inside a project folder belongs to this machine.

    The project sync asks it: a working copy pushed to a server must carry the
    project's definition and nothing of the bench that holds it.
    """
    return name in _MACHINE_STATE


def machine_state_names() -> frozenset[str]:
    """The names :func:`is_machine_state` takes, for the exclusion list's examples
    (``project_never_travels.EXCLUSIONS``) and nothing else: never to compose a path."""
    return _MACHINE_STATE


def resolution_generation() -> int:
    """How many times :func:`forget_resolved` ran: a value derived from a project's bundle and
    remembered under an older generation is asked again (``display_snapshot``'s bundle key)."""
    return _generation


def forget_resolved(project_path: Path | str | None = None) -> None:
    """Drop what this process remembers about *project_path* (everything when ``None``).

    The state folder is found by an id that never changes once minted, so within a
    run its answer cannot move and only a test redirecting :func:`local_root` needs
    this. The venv is a different matter — it follows the project's BUNDLE, which a
    bundle import replaces mid-session — so the pool's memo is dropped here too and
    every path that swaps a bundle calls this with the project it swapped.
    """
    from . import venv_key

    global _generation
    _generation += 1
    if project_path is None:
        _RESOLVED.clear()
    else:
        _RESOLVED.pop(os.path.normcase(os.path.abspath(Path(project_path))), None)
    venv_key.forget_resolved(project_path)


def local_root() -> Path:
    """The per-user, per-machine folder every project's local state hangs under (beside
    the outbox: both under ``app_identity.local_data_dir``)."""
    return app_identity.local_data_dir() / "projects"


def readable_name(name: str) -> str:
    """The name as typed, minus what Windows refuses — one usable path component.

    Public because it is not only this module's: the Evidence tab pre-fills a folder
    named after the project under the machine-level default root, and a project folder
    whose name Windows refuses would otherwise be offered as a path nothing can
    create. One rule, so the suggestion and the local-state folder agree.
    """
    return _ILLEGAL.sub("_", unicodedata.normalize("NFC", name)).rstrip(". ") or "project"


def namespace(project_path: Path | str, project_id: str) -> str:
    """``<readable name>__<id8>`` for this project's MACHINE-LOCAL folder — no I/O.

    Both halves go through :func:`readable_name`, so the result is one path component
    whatever a hand-edited ``config.json`` put in the id.
    """
    return f"{readable_name(Path(project_path).name)}__{readable_name(project_id[:8])}"


def _marker_id(folder: Path) -> str:
    """The project_id a state folder claims, or ``""`` when it claims none."""
    try:
        return (folder / _MARKER_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def marker_of(folder: Path) -> str:
    """The project_id the marker in *folder* names (a state folder, a working copy), ``""`` for
    none: what proves a folder under the app-data root is one this app made."""
    return _marker_id(folder)


def _existing(root: Path, project_id: str, *, set_aside: bool = True) -> Path | None:
    """The folder under *root* that already belongs to *project_id*, or ``None``.

    Chosen by the MARKER alone, never by the name. The readable half is a label a
    rename changes, the eight characters are a label two ids can share, and a
    folder that had to be disambiguated carries a ``-2`` on top of both — a name
    match would have to know all three, and the marker simply says whose it is.
    ``iterdir`` serves files too; reading a marker *inside* one is the OSError
    :func:`_marker_id` answers ``""`` to, so a stray file can never match. Without
    *set_aside*, a folder set aside to be removed (:data:`SET_ASIDE_SUFFIX`) is not one: a
    checkout made into it would be deleted by the sweep that finishes the removal.
    """
    mine = sorted(
        p
        for p in (root.iterdir() if root.is_dir() else ())
        if _marker_id(p) == project_id and (set_aside or not p.name.endswith(SET_ASIDE_SUFFIX))
    )
    # Two folders for one id — which only a copied app-data root can produce — is a
    # wasted pip install, not an error worth raising in a path lookup, so one is
    # picked and it is always the same one.
    return mine[0] if mine else None


def _claim(root: Path, wanted: str, project_id: str) -> Path:
    """Make the folder *wanted* under *root* and write the marker proving whose it is.

    The name is disambiguated when it is taken, because eight hex characters are a
    label two ids can share; the marker inside is what a later lookup matches on.
    """
    folder = root / wanted
    taken = 1
    while folder.exists():  # somebody else's eight characters
        taken += 1
        folder = root / f"{wanted}-{taken}"
    folder.mkdir(parents=True)
    (folder / _MARKER_NAME).write_text(project_id, encoding="utf-8")
    return folder


def _resolve(project_path: Path | str, *, create: bool) -> Path | None:
    """This project's machine-local folder, made if *create* and it has none yet."""
    project_path = Path(project_path)
    key = os.path.normcase(os.path.abspath(project_path))
    resolved = _RESOLVED.get(key)
    if resolved is not None:
        return resolved
    project_id = read_project_id(project_path)
    if not project_id:
        return None
    root = local_root()
    try:
        resolved = _existing(root, project_id)
        if resolved is None:
            if not create:
                return None
            resolved = _claim(root, namespace(project_path, project_id), project_id)
    except OSError:
        logger.warning("No machine-local state for %s", project_path, exc_info=True)
        return None
    _RESOLVED[key] = resolved
    return resolved


def state_dir(project_path: Path | str) -> Path | None:
    """This project's machine-local folder, or ``None`` when it has no id.

    Found by ID, never by name: an existing folder for this id is
    used whatever readable half it carries, so renaming the project folder does
    not strand the venv this machine already built. The folder and its marker are
    created here if this machine has none yet — this is the only place that knows
    the name, and a folder made anywhere else would carry no proof of whose it is.
    """
    return _resolve(project_path, create=True)


def existing_state_dir(project_path: Path | str) -> Path | None:
    """The folder this machine ALREADY has for the project — never creates one.

    For the two callers that must not leave a folder behind: asking whether there
    is an interpreter to run is a question about any folder at all, and the New
    Project rollback deletes what it is given.
    """
    return _resolve(project_path, create=False)


def venvs_root() -> Path:
    """Where the SHARED Python environments live — one per bundle, not per project.

    Beside :func:`local_root`, not inside it, for the reason
    :func:`working_copies_root` is: what lives here belongs to a bundle, and hanging
    it under a folder named after one project would be a lie about whose it is.
    :mod:`venv_pool` owns everything inside; this module owns the place, so that the
    guard in ``tests/test_project_local_state.py`` has exactly two files to allow.
    """
    return local_root().parent / "venvs"


def python_runtimes_root() -> Path:
    """Private base interpreters shared by the bundle environments on this machine."""
    return local_root().parent / "python"


def python_runtime_cache_root() -> Path:
    """Private download/cache storage for the runtime installer."""
    return local_root().parent / "python_cache"


def remove_legacy_venvs() -> int:
    """Delete the per-project ``.venv`` folders the one-venv-per-project layout left.

    Nothing reads them since the pool: they are app-owned, re-creatable, and each is
    the hundreds of megabytes this change exists to give back. Everything else in a
    state folder — the media cache, the sync's origin record and trash — is another
    concern's and is not touched. Re-proved right here, as every delete in this repo
    is: the parent must be the state root, the folder must carry the marker that says
    it is a project's state, and a link is refused outright. Returns how many went.
    """
    root = local_root()
    removed = 0
    for state in sorted(root.iterdir()) if root.is_dir() else ():
        if not state.is_dir() or state.parent != root or not _marker_id(state):
            continue
        for name in (_VENV_DIR_NAME, _INSTALL_RECORD_NAME):
            stale = state / name
            try:
                # A JUNCTION answers False to ``is_symlink`` and True to ``is_dir`` on
                # Windows, so it would be walked into as if it were the old venv —
                # ``rmtree`` then refuses the reparse point and this logs a traceback
                # on every single start-up, for ever. Refused here instead.
                if is_link(stale) or not stale.exists():
                    continue
                if stale.is_dir():
                    shutil.rmtree(stale)
                else:
                    stale.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not remove the retired %s", stale, exc_info=True)
    return removed


#: The suffix of a working copy set aside to be removed (``working_copy_cleanup``, *Forget*). It
#: still carries its marker, and is never this project's working copy again.
SET_ASIDE_SUFFIX = ".removing"


def working_copies_root() -> Path:
    """Where projects that live on a SERVER are checked out to.

    Beside :func:`local_root`, not inside it. A working copy is an ordinary project
    folder — the runner, the editor, the bundle install and the configs all work on
    it unchanged — so it is not state about a project, it IS one; its own venv and
    cache hang under ``projects/`` keyed by the same id, exactly like a project on
    a local disk.
    """
    return local_root().parent / "working_copies"


def app_data_folders() -> list[Path]:
    """The app-data folders the app writes its own files into with ``atomic_write``:
    the settings folder (the one ``save_app_settings`` writes), the local root, the
    state root and each project's state folder, the environment pool and each of its
    folders. Only folders this machine alone writes — never a working copy (it IS a
    project). The app's own folders and their direct children are checked for links
    (a junction may lead to a share) and for lying on a share (a redirected ``%APPDATA%``):
    such a folder is left out, and so is all the local root holds when the local root is
    one. Missing folders are left out.
    """

    def plain(folder: Path) -> bool:
        # A folder on a share (``%APPDATA%`` redirected to a UNC path) is written by other PCs
        # too, and a pid dead here may be their write in flight.
        return (
            not is_link(folder) and not evidence_store_fs.is_remote_root(folder) and folder.is_dir()
        )

    settings = get_app_settings_file().parent
    folders = [settings] if plain(settings) else []
    if not plain(local_root().parent):
        return folders
    folders.append(local_root().parent)
    for parent in (local_root(), venvs_root()):
        if not plain(parent):
            continue
        folders.append(parent)
        try:
            folders += [child for child in parent.iterdir() if plain(child)]
        except OSError:
            logger.warning("Could not list %s", parent, exc_info=True)
    return folders


def working_copy_dir(project_id: str, project_path: Path | str) -> Path:
    """This machine's working copy folder for *project_id*, made when it has none.

    Named ``<name>__<id8>`` after *project_path* — the path the project has ON THE
    SERVER — and matched on the marker alone, like every other state folder: a
    project renamed on the server keeps the copy this bench already downloaded
    rather than downloading a second one beside it.
    """
    root = working_copies_root()
    found = _existing(root, project_id, set_aside=False)
    if found is not None:
        return found
    root.mkdir(parents=True, exist_ok=True)
    return _claim(root, namespace(project_path, project_id), project_id)


def venv_dir(project_path: Path | str) -> Path | None:
    """Where this project's venv is, or would be — ``None`` when it has no bundle.

    Shared: it belongs to the project's BUNDLE, and every project on that bundle gets
    this same answer. "Would be" is what a user who has no environment yet is shown,
    so the folder is named even before it is built.
    """
    from . import venv_pool

    folder = venv_pool.placement(project_path)
    return venv_pool.venv_in(folder) if folder is not None else None


def media_cache_dir(project_path: Path | str) -> Path | None:
    """Where this project's rendered-media cache lives — ``None`` without an id."""
    state = state_dir(project_path)
    return state / _MEDIA_CACHE_DIR_NAME if state is not None else None


def evidence_index_cache_dir(project_id: str, project_name: str = "") -> Path | None:
    """Where this PC keeps its copy of the project's evidence index shards read from a server
    (FTPS, SFTP: :mod:`evidence_shard_cache`): in the project's machine-local folder, found by
    its id alone (a search knows no project folder), made under *project_name* when this PC has
    none yet. Never inside the project. ``None`` without an id or an app-data root."""
    if not project_id:
        return None
    root = local_root()
    try:
        state = _existing(root, project_id) or _claim(
            root, namespace(project_name or "project", project_id), project_id
        )
    except OSError:
        logger.warning("No machine-local state for project %s", project_id, exc_info=True)
        return None
    return state / _INDEX_CACHE_DIR_NAME


def local_trash_dir(project_path: Path | str) -> Path | None:
    """Where a file the SYNC removes from a working copy is kept. ``None`` without an id.

    A pull removes a file here when it is gone on the server, and the premise that
    made that safe — "the server's copy is in ``.trash``" — only holds when this app
    did the deleting. An administrator's cleanup, a restored backup, a folder dragged
    aside on the server: the file is simply not there any more, and unlinking the last
    copy of it destroys work nobody meant to delete. So it is MOVED here instead.

    It lives in the machine-local state folder, beside the venv: outside the project
    folder, so it never travels, never has to be excluded from a scan, and is not
    something another bench can see. Composed here because this module is the one
    place allowed to name what lives under the app-data root.
    """
    state = state_dir(project_path)
    return state / _TRASH_DIR_NAME if state is not None else None


def bench_unsafe_file() -> Path | None:
    """Where THIS MACHINE's persistent UNSAFE latch lives. ``None`` when the
    app-data root cannot be made or listed at all.

    A safety interlock is a statement about the hardware in front of ONE BENCH. It
    was ``<project>/config/bench_unsafe.json``, inside the folder several benches
    share, so BENCH-02 latching blocked BENCH-01 and BENCH-03, and BENCH-03 clearing
    it cleared the latch of a bench whose supply was still live.

    It takes no project, and that is the point: a PSU whose output could not be
    proved off is energized whatever project the window has open, so a latch keyed
    by ``project_id`` let the operator walk past it by opening another project. ONE
    file per machine, beside the state folders rather than inside one — nothing in
    it belongs to a project.

    Composed here because this module is the one place allowed to name what lives
    under the app-data root.
    """
    root = local_root().parent
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("No machine-local state root for the UNSAFE latch", exc_info=True)
        return None
    return root / _BENCH_UNSAFE_NAME


def bench_state_file(project_path: Path | str, name: str) -> Path | None:
    """Where one bench's own working state for a project lives; ``None`` without an id.

    The preflight's last answers, the last-seen instrument identities and the tests'
    addresses and remote/manual describe the hardware plugged into THIS PC. In the
    project folder they travelled: bench B inherited bench A's checked endpoints and
    A's identity cache, and the
    "identity changed" gate — which exists to notice that somebody swapped an
    instrument — compared B's hardware against A's memory of its own.

    Unlike the latch this IS per project, because what was checked for a project is
    a fact about that project on this bench.
    """
    if name not in _BENCH_STATE_NAMES:
        raise ValueError(f"{name} is not bench state this seam places")
    state = state_dir(project_path)
    return state / name if state is not None else None


def install_record(project_path: Path | str) -> Path | None:
    """Where the record of what was installed into THIS venv lives.

    It describes the venv, so it lives beside it — which is now the shared pool
    folder. In the project it was a claim every bench inherited and none of them
    could check: bench A upgrades the bundle, bench B reads declared == installed
    beside a venv still holding the old wheels and calls it in sync. ``None``
    without a bundle, as the venv is.
    """
    from . import venv_pool

    folder = venv_pool.placement(project_path)
    return venv_pool.install_record_in(folder) if folder is not None else None


def venv_python(venv: Path) -> Path:
    """The interpreter inside *venv*, whether or not it exists."""
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def existing_venv_python(project_path: Path | str) -> Path | None:
    """This project's interpreter, or ``None`` when there is no venv to run.

    Read-only: nothing is made to answer it. Every run and every display sweep asks
    this, of whatever path the caller has. A build that was interrupted is not an
    answer — only a pool folder whose ``.complete`` marker was written is.
    """
    from . import venv_pool

    folder = venv_pool.resolve(project_path)
    if folder is None:
        return None
    python = venv_python(venv_pool.venv_in(folder))
    return python if python.exists() else None
