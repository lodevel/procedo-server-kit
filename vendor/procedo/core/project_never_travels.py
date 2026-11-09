"""What is in a project folder but is not the project: the paths that never travel as its
files, over any storage, and the folders that travel as objects of their own.

**Exclusions** (:func:`exclusion`, the list :data:`EXCLUSIONS`): evidence in every shape the
folder ever held it — the legacy in-project trees, and, for a project on a server, the shared
layout under :data:`EVIDENCE_DIRNAME` that IS "in the project folder" for such a project —
build output, transfers in progress, this machine's own state and one bench's own working
state (the editor's session and chat files, the configuration lock, a ``.bak``). The sync
plans none of them (``project_sync_plan``), a move or a copy leaves them behind
(``project_relocate``), Studio's *Duplicate Test* does not copy them (:func:`bench_state`),
and a server refuses them (``procedo_server_contract.md`` §5.2, rule P11).

**Objects** (:data:`OBJECT_DIRS`): ``bundle/`` and ``releases/`` travel as objects of their
own, never as files, over a storage read by what changed.

The list is data, and this module reads no storage and imports no evidence backend, so a
check outside the app (the server conformance kit, its reference server) enumerates it without
the app's stores.

Qt-free, stdlib only, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import evidence_legacy, project_local_state

#: "In the project folder" is the shared layout under ``<project>/evidence/`` — for a
#: project in a folder (``evidence_config.default_spec``) and for a project ON A SERVER,
#: reached through that same server (same type, same credential); never the project's ROOT,
#: where a board would collide with the reserved ``tests/``, ``config/``, ``bundle/``.
#: Named ONE place so the pre-fills and the sync's exclusion never disagree on it.
EVIDENCE_DIRNAME = "evidence"


def _evidence_roots() -> frozenset[str]:
    """The folders a project's own evidence occupies, ASKED of the modules that own
    those names rather than spelled again here: the old layout's two trees
    (:mod:`evidence_legacy`, which an older Bench may still write), the pre-2025
    root-level ``results/`` and ``reports/``, and ``EVIDENCE_DIRNAME``
    — the folder "in the project folder" means, so the pre-fills and this exclusion
    can never name two different folders.
    """
    named = {tree.name for tree in evidence_legacy.trees(Path("project"))}
    return frozenset(named | {"results", "reports", EVIDENCE_DIRNAME})


#: All four are ROOT-anchored, exactly as the store composes them, so a test folder
#: of its own named ``exports`` still travels with the project.
_EVIDENCE_ROOTS = _evidence_roots()
#: Where a confirmed server-side delete puts the file instead of unlinking it
#: (``project_sync_trash.trash_remote``). ROOT-anchored on the server, and outside the
#: sync in BOTH directions: never downloaded, never planned, and never itself
#: trashed — a trash folder that synced would come back down as the project's own
#: files and then be deleted again on the next pass, for ever.
TRASH_DIR = ".trash"
#: Build output, at any depth.
_BUILD_DIRS = frozenset({"__pycache__"})
#: One bench's own working state, left in a test folder by whoever is working in it
#: and rewritten as they work — not the project's definition, and (the two LLM
#: files especially, rewritten on every turn of a conversation) enough churn to
#: dominate the push traffic on its own. ``.project_id.lock`` is the operating
#: system lock ``project_identity`` holds while it mints an id, carrying this
#: host's name and pid; the next two are the workflow editor's per-test session
#: baseline and chat history (``procedo/studio/core/artifact_manager.py``,
#: ``core/chat_history.py``). ``bench_unsafe.json`` is the persistent UNSAFE safety
#: latch: it lives in machine-local state now
#: (``project_local_state.bench_unsafe_file``) because one bench's live PSU is not
#: another bench's, and a copy of it that a folder still holds is the one file that
#: must never be handed to a second bench. ``bench_preflight.json`` (what this
#: operator last checked) and ``bench_fingerprints.json`` (the instrument identities
#: last seen AT THIS BENCH) moved out for the same reason — the second one exists to
#: notice a swapped instrument, and a shared copy compared one bench's hardware
#: against another's memory.
_BENCH_FILES = frozenset(
    {
        ".project_id.lock",
        ".evidence-rename.json",  # recovery belongs to the working copy that moved it
        ".llm_session.json",
        ".llm_chat_history.json",
        "bench_unsafe.json",
        "bench_preflight.json",
        "bench_fingerprints.json",
    }
)


#: Studio's *Delete test* renames the folder to ``.<name>.deleting`` (``.deleting2``…) before it
#: removes it, so a file held open stops the delete before anything goes, and its *Duplicate
#: Test* copies a test into ``.<name>.copying`` (``.copying2``…) before it gets its name
#: (``procedo.studio.core.test_folder_ops``). Mid-way, or left behind by one that failed, it is
#: not the project: carried up, every other PC would receive half a test.
SET_ASIDE_WORDS = ("deleting", "copying")
_SET_ASIDE = re.compile(r"\..+\.(?:" + "|".join(SET_ASIDE_WORDS) + r")\d*")


def bench_state(name: str) -> bool:
    """Whether a file or folder named *name* in the project is never the project's: build output
    (:data:`_BUILD_DIRS`), this machine's (``project_local_state.is_machine_state``) or one
    bench's working state (:data:`_BENCH_FILES`). The sync leaves it out (:func:`exclusion`), and
    Studio's *Duplicate Test* does not copy it into the copy (the one list for both)."""
    return name in _BUILD_DIRS or name in _BENCH_FILES or project_local_state.is_machine_state(name)


