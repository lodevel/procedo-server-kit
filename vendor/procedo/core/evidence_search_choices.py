"""What *Search Results*' fields propose, on every storage (``docs/plans/evidence_db_design.md``,
slice 12; the owner's decisions of 2026-11-07): an action of the evidence store
(``EvidenceStore.search_choices``, and ``search_readings`` for a test's readings), so each list is
complete, read from the storage itself, never only what earlier searches brought back.

* **What a storage proposes** it declares (``EvidenceStore.SEARCH_CHOICES``, these names): a
  field it does not declare is empty, never invented, and ``search_readings`` on a storage that
  records no reading is refused (``invalid``).
* **A folder, a share, FTPS, SFTP** (:data:`INDEX_CHOICES`): their index shards, as the search
  reads them (on a server through this PC's copy, :mod:`evidence_shard_cache`): the part numbers,
  the boards, the tests (the name each run recorded, each test a report names), the operators
  (one spelling per name, folded) and the benches (their machine id: a folder knows no label),
  of the items not deleted (:func:`choices_of`). No reading: the index records none.
* **The database** (:data:`DATABASE_CHOICES`): :mod:`evidence_db.search_choices`, each list
  walking its index, and a test's readings.
* **The web server** (:data:`WEB_CHOICES`): E4 (the boards and their part numbers), P4 (the
  project's tests: :data:`PROJECT_TESTS`, its test list, not the names runs recorded), E14
  (the benches the server has seen, with their labels), E16 (the operators the project's
  runs recorded) and E17 (a test's readings, :mod:`evidence_store_api_search`).

A reading is named by its number in its test (``{1}``, ``{2}``…): its name only means something
inside one test, so readings are proposed for one test only. Qt-free.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evidence_board import board_of
from .evidence_index import DELETED, RUN
from .evidence_search import records_in, rows_of
from .evidence_store import EvidenceError
from .evidence_tree import name_key

#: What a storage may propose (``EvidenceStore.SEARCH_CHOICES``). ``tests`` are the names runs
#: recorded and reports name; ``project_tests`` the project's test list instead (its tests'
#: names now); ``readings`` a test's readings (``search_readings``).
PART_NUMBERS, BOARDS, TESTS, PROJECT_TESTS = "part_numbers", "boards", "tests", "project_tests"
OPERATORS, BENCHES, READINGS = "operators", "benches", "readings"
INDEX_CHOICES = frozenset({PART_NUMBERS, BOARDS, TESTS, OPERATORS, BENCHES})
DATABASE_CHOICES = INDEX_CHOICES | {READINGS}
WEB_CHOICES = frozenset({PART_NUMBERS, BOARDS, PROJECT_TESTS, OPERATORS, BENCHES, READINGS})
NO_READINGS = "this storage records no reading to propose"


@dataclass(frozen=True)
class Choices:
    """What the search's fields propose. ``benches`` are ``(machine_id, label)`` (``""``: no
    label known); every list sorted."""

    part_numbers: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    operators: list[str] = field(default_factory=list)
    benches: list[tuple[str, str]] = field(default_factory=list)


def choices_of(records: Iterable[dict]) -> Choices:
    """What the index records *records* propose for the items not deleted: every part number,
    every board (as a person reads it, ``ResultRow.board_label``), every test, every operator
    (one spelling per name, folded), every bench (its machine id), sorted. The search's own
    reader, so a proposed value finds rows."""
    live = [rec for rec in records if not rec.get(DELETED)]
    rows = [row for rec in live for row in rows_of("", rec)]
    runs = [rec for rec in live if rec.get("kind") == RUN]
    operators = {name_key(o): o for rec in runs if isinstance(o := rec.get("operator"), str) and o}
    machines = {m for rec in runs if isinstance(m := rec.get("machine_id"), str) and m}
    return Choices(
        part_numbers=sorted({board_of(row.board).part_number for row in rows} - {""}),
        boards=sorted({row.board_label for row in rows}),
        tests=sorted({row.test for row in rows if row.test}),
        operators=sorted(operators.values(), key=str.casefold),
        benches=[(machine, "") for machine in sorted(machines)],
    )


def known_choices(folders: Iterable[str | Path]) -> Choices:
    """What bare folders' shards propose (:func:`choices_of`), read without a store."""
    return choices_of(rec for _folder, rec in records_in(folders))


def merged(answers: Iterable[Choices]) -> Choices:
    """Several storages' proposals as one: each value once, an operator once per name (folded),
    a bench once, with the label one of them knows."""
    every = list(answers)
    operators = {name_key(o): o for got in every for o in got.operators}
    benches: dict[str, str] = {}
    for got in every:
        for machine, label in got.benches:
            benches[machine] = benches.get(machine) or label
    return Choices(
        part_numbers=sorted({v for got in every for v in got.part_numbers}),
        boards=sorted({v for got in every for v in got.boards}),
        tests=sorted({v for got in every for v in got.tests}),
        operators=sorted(operators.values(), key=str.casefold),
        benches=sorted(benches.items(), key=lambda b: (b[1].casefold(), b[0])),
    )


def merged_readings(answers: Iterable[list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """Several storages' readings of one test as one list: one per name (folded), with the unit
    one of them carries, sorted by the folded name."""
    named: dict[str, tuple[str, str]] = {}
    for got in answers:
        for name, unit in got:
            held = named.get(name_key(name))
            if held is None or not held[1]:
                named[name_key(name)] = (name, unit)
    return [named[fold] for fold in sorted(named)]


def refuse_readings(store: Any) -> None:
    """Refuse (``invalid``) ``search_readings`` on a storage that records no reading."""
    if READINGS not in frozenset(getattr(store, "SEARCH_CHOICES", ())):
        raise EvidenceError("invalid", NO_READINGS)
