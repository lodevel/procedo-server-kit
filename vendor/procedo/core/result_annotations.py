"""Per-result annotations (comments + verdict overrides) and the board's report-level draft."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import evidence_draft
from .annotation_entries import (
    CURRENT,
    OVERRIDE,
    TEST,
    Shown,
    as_recorded,
    changes,
    current_data,
    heads,
    histories,
    make_entry,
    slot,
    value_of,
    wanted_values,
    who_when,
)
from .evidence_board import segments
from .evidence_store import ArtifactInfo, EvidenceError
from .report_export import text_file_bytes

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .evidence_store import EvidenceKey, EvidenceStore, OpContext


# ---------------------------------------------------------------------------
#  Per-result annotations: comments and verdict overrides as entries
# ---------------------------------------------------------------------------

# The entries live wherever the run's store keeps them (``annotations/`` in the generation,
# the execution block merged in from the manifest) — or, for a run still waiting in this
# PC's outbox, queued there (``outbox_reads``); :mod:`annotation_entries` owns the rule.


def _read_annotation(
    store: EvidenceStore, key: EvidenceKey, ctx: OpContext | None = None
) -> dict[str, Any] | None:
    from .outbox_reads import read_annotation

    return read_annotation(store, key, ctx)


def load_result_metadata(
    store: EvidenceStore, key: EvidenceKey, ctx: OpContext | None = None
) -> dict[str, Any] | None:
    """The run's annotation as a reader sees it: what is current per key in the run file's
    shape (:func:`annotation_entries.current_data`: ``comments``, ``verdict_overrides``,
    each value with who and when), every entry (``entries``, the history) and the
    ``execution`` block; ``None`` when the run is not there. ``ctx``: the operation reading
    it (one outbox read)."""
    raw = _read_annotation(store, key, ctx)
    if not isinstance(raw, dict):
        return None
    return {**current_data(raw.get("entries") or []), "execution": raw.get("execution", {})}


def _identity(store: EvidenceStore) -> tuple[str, str]:
    """``(machine id, station name)`` an entry saved through ``store`` carries."""
    from .app_settings import station_id

    return getattr(store, "machine_id", "") or "", station_id()


def save_result_metadata(
    store: EvidenceStore,
    key: EvidenceKey,
    comments: dict[str, Any] | None = None,
    verdict_overrides: dict[str, Any] | None = None,
    operator: str | None = None,
    *,
    seen: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Add one entry per comment or verdict override of the run ``key`` names whose value
    differs from what the person saw — ``seen``: the run's annotation as
    :func:`load_result_metadata` read it when they opened the run (``None``: as it is now).
    A key they did not change adds nothing, so someone else's save meanwhile is never
    touched.

    *comments* / *verdict_overrides* are in the run file's shape:
    ``{"test": "text" | {"text": …}, "criteria": {key: …}}`` (``"value"`` for a verdict);
    an empty or absent value clears the key. ``operator`` is who saved them.

    Returns, per entry added, what the store answered
    (:func:`annotation_entries.outcome`: ``saved``, or ``changed`` with the current one);
    ``[]`` when nothing differs. Raises ``EvidenceError`` when the store refuses."""
    if seen is None:
        seen = load_result_metadata(store, key) or {}
    wanted = wanted_values(comments or {}, verdict_overrides or {})
    added = changes(seen.get("entries") or [], wanted, operator or "", *_identity(store))
    if not added:
        return []
    from .outbox_reads import add_annotation_entries

    return add_annotation_entries(store, key, added)


def use_values(
    store: EvidenceStore, key: EvidenceKey, chosen: list[dict[str, Any]], operator: str | None
) -> list[dict[str, Any]]:
    """ "Use mine" / "Use this one": each ``chosen`` entry's value made current again — added
    as a NEW entry by ``operator`` replacing its key's current one, nothing removed. Returns
    what the store answered, as :func:`save_result_metadata` does."""
    now = heads((load_result_metadata(store, key) or {}).get("entries") or [])
    added = []
    for e in chosen:
        head = now.get(slot(e))
        replaces = head["entry_id"] if head else None
        added.append(make_entry(*slot(e), value_of(e), replaces, operator or "", *_identity(store)))
    if not added:
        return []
    from .outbox_reads import add_annotation_entries

    return add_annotation_entries(store, key, added)


def preview_key(name: str, key: str) -> str:
    """An entry key as the report's preview names it: ``test:<test>`` or
    ``crit:<test>:<check key>``."""
    return f"test:{name}" if key == TEST else f"crit:{name}:{key}"