#: Which name of a path an entry of the exclusion list looks at: the first, every one, the last.
AT_ROOT = "root"
AT_ANY_DEPTH = "any depth"
AS_FILE = "file"


@dataclass(frozen=True)
class Excluded:
    """One entry of the exclusion list (:data:`EXCLUSIONS`): which name of a path it looks at
    (:data:`AT_ROOT`, :data:`AT_ANY_DEPTH`, :data:`AS_FILE`), whether it takes a name, why
    (``{name}`` is the name taken), and names it takes, one for each way it can match.

    The list is data so that a check outside the app enumerates it rather than copying it: the
    server conformance kit sends a path built from every example and expects the server to
    refuse it, so an entry added here is checked there with no change of its own.
    """

    where: str
    takes: Callable[[str], bool]
    why: str
    examples: tuple[str, ...]


def _one_of(names: frozenset[str]) -> Callable[[str], bool]:
    return names.__contains__


def _ends_with(suffix: str) -> Callable[[str], bool]:
    return lambda name: name.endswith(suffix)


#: The exclusion list :func:`exclusion` reads, in the order it reads it: the root entries, then
#: each name of the path against the any-depth entries, then the last name against the file
#: entries.
EXCLUSIONS: tuple[Excluded, ...] = (
    Excluded(
        AT_ROOT,
        _one_of(_EVIDENCE_ROOTS),
        "{name}/ is evidence, which lives in a store of its own",
        tuple(sorted(_EVIDENCE_ROOTS)),
    ),
    Excluded(
        AT_ROOT,
        _one_of(frozenset({TRASH_DIR})),
        "{name}/ is what the server keeps a deleted file in, not the project",
        (TRASH_DIR,),
    ),
    Excluded(
        AT_ANY_DEPTH, _one_of(_BUILD_DIRS), "{name}/ is build output", tuple(sorted(_BUILD_DIRS))
    ),
    Excluded(
        AT_ANY_DEPTH,
        project_local_state.is_machine_state,
        "{name} belongs to the machine, not to the project",
        tuple(sorted(project_local_state.machine_state_names())),
    ),
    Excluded(
        AT_ANY_DEPTH,
        lambda name: _SET_ASIDE.fullmatch(name) is not None,
        "{name} is a folder Studio is deleting or copying, set aside under a hidden name",
        tuple(f".T1.{word}{n}" for word in SET_ASIDE_WORDS for n in ("", "2")),
    ),
    Excluded(
        AS_FILE,
        _one_of(_BENCH_FILES),
        "{name} is one bench's own working state, rewritten as it works",
        tuple(sorted(_BENCH_FILES)),
    ),
    Excluded(
        AS_FILE,
        _ends_with(".part"),
        "a .part file is a transfer that has not landed",
        ("config.json.sync.17.part",),
    ),
    Excluded(
        AS_FILE,
        _ends_with(".bak"),
        "a .bak file is somebody's own copy of a file that already travels",
        ("procedure.json.bak",),
    ),
)


def exclusion(path: str) -> str:
    """Why this relative path is not part of the project (``""`` when it is): the first entry
    of :data:`EXCLUSIONS` that takes one of its names, as spelled (compared exactly).

    Evidence never travels: it may be large, and it belongs to a store of its own —
    for a project on a server, that store is reached through the SAME server, under
    the project's own folder, and still excluded by name rather than carried down on
    every checkout. Machine state never travels: a venv bakes in its own path and a
    media cache is this bench's. Nor does one bench's working state
    (:data:`_BENCH_FILES`), which is rewritten far more often than the project it
    sits in. ``bundle/`` DOES travel — the wheels are the project's definition.
    """
    parts = path.split("/")
    looked_at = (
        (AT_ROOT, parts[:1]),
        (AT_ANY_DEPTH, parts),
        (AS_FILE, parts[-1:]),
    )
    for where, names in looked_at:
        for name in names:
            for entry in EXCLUSIONS:
                if entry.where == where and entry.takes(name):
                    return entry.why.format(name=name)
    return ""


#: The folders of a project that travel as objects, never as files, over a storage read by
#: what changed (``data_format.md`` §4.2): the bundle, and the releases.
OBJECT_DIRS = frozenset({"bundle", "releases"})


def is_object(path: str) -> bool:
    """Whether *path* is under ``bundle/`` or ``releases/`` (its folder compared as a name)."""
    return "/" in path and path.split("/", 1)[0].casefold() in OBJECT_DIRS
