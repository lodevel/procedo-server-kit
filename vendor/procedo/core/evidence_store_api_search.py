"""The web server store's search actions (``EvidenceStore.search``, ``search_choices``,
``search_readings``), mixed into :class:`evidence_store_api.ApiStore`, which owns the calls
(``_call``) and the operation (``_op``) they run in, on the caller's worker.

* **The search**: E2 (runs) and E8 (reports), the server filtering (:mod:`evidence_api_search`);
  a measurement always with its test (:func:`evidence_search.refuse_unanswered`).
* **What the fields propose** (:data:`evidence_search_choices.WEB_CHOICES`): E4 lists the
  project's boards (their part numbers with them), P4 its tests (the project's test list: a
  project the server holds no files of has none), E14 the benches the server has seen, with
  their labels, E16 the operators the project's runs recorded
  (:func:`evidence_api_search.operators`), E17 a test's readings
  (:func:`evidence_api_search.readings`). A call the server does not offer (§6.1) is not sent:
  its field proposes nothing from this server, typing still works.

Qt-free.
"""

from __future__ import annotations

from typing import Any

from .evidence_api_run import board_key
from .evidence_board import board_of, label_of
from .evidence_search import RUN_FILTERS, Filters, ResultRow, refuse_unanswered
from .evidence_search_choices import WEB_CHOICES, Choices
from .evidence_store import NEWEST, EvidenceError, OpContext
from .evidence_tree import name_key
from .server_offer import BENCHES, OPERATORS, READINGS, TESTS


class ApiSearch:
    """The search actions of :class:`evidence_store_api.ApiStore`."""

    #: E2 filters runs by the operator, the bench and a measurement.
    SEARCH_RUN_FILTERS = RUN_FILTERS
    #: E4, P4, E14, E16 and E17: the boards and part numbers, the project's tests, the benches,
    #: the operators, a test's readings.
    SEARCH_CHOICES = WEB_CHOICES

    _call: Any
    _op: Any
    _ready: Any
    calls: frozenset[str]

    def search(
        self,
        project_id: str,
        filters: Filters,
        ctx: OpContext | None = None,
        *,
        limit: int = NEWEST,
    ) -> list[ResultRow]:
        """The project's runs (E2) and reports (E8) the filters name, the server filtering
        (:mod:`evidence_api_search`); a measurement always with its test."""
        from .evidence_api_search import search

        refuse_unanswered(self, filters)
        with self._op(ctx):
            return search(self, project_id, filters, limit)

    def search_choices(self, project_id: str, ctx: OpContext | None = None) -> Choices:
        """What the fields propose: E4's boards, P4's tests, E14's benches, E16's operators
        (one spelling per name, folded); each of P4, E14, E16 only from a server offering it."""
        from .evidence_api_search import operators as listed_operators

        with self._op(ctx):
            boards = self._call("listBoards", "GET", "projects", project_id, "boards").data
            tests = self._tests(project_id) if TESTS in self.calls else []
            benches = (
                self._call("listBenches", "GET", "benches").data if BENCHES in self.calls else {}
            )
        listed = listed_operators(self, project_id, ctx) if OPERATORS in self.calls else []
        operators = {name_key(o): o for o in listed if o}
        keys = [board_key(b) for b in boards.get("boards") or [] if isinstance(b, dict)]
        named = [(str(b.get("machine_id") or ""), str(b.get("label") or ""))
                 for b in benches.get("benches") or [] if isinstance(b, dict)]  # fmt: skip
        return Choices(
            part_numbers=sorted({board_of(k).part_number for k in keys} - {""}),
            boards=sorted({label_of(k) for k in keys}),
            tests=sorted(tests),
            operators=sorted(operators.values(), key=str.casefold),
            benches=sorted({b for b in named if b[0]}, key=lambda b: (b[1].casefold(), b[0])),
        )

    def _tests(self, project_id: str) -> list[str]:
        """The project's test names (P4); none for a project the server holds no files of."""
        try:
            found = self._call("listTests", "GET", "projects", project_id, "tests").data
        except EvidenceError as exc:
            if exc.kind == "not_found":
                return []
            raise
        return [str(t["test"]) for t in found.get("tests") or []
                if isinstance(t, dict) and t.get("test")]  # fmt: skip

    def search_readings(
        self, project_id: str, test: str, ctx: OpContext | None = None
    ) -> list[tuple[str, str]]:
        """E17: the readings *test*'s runs recorded (:func:`evidence_api_search.readings`);
        none from a server that does not offer E17."""
        from .evidence_api_search import readings

        with self._op(ctx):
            self._ready()
        return readings(self, project_id, test, ctx) if READINGS in self.calls else []
