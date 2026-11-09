"""The web server's two searches, E2 (runs) and E8 (artifacts), paged, and the evidence search
over them (``EvidenceStore.search``, ``data_format.md`` §6.2): the same rows a folder's index
or the database answers, the server doing the filtering.

* **Runs: E2**, with every filter the contract offers for what the search asks: ``project_id``,
  ``part_number``, ``test`` (the name a run recorded), ``verdict``, ``from``/``to`` (``ended``
  within ``[from, to)``: the local midnight the first day picked starts with, and the one after
  the last), ``deleted``, and the run filters only a storage recording them answers:
  ``operator``, ``machine_id``,
  ``measurement`` with ``value_min``/``value_max``. The Board filter is ``serial``, once per
  serial it may name (:func:`evidence_search.serials_named`); the label's part number and
  revision are then kept by :func:`evidence_search.matches`, as on every storage.
* **Reports: E8** ``kind=report`` with ``include=info`` (a report's row is read from its record,
  :func:`evidence_index.info_of_report`, as a folder's and the database's are), narrowed by
  ``serial``, ``part_number``, ``test``, ``verdict``, ``deleted`` and ``from``/``to`` as E2's:
  a report records its time in UTC (:mod:`utc_time`), the ``generated`` the server holds, so a
  folder, the database and the server compare the same instant with the same bounds. Given a
  run filter, no report is asked (a report records none of them).

Both answer newest first (E2 by ``run_id``, E8 by ``generated``): ``limit`` is at most 500 a
page (the contract's floor for a server's ceiling), and the pages follow the ``cursor`` only
until the newest *limit* rows are in hand (:func:`_newest`).

**Operators: E16** (:func:`operators`): the names a project's runs recorded, deleted runs'
included, once per case folding, ascending by case folding, for the Operator proposals.
**Readings: E17** (:func:`readings`): the measurement names one test's runs recorded, each with
its unit, deleted runs' included, once per case folding, ascending by case folding, for the
Measurement proposals.

A row's ``folder`` names the server and the project (``<identity>/<project_id>``) and its
``path`` the item below it, laid out as a folder's (:func:`evidence_search.item_path`). Called
inside the store's operation (``ApiStore.search``), on its caller's worker. Qt-free.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from .evidence_api_run import board_key
from .evidence_index import REPORT, RUN, info_of_report
from .evidence_search import Filters, ResultRow, in_order, item_path, matches, serials_named

#: The rows asked for a page: the contract lets a server lower a larger ``limit`` to its own
#: ceiling, never below 500 (E2, E8).
PAGE = 500
#: A verdict as E2 and E8 take it: one of six fixed words, exactly.
VERDICTS = frozenset({"PASS", "FAIL", "ERROR", "ABORTED", "PARTIAL", "SKIP"})


def pages(
    store: Any, op: str, path: tuple[str, ...], query: dict[str, Any], rows: str
) -> Iterator[list[dict[str, Any]]]:
    """Each page of the paged search *op* (E2 or E8) asked with *query*, its *rows*, following
    the ``cursor`` until the answer says it is complete."""
    cursor = None
    while True:
        answer = store._call(
            op, "GET", *path, query={**query, **({"cursor": cursor} if cursor else {})}
        )
        yield [r for r in answer.data.get(rows) or [] if isinstance(r, dict)]
        cursor = answer.data.get("cursor")
        if not cursor:
            return


def artifacts(store: Any, project_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
    """E8, every page."""
    path = ("projects", project_id, "artifacts")
    return [a for page in pages(store, "listArtifacts", path, query, "artifacts") for a in page]


def runs(store: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    """E2, every page."""
    return [r for page in pages(store, "searchRuns", ("runs",), query, "runs") for r in page
            if r.get("run_id")]  # fmt: skip


def search(store: Any, project_id: str, flt: Filters, limit: int) -> list[ResultRow]:
    """The project's reports and runs *flt* names, the newest *limit*, in the contract's order."""
    folder = f"{store.io.identity}/{project_id}"
    serials: list[str | None] = list(serials_named(flt.board)) if flt.board is not None else [None]
    found: dict[tuple[str, str], ResultRow] = {}  # a report: one row per test it covers
    for serial in serials:
        runs_asked = ("searchRuns", ("runs",), run_query(project_id, flt, serial), "runs")
        for row in _newest(store, runs_asked, lambda run: _run_rows(run, folder), flt, limit):
            found.setdefault((row.path, row.test), row)
        if not flt.run_filters():
            path = ("projects", project_id, "artifacts")
            asked = ("listArtifacts", path, report_query(flt, serial), "artifacts")
            for row in _newest(store, asked, lambda art: _report_rows(art, folder), flt, limit):
                found.setdefault((row.path, row.test), row)
    return in_order(found.values())[:limit]


def operators(store: Any, project_id: str, ctx: Any = None) -> list[str]:
    """E16: every operator the project's runs recorded (deleted runs' included), once per case
    folding, ascending by case folding, as the server answers them; ``[]`` for a project the
    server never saw. On the caller's worker, inside the store's operation (*ctx*: the
    caller's ``OpContext``, or none)."""
    with store._op(ctx):
        answer = store._call("listOperators", "GET", "projects", project_id, "operators")
    found = answer.data.get("operators")
    return [name for name in found if isinstance(name, str)] if isinstance(found, list) else []


def readings(store: Any, project_id: str, test: str, ctx: Any = None) -> list[tuple[str, str]]:
    """E17: every reading the runs of *test* (the name they recorded, by case folding) recorded
    in the project, deleted runs' included, as ``(name, unit)`` (``""``: no unit recorded), once
    per case folding, ascending by case folding, as the server answers them; ``[]`` for a
    project the server never saw. On the caller's worker, inside the store's operation (*ctx*:
    the caller's ``OpContext``, or none)."""
    with store._op(ctx):
        answer = store._call(
            "listReadings", "GET", "projects", project_id, "readings", query={"test": test}
        )
    found = answer.data.get("readings")
    return [
        (item["name"], item["units"] if isinstance(item.get("units"), str) else "")
        for item in (found if isinstance(found, list) else [])
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
    ]


def run_query(project_id: str, flt: Filters, serial: str | None) -> dict[str, Any]:
    """E2's parameters for *flt* (a test reads them): every filter the contract offers."""
    query: dict[str, Any] = {"project_id": project_id, "deleted": flt.deleted}
    for name, value in (("serial", serial), ("part_number", flt.part_number), ("test", flt.test),
                        ("operator", flt.operator), ("machine_id", flt.machine_id)):  # fmt: skip
        if value is not None:
            query[name] = value
    if flt.verdict is not None and flt.verdict.upper() in VERDICTS:
        query["verdict"] = flt.verdict.upper()
    query.update(_days(flt))
    if flt.measured is not None:
        query["measurement"] = flt.measured.name
        if flt.measured.low is not None:
            query["value_min"] = repr(float(flt.measured.low))
        if flt.measured.high is not None:
            query["value_max"] = repr(float(flt.measured.high))
    return query


def report_query(flt: Filters, serial: str | None) -> dict[str, Any]:
    """E8's parameters for *flt* (a test reads them): a project's reports, with their records."""
    query: dict[str, Any] = {"kind": REPORT, "include": "info", "deleted": flt.deleted}
    for name, value in (("serial", serial), ("part_number", flt.part_number), ("test", flt.test)):
        if value is not None:
            query[name] = value
    if flt.verdict is not None and flt.verdict.upper() in VERDICTS:
        query["verdict"] = flt.verdict.upper()
    query.update(_days(flt))
    return query


def _days(flt: Filters) -> dict[str, str]:
    """``from``/``to``: the instants the app made of the days picked (local midnight of the
    first, local midnight after the last, :class:`evidence_search.Filters`), as every storage
    is asked them."""
    days = {}
    if flt.since:
        days["from"] = flt.since
    if flt.until:
        days["to"] = flt.until
    return days


def _newest(
    store: Any,
    asked: tuple[str, tuple[str, ...], dict[str, Any], str],
    rows_of: Callable[[dict[str, Any]], list[ResultRow]],
    flt: Filters,
    limit: int,
) -> list[ResultRow]:
    """The rows of the paged search *asked* (``(op, path, query, rows)``: E2 or E8, both newest
    first) that *flt* names, page by page until the newest *limit* are in hand."""
    op, path, query, key = asked
    kept: list[ResultRow] = []
    cursor = None
    while len(kept) < limit:
        page = {
            **query,
            "limit": min(PAGE, limit - len(kept)),
            **({"cursor": cursor} if cursor else {}),
        }
        answer = store._call(op, "GET", *path, query=page)
        for item in answer.data.get(key) or []:
            if isinstance(item, dict):
                kept.extend(row for row in rows_of(item) if matches(row, flt))
        cursor = answer.data.get("cursor")
        if not cursor:
            break
    return kept


def _run_rows(run: dict[str, Any], folder: str) -> list[ResultRow]:
    """A run's row (none for a run naming no id or test id)."""
    run_id, test_id = str(run.get("run_id") or ""), str(run.get("test_id") or "")
    if not run_id or not test_id:
        return []
    key = board_key(run)
    return [ResultRow(folder, key, str(run.get("test") or ""), RUN, run_id,
                      str(run.get("verdict") or ""), str(run.get("ended") or ""),
                      str(run.get("machine_id") or ""),
                      item_path(folder, RUN, key, test_id, run_id),
                      bool(run.get("deleted_at")), test_id)]  # fmt: skip


def _report_rows(artifact: dict[str, Any], folder: str) -> list[ResultRow]:
    """A report's rows: one per test its record names (none: one row), as a folder's index line
    is; none for a report naming no time, as a folder's index holds no line for one."""
    name = str(artifact.get("name") or "")
    info = artifact.get("info") if isinstance(artifact.get("info"), dict) else {}
    found = info_of_report(name, info)
    if not name or not found.generated:
        return []
    key = board_key(artifact)
    return [
        ResultRow(folder, key, test, REPORT, name, str(found.overall_verdict or ""),
                  str(found.generated), str(artifact.get("machine_id") or ""),
                  item_path(folder, REPORT, key, "", name), bool(artifact.get("deleted_at")))
        for test in (list(found.tests) or [""])
    ]  # fmt: skip