def marks(
    name: str,
    meta: dict[str, Any] | None,
    *,
    absolute: bool = False,
    shown: Shown | None = None,
) -> dict[str, dict]:
    """Who set each current value of the run of test ``name``, and how many entries its key
    holds, by preview key: ``{"verdict": "bob · today 09:12", "verdict_history": 2,
    "comment": "alice · today 08:55", "comment_history": 1}`` (a check's time is short:
    ``bob · 09:12``; ``absolute``: the date in full, for a report read any day; who, as
    *shown*: ``server_lists.namer`` of the run's store)."""
    found: dict[str, dict] = {}
    for (kind, key), lines in histories((meta or {}).get("entries") or []).items():
        field = "verdict" if kind == OVERRIDE else "comment"
        mark = found.setdefault(preview_key(name, key), {})
        mark[f"{field}_history"] = len(lines)
        head = next(line.entry for line in lines if line.status == CURRENT)
        if value_of(head) is not None:
            short = key != TEST and not absolute
            mark[field] = who_when(head, short=short, absolute=absolute, shown=shown or as_recorded)
    return found


def report_marks(
    metas: dict[str, dict[str, Any] | None], shown: Shown | None = None
) -> dict[str, dict]:
    """:func:`marks` of every test (``metas``: test -> its annotation), dated in full: what a
    generated report shows under each current verdict and comment (who, as *shown*)."""
    found: dict[str, dict] = {}
    for name, meta in metas.items():
        found.update(marks(name, meta, absolute=True, shown=shown))
    return found


def aggregate_annotations_for_report(
    metas: list[tuple[str, dict[str, Any] | None]],  # (test, load_result_metadata) per test
    global_draft: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Compile per-result annotations into aggregate dicts.

    Returns ``(comments, verdict_overrides)`` using the global key format
    expected by ``_build_report_template_context``:
      - ``"test:<test_name>"`` for test-level entries
      - ``"crit:<test_name>:<crit_id>"`` for criterion-level entries
      - ``"report"``  and ``"overall"`` for report-level entries (from global draft)

    """
    comments: dict[str, str] = {}
    verdict_overrides: dict[str, str] = {}

    for name, meta in metas:
        if meta is None:
            continue
        meta_comments = meta.get("comments", {})
        meta_verdicts = meta.get("verdict_overrides", {})

        # Test-level comment
        test_c = meta_comments.get("test", {})
        text = test_c.get("text", "") if isinstance(test_c, dict) else str(test_c)
        if text:
            comments[f"test:{name}"] = text

        # Test-level verdict
        test_v = meta_verdicts.get("test", {})
        value = test_v.get("value", "") if isinstance(test_v, dict) else str(test_v)
        if value:
            verdict_overrides[f"test:{name}"] = value

        # Criterion-level comments
        for crit_id, entry in meta_comments.get("criteria", {}).items():
            text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
            if text:
                comments[f"crit:{name}:{crit_id}"] = text

        # Criterion-level verdicts
        for crit_id, entry in meta_verdicts.get("criteria", {}).items():
            value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
            if value:
                verdict_overrides[f"crit:{name}:{crit_id}"] = value

    # Merge report-level entries from global draft
    if global_draft:
        report_comment = global_draft.get("report_comment", "")
        if report_comment:
            comments["report"] = report_comment
        overall_override = global_draft.get("overall_verdict_override", "")
        if overall_override:
            verdict_overrides["overall"] = overall_override

    return comments, verdict_overrides


# ---------------------------------------------------------------------------
#  Global draft helpers (report-level metadata only)
# ---------------------------------------------------------------------------


def global_draft_name(board: str) -> str:
    """The board's report draft (artifact kind ``draft``, beside its reports), named by
    its serial as its folder spells it (escaped, so any serial makes one file name): *board*
    is the board's key (:mod:`evidence_board`), and the draft is in that board's own folder."""
    return f"{segments(board)[-1]}.draft.json"


def save_global_draft(
    store: EvidenceStore,
    project_id: str,
    board: str,
    data: dict[str, Any],
    *,
    replaces: str | None = None,
    by: str = "",
) -> str:
    """Persist report-level metadata as the board's draft through the store (the bytes are
    what the pre-seam text-mode ``json.dump`` wrote, with who saved it when *by* is given),
    replacing the version *replaces* — the one read (:func:`draft_version`; ``None``: none).
    Its push replaces the draft held only when that is still the one read
    (:mod:`evidence_draft`). The version saved. Raises ``EvidenceError`` when the store
    refuses."""
    data["version"] = "2.0"
    data["modified_at"] = datetime.now().isoformat()
    if by:
        data[evidence_draft.SAVED_BY] = by
    name = global_draft_name(board)
    body = text_file_bytes(json.dumps(data, indent=2))
    store.write_artifact(
        "draft",
        project_id,
        board,
        name,
        body,
        ArtifactInfo(name, data["modified_at"], "", "", "", ""),
        replaces=replaces,
    )
    log.info("Global draft saved: %s/%s", board, name)
    return evidence_draft.version_of(body)


def changed_draft(
    store: EvidenceStore, project_id: str, board: str, replaces: str | None
) -> dict[str, str] | None:
    """The board's draft held now (this PC's queued copy, else the store's) when it is not
    the version *replaces* — someone saved it since it was read: ``{"by", "at", "version"}``.
    ``None`` when it is (or none is held, or the store cannot say: its push compares)."""
    try:
        held = store.read_artifact("draft", project_id, board, global_draft_name(board))
    except EvidenceError:
        return None
    version = evidence_draft.version_of(held)
    if version == replaces:
        return None
    by, at = evidence_draft.who(held)
    return {"by": by, "at": at, "version": version}


def draft_version(draft: dict[str, Any] | None) -> str | None:
    """The version of a draft as :func:`load_global_draft` read it (``None``: none read)."""
    return None if draft is None else evidence_draft.version_of_content(draft)


def load_global_draft(store: EvidenceStore, project_id: str, board: str) -> dict[str, Any] | None:
    """The board's draft, or *None* when absent, unreadable or not version 2.0."""
    try:
        data = json.loads(store.read_artifact("draft", project_id, board, global_draft_name(board)))
    except EvidenceError as exc:
        if exc.kind != "not_found":
            log.warning("Could not read the report draft of %s: %s", board, exc)
        return None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != "2.0":
        return None  # ignore old-format drafts
    return data


