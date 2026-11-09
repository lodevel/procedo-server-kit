"""A project-sync pass's report: what one pass did and decided not to do (:class:`SyncReport`),
and the text a human reads of it (pure, no Qt)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the report and its words stay free of the sync's machinery
    from .project_origin import Conflict


@dataclass
class SyncReport:
    """What one pass did, and everything it decided not to do.

    ``conflicts`` lists the files nothing acted on because both copies moved — changed
    on both sides, new on both, or changed on one and deleted on the other — with the
    kind of each (``project_sync_plan.EDITED_EDITED``…): the plan found it, or the
    server's or the working copy's copy moved after the plan was made and the transfer
    stood down. Both copies are as they were; somebody decides
    (``project_sync.resolve``). It belongs in front of a human, not only in a log line.

    ``pending_deletes`` is the owner's rule made visible: a file this working copy
    no longer has, which the pass did NOT remove from the server because nobody
    approved that path. Its manifest row is kept, so the next pass sees the same
    thing and asks again.
    """

    downloaded: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    #: Files taken OUT of the working copy — they are in :attr:`local_trash`, never
    #: destroyed, because a file can leave a server in ways this app never sees.
    deleted_local: list[str] = field(default_factory=list)
    deleted_remote: list[str] = field(default_factory=list)
    pending_deletes: list[str] = field(default_factory=list)
    #: Where this pass put what it took out of the working copy (``""`` when it took
    #: nothing). It belongs in front of the user: a pass that removes files from
    #: under a bench must say where they went.
    local_trash: str = ""
    skipped: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    #: Downloads this pass left for the background one a first open makes after the
    #: project is shown (``project_sync_plan.deferred``): the board archive, documents,
    #: anything large. Their rows are as they were, so the next pass downloads them.
    deferred: list[str] = field(default_factory=list)
    #: What a pass did NOT bring into the working copy, or take out of it, because a test
    #: was running from it (``project_sync.pull``'s *hold*): their rows are as they were,
    #: so the pass after the run does it.
    held: list[str] = field(default_factory=list)


#: Everything a project RUNS in lives under this one prefix.
_BUNDLE_PREFIX = "bundle/"


def bundle_touched(report: Any) -> bool:
    """Whether this pass changed the project's bundle, in either direction.

    ``downloaded`` covers a new or restored wheel, a rewritten ``requirements.txt``
    and a replaced ``bundle.json``; ``deleted_local`` covers a wheel that is gone on
    the server and has just been taken out of the working copy. Either means the
    project asks for a different environment than the one this process resolved.
    """
    touched = list(getattr(report, "downloaded", [])) + list(getattr(report, "deleted_local", []))
    return any(str(path).replace("\\", "/").startswith(_BUNDLE_PREFIX) for path in touched)


#: What a conflict says, by its kind (``project_sync_plan.EDITED_EDITED``…).
_CONFLICT_WORDS = {
    "edited/edited": "changed here and on the server",
    "new/new": "new here and on the server, with different contents",
    "edited/deleted": "changed here, deleted on the server",
    "deleted/edited": "deleted here, changed on the server",
}


def conflict_line(path: str, kind: str) -> str:
    """``tests/t1/test.py: changed here and on the server — neither copy was touched``."""
    return f"{path}: {_CONFLICT_WORDS.get(kind, kind)} — neither copy was touched"


def open_questions(report: Any) -> str:
    """The lines of *report* nobody has decided yet — a skip or a refusal already
    happened, so only the conflicts and a pending delete are still open. What the
    sync dialog shows ABOVE the log, in a warning colour, so a finished push
    and a question needing an answer never read as the same plain line.
    :func:`describe_report` keeps including them too, unchanged, for the
    tooltip and the status line."""
    if report is None:
        return ""
    lines = []
    for conflict in getattr(report, "conflicts", []):
        lines.append(conflict_line(conflict.path, conflict.kind))
    for path in getattr(report, "pending_deletes", []):
        lines.append(f"{path}: missing here and still on the server — nobody has said it may go")
    return "\n".join(lines)


def describe_report(report: Any) -> str:
    """One pass, in the lines a human has to see — conflicts first."""
    if report is None:
        return ""
    lines = []
    for conflict in getattr(report, "conflicts", []):
        lines.append(conflict_line(conflict.path, conflict.kind))
    for path in getattr(report, "pending_deletes", []):
        lines.append(f"{path}: missing here and still on the server — nobody has said it may go")
    for path in getattr(report, "deleted_local", []):
        lines.append(f"{path}: gone on the server — moved to this PC's trash, not deleted")
    if getattr(report, "local_trash", ""):
        lines.append(f"this PC's trash for that pass: {report.local_trash}")
    for path, why in getattr(report, "skipped", []):
        lines.append(f"{path}: not synced ({why})")
    for path, why in getattr(report, "refused", []):
        lines.append(f"{path}: refused ({why})")
    counts = [
        f"{len(getattr(report, name, []))} {label}"
        for name, label in (
            ("downloaded", "downloaded"),
            ("uploaded", "uploaded"),
            ("deleted_local", "deleted here"),
            ("deleted_remote", "deleted on the server"),
        )
        if getattr(report, name, [])
    ]
    if counts:
        lines.append(", ".join(counts))
    return "\n".join(lines) or "Nothing to do."


def _named(paths: list[str]) -> tuple[list[str], int]:
    """The tests *paths* touch (their folders, in order) and how many files outside tests."""
    tests: dict[str, None] = {}
    others = 0
    for path in paths:
        parts = str(path).replace("\\", "/").split("/")
        if len(parts) > 2 and parts[0] == "tests":
            tests.setdefault(parts[1])
        else:
            others += 1
    return list(tests), others


def _files(count: int) -> str:
    return f"{count} file{'' if count == 1 else 's'}"


def updated_notice(report: Any, where: str = "share") -> str:
    """``Updated from the share: <b>t1</b>, <b>t2</b> (3 files)`` — what a background pass brought
    into the copy from *where* (``share``, ``server``; rich text, names escaped); ``""`` when it
    brought nothing. *report*: a ``SyncReport``, or the pass stamp of the app that made it."""
    paths = list(getattr(report, "downloaded", []))
    if not paths:
        return ""
    tests, _others = _named(paths)
    if not tests:
        return f"Updated from the {where}: {_files(len(paths))}"
    names = ", ".join(f"<b>{_escape(name)}</b>" for name in tests)
    return f"Updated from the {where}: {names} ({_files(len(paths))})"


def deleted_notice(report: Any) -> str:
    """``Deleted on the share: <b>t1</b>. This PC's copy is in its trash.`` (rich text); ``""``
    when the pass took nothing out of the copy."""
    paths = list(getattr(report, "deleted_local", []))
    if not paths:
        return ""
    tests, others = _named(paths)
    named = [f"<b>{_escape(name)}</b>" for name in tests]
    if others:
        named.append(_files(others))
    return f"Deleted on the share: {', '.join(named)}. This PC's copy is in its trash."


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
