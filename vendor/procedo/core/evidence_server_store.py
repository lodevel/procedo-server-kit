"""What every store of the shared layout on a server TREE adds to it: the server is never
grown a base path, Check access and the sign-in test, and the artifact validator.

:class:`ServerTreeStore` is :class:`evidence_remote_cache.RemoteCachedStore` plus what only a
server root does, the mirror of what :class:`evidence_store_fs_shared.FilesystemStore` owns
for a filesystem root. It is written over the tree alone (``self.io``), so FTPS
(:class:`evidence_store_ftps.FtpsStore`) and SFTP (:class:`evidence_store_sftp.SftpStore`)
share it word for word; a backend adds its endpoint, its params and its own wording
(:meth:`ServerTreeStore._missing_folder_note`, :meth:`ServerTreeStore._capability_notes`).

* **A missing base path is reported, never created** (``_mkdir_safe``), so a mistyped
  ``base_path`` cannot quietly grow a second, empty store.
* **Check access** proves list, write, rename and delete in the evidence folder; **sign_in**
  proves the credential and the base path and writes nothing.
* **An artifact's cached copy is revalidated** against the server's size and second
  (``_fetch_if_stale``), since artifacts are overwritten by name.

Qt-free.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path, PurePosixPath

from .evidence_owner import check_owner
from .evidence_remote_cache import RemoteCachedStore
from .evidence_shared_layout import _PROBE
from .evidence_store import Absent, AccessReport, EvidenceError, OpContext
from .evidence_store_fs import os_error
from .evidence_store_fs_shared import SKEW_WARN_S
from .evidence_tree import make_dirs

log = logging.getLogger(__name__)


class ServerTreeStore(RemoteCachedStore):
    """The shared layout on a server tree (see the module note)."""

    def _missing_folder_note(self) -> str:
        """What a sign-in test says when the base path is not on the server."""
        return f"the base path does not exist on the server: {self.root}"

    def _capability_notes(self) -> list[str]:
        """What Check access adds about the server's own abilities (FTPS: no ``MLSD``)."""
        return []

    # -- paths ----------------------------------------------------------------------

    def _mkdir_safe(self, target: PurePosixPath) -> None:
        """mkdir -p below the root — but the ROOT is never created: a base path that is
        not there is a mistyped configuration, not a store to grow."""
        if not self.io.is_dir(self.root):
            raise EvidenceError(
                "not_found", f"the base path does not exist on the server: {self.root}"
            )
        make_dirs(self.io, self.root, target)

    # -- access ---------------------------------------------------------------------

    def check_access(self, ctx: OpContext | None = None) -> AccessReport:
        """Prove login + list + write + rename + delete IN the project's evidence
        folder — the base path itself since 2026-09-19 — time the round trip, and
        compare the server's own timestamp with this clock. A base path another
        project's marker owns is the whole answer: nothing is probed and the refusal
        names both projects."""
        report = AccessReport(False, False, False, False, False, None, None)
        if not self.project_id:
            report.notes.append("no project_id yet: open the project once, then check again")
            return report
        with self.io.operation((ctx or OpContext()).cancel):
            try:
                # ``denied`` is the only error this raises: a marker read that could not
                # CONCLUDE — a rejected credential among them — claims nothing and
                # refuses nothing, and the probe below meets that failure itself, where
                # ``auth`` is already told from a capability that FAILED.
                check_owner(self, self.project_id)
            except EvidenceError as exc:
                refused = AccessReport(
                    False, False, False, False, False, None, None, probed=frozenset()
                )
                refused.notes.append(str(exc))
                return refused
            ns = self._namespace(self.project_id)
            probe = ns / _PROBE
            moved = probe.with_suffix(".tmp2")
            start = time.perf_counter()
            try:
                if not self.io.is_dir(self.root):  # a mistyped base path is reported
                    raise OSError(f"the base path does not exist on the server: {self.root}")
                report.reachable = True
                self._mkdir_safe(ns)
                self.io.list_names(ns)
                report.can_list = True
                self.io.write_atomic(probe, b"probe", self.tag)
                report.can_write = True
                report.clock_skew_s = self.io.stat(probe)[1] - time.time()
                self.io.rename(probe, moved)
                report.can_rename = True
                self.io.unlink(moved)
                report.can_delete = True
                report.per_file_ms = (time.perf_counter() - start) * 1000 / 3
            except EvidenceError as exc:
                # A cancelled probe is not a capability that FAILED, and neither is a
                # rejected credential: "write: FAILED" would send the user to the
                # server's ACL when the answer is the password, so ``auth`` stays a
                # typed error and the view offers the credentials editor.
                if exc.kind in ("cancelled", "auth"):
                    raise
                report.notes.append(str(exc))
            except OSError as exc:
                # The tree raises OSErrors — ``AuthRequired`` IS one — so this arm has
                # to classify before it decides. Without it a ``530`` at the write (a
                # server that lets the credential list but not store) was reported as
                # "write: FAILED", sending the user to the server's ACL to fix a
                # password. Only the classification tells the two apart.
                failure = os_error(exc)
                if failure.kind == "auth":
                    raise failure from exc
                report.notes.append(str(exc))
            for leftover in (probe, moved):
                try:
                    self.io.unlink(leftover, missing_ok=True)
                except (OSError, EvidenceError):
                    pass
        for capability in ("list", "write", "rename", "delete"):
            ok = getattr(report, "can_" + capability)
            report.notes.append(f"{capability}: {'ok' if ok else 'FAILED'}")
        if report.per_file_ms is not None:
            report.notes.append(f"≈{report.per_file_ms:.1f} ms per file")
        if report.clock_skew_s is not None and abs(report.clock_skew_s) > SKEW_WARN_S:
            report.notes.append(f"clock skew {report.clock_skew_s:+.0f} s against the store")
        report.notes.extend(self._capability_notes())
        return report

    def sign_in(self, ctx: OpContext | None = None) -> AccessReport:
        """Prove the credential and the base path WITHOUT a project — what the
        machine-settings credentials editor tests when no project is open.

        ``check_access`` cannot answer there: it refuses before it touches the network
        without a ``project_id``, because proving a write means writing inside a
        project's evidence folder. This logs in and lists the base path and writes nothing,
        which is enough to tell a wrong password (``auth``) from a working one and a
        mistyped base path from a reachable store — and creates no folder to find out.

        The report SAYS it wrote nothing: ``probed`` carries only ``list``, so the
        renderer prints "not tested here" for write, rename and delete rather than
        "FAILED" — which would send the user to the server's permissions over a probe
        that deliberately never went there.
        """
        report = AccessReport(
            False, False, False, False, False, None, None, probed=frozenset({"list"})
        )
        start = time.perf_counter()
        try:
            with self.io.operation((ctx or OpContext()).cancel):
                if not self.io.is_dir(self.root):
                    report.notes.append(self._missing_folder_note())
                    return report
                report.reachable = True
                report.can_list = self.io.list_names(self.root) is not None
                report.per_file_ms = (time.perf_counter() - start) * 1000 / 2
        except OSError as exc:  # a rejected credential is ``auth``, not an empty answer
            raise os_error(exc) from exc
        report.notes.append(
            f"signed in as {self.endpoint.user or '(no user)'} at {self.io.identity}"
        )
        report.notes.append(
            "write, rename and delete are proved per project: open one and use Check "
            "access on its Evidence tab"
        )
        return report

    # -- materialization: the artifact validator ------------------------------------

    def _fetch_if_stale(self, remote: PurePosixPath, local: Path) -> None:
        """Reuse the cached copy only while the server's size and time still match AND
        the file was already that old when this machine fetched it.

        ``MLSD``'s ``modify`` fact has one-second resolution, so an artifact rewritten
        to the same length within the second of a fetch is indistinguishable from the
        copy held — a draft saved twice in a second served the first bytes back. The
        stamp therefore also records WHEN the fetch happened, and a file whose time is
        not strictly older than that is fetched again rather than trusted. Only a proven
        absence is ignored: an unreachable or refused server must not quietly serve a
        stale artifact as current.
        """
        try:
            size, mtime = self.io.stat(remote)
        except Absent:
            return  # genuinely not on the server: a sidecar that was never written
        stamp = local.with_name(local.name + ".stamp")
        want = f"{size}:{int(mtime)}"
        if local.is_file() and stamp.is_file():
            held = stamp.read_text(encoding="utf-8").split(":")
            if len(held) == 3 and f"{held[0]}:{held[1]}" == want and int(mtime) < int(held[2]):
                return
        fetched = int(time.time())
        local.write_bytes(self.io.read_bytes(remote))
        stamp.write_text(f"{want}:{fetched}", encoding="utf-8")