def split_annotations_for_saving(
    comments: dict[str, str],
    verdict_overrides: dict[str, str],
    procedure_names: list[str],
    operator: str | None = None,
    *,
    alias_report_globals_to_single_test: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Split aggregate comments/overrides into per-result local dicts.

    Returns ``(per_result, report_globals)`` where:
    - *per_result* maps procedure name → ``{"comments": ..., "verdict_overrides": ...}``
      using the v2 local metadata schema (attributed dicts).
    - *report_globals* is a dict with ``report_comment`` and
      ``overall_verdict_override`` for the global draft.

    *operator* is recorded as the ``by`` field on each entry.

    When *alias_report_globals_to_single_test* is True AND
    ``len(procedure_names) == 1``, a non-empty ``overall`` verdict /
    ``report`` comment is also written to that single test's
    ``verdict_overrides.test`` / ``comments.test`` (only when those
    test-level fields aren't already populated, so an explicit
    per-test edit wins). This makes the report-level dropdown +
    comment box at the top of the single-test preview persist where
    ``ProcedureInfo.overall_verdict`` looks for them. Multi-test
    callers leave this False so ``overall`` / ``report`` stay in the
    aggregate report-globals draft.
    """
    by = operator or ""
    per_result: dict[str, dict[str, Any]] = {
        name: {
            "comments": {"test": {}, "criteria": {}},
            "verdict_overrides": {"test": {}, "criteria": {}},
        }
        for name in procedure_names
    }

    report_comment = ""
    overall_verdict_override = ""

    for key, value in comments.items():
        if key == "report":
            report_comment = value
        elif key.startswith("test:"):
            name = key[len("test:") :]
            if name in per_result and value:
                per_result[name]["comments"]["test"] = {"text": value, "by": by}
        elif key.startswith("crit:"):
            parts = key.split(":", 2)
            if len(parts) == 3 and parts[1] in per_result and value:
                per_result[parts[1]]["comments"]["criteria"][parts[2]] = {"text": value, "by": by}

    for key, value in verdict_overrides.items():
        if key == "overall":
            overall_verdict_override = value
        elif key.startswith("test:"):
            name = key[len("test:") :]
            if name in per_result and value:
                per_result[name]["verdict_overrides"]["test"] = {"value": value, "by": by}
        elif key.startswith("crit:"):
            parts = key.split(":", 2)
            if len(parts) == 3 and parts[1] in per_result and value:
                per_result[parts[1]]["verdict_overrides"]["criteria"][parts[2]] = {
                    "value": value,
                    "by": by,
                }

    if alias_report_globals_to_single_test and len(procedure_names) == 1:
        only_name = procedure_names[0]
        bucket = per_result[only_name]
        if overall_verdict_override and not bucket["verdict_overrides"]["test"]:
            bucket["verdict_overrides"]["test"] = {
                "value": overall_verdict_override,
                "by": by,
            }
        if report_comment and not bucket["comments"]["test"]:
            bucket["comments"]["test"] = {"text": report_comment, "by": by}

    report_globals = {
        "report_comment": report_comment,
        "overall_verdict_override": overall_verdict_override,
    }
    return per_result, report_globals
