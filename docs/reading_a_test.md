# Reading a Procedo test and its runs

For a server that reads the tests it stores (to know their steps) and their runs (to know how
each step went). Whether a server reads them at all is its own choice: see what a server must,
may and chooses in [the contract's §0](../procedo_server_contract.md). This guide says what the
other documents do not: which file to read, and how a step in a test meets its results in a run.

The references, copied from the Procedo engine as it ships them:

- [`Canonical_Text_Procedure_v2.md`](Canonical_Text_Procedure_v2.md): the grammar of the test text.
- [`Procedure_JSON_Schema_v2.md`](Procedure_JSON_Schema_v2.md): the format of `procedure.json`.
- [`procedure_v2.schema.json`](procedure_v2.schema.json): its JSON schema, the engine's base one.

The base schema knows only the engine's own steps and its `dmm` type, and refuses a test using
an instrument pack's. A project's bundle carries `rules/procedure_v2.schema.json`: the base
schema with its packs' parts merged in. Validate against that one.

## 1. Which file to read

Of a test's files ([contract §5.3](../procedo_server_contract.md)), read **`procedure.json`**:
the test parsed by the engine, every step numbered. `procedure_text.md` is the same test as its
author wrote it (for people), `test.py` the Python generated from `procedure.json` (what a bench
runs), `test.json` the test's id and format. A code test (`openhtf/`) has no `procedure.json`:
nothing here applies to it.

Read the `procedure.json` the run ran, not the test's current one: a run of a draft
(`draft: true`) keeps it among its own files as `definition/procedure.json`; any other run ran
its release `test_version` (P8b, the document `procedure.json`).

## 2. Steps and notes

```text
# VOUT_CHECK

## Title
VOUT at no load

## Meta
format_version: 2.0.1
board: BRD-MAIN
rules_pack: rules_packager_base@4.0.1
requirement: VOUT stays between 4.9 V and 5.1 V.
authored_by: J. Doe

## Equipment
DMM1 : dmm

## Steps
1. Connect DMM1 + to TP VOUT.
> Let the board warm up for one minute first.
2. Wait 60 s.
3. Operator: read DC voltage on TP VOUT with DMM1 as {1}.

## Expected
{1} in [4.9, 5.1] V
```

parses to (the `media` lists, pictures for the operator, left out):

```json
{
 "format_version": "2.0.1",
 "id": "VOUT_CHECK",
 "title": "VOUT at no load",
 "description": "",
 "meta": {
  "format_version": "2.0.1",
  "board": "BRD-MAIN",
  "rules_pack": "rules_packager_base@4.0.1",
  "requirement": "VOUT stays between 4.9 V and 5.1 V.",
  "authored_by": "J. Doe"
 },
 "equipment": [{"id": "DMM1", "type": "dmm"}],
 "steps": [
  {"op": "connect", "text": "DMM1 + to TP VOUT", "n": 1, "source": "operator"},
  {"op": "note", "text": "Let the board warm up for one minute first."},
  {"op": "wait", "duration": 60.0, "duration_unit": "s", "n": 2, "source": "operator"},
  {"op": "operator.read", "quantity": "DC voltage", "location": "TP VOUT", "ref": 1,
   "device": "DMM1", "n": 3, "source": "operator"}
 ],
 "expected": [{"kind": "range", "ref": 1, "lo_value": 4.9, "hi_value": 5.1, "unit": "V"}]
}
```

- A **step** is an entry with an `n`, unique in the whole test, branches and loops included.
  Expect `op`s you do not know: an instrument pack's are `<namespace>.<verb>`.
- A **note** is not a step: no `n`, it never runs and has no result.
- `meta.requirement` is the author's free text: never parse it. `meta.board` is the board as the
  author named it; the board a run tested is the run's own (`serial`, `part_number`,
  `board_revision`).

## 3. Macros are not steps

The `@` lines of the grammar (its "Macros" section) have no number and no result. In
`procedure.json` they are blocks holding their steps in `body`, never unrolled (the format's
"Macro nodes"); `@LET` leaves no node, only its value in `_macros.lets`. Two blocks the format
does not show yet: `@DO var` … `@WHILE cond` `@ENDDO` is
`{"op": "block_do", "var", "condition", "body"}`, and `@BREAK` is `{"op": "break"}`.

**The trap.** A step is not one thing that happens once. Inside a `@FOR` it runs once per turn;
inside a `@DO`, once or more; inside an `@IF` branch, maybe never; after a `@BREAK` or an
`@ABORT`, maybe never. How many times it ran is known only from the run (section 4). "One step,
one thing to validate" counts wrong as soon as a test holds a loop or a branch.

```text
# RAILS_CHECK

## Meta
format_version: 2.0.1
board: BRD-MAIN
rules_pack: rules_packager_base@4.0.1
requirement: Every rail is within its limits.
authored_by: J. Doe

## Equipment
DMM1 : dmm

## Steps
@LET settle = 2
@TABLE RAILS
@ROW RAILS net=V3V3 lo=3.2 hi=3.4
@ROW RAILS net=V5 lo=4.9 hi=5.1
@ENDTABLE
@ALLOC R START=10 COUNT=COUNT(RAILS)
1. Operator: switch the board on.
@FOR i, row IN RAILS
2. Wait ${settle} s.
3. Operator: read DC voltage on TP ${row.net} with DMM1 as {R + i}.
@ENDFOR
@IF {10} < 3.0 V
4. Operator: judge the power LED (target = "off") as {2}.
@ELSE
5. Operator: read DC current on TP IIN with DMM1 as {3}.
@ENDIF
@DO k
6. Operator: press the reset button.
7. Operator: read DC voltage on TP VRST with DMM1 as {4}.
@WHILE {4} < 1.0 V
@ENDDO

## Expected
@FOR i, row IN RAILS
{R + i} in [${row.lo}, ${row.hi}] V
@ENDFOR
{3} < 0.5 A
{4} >= 1.0 V
```

Its `steps` (`media` left out): `${…}` and the ref `R + i` stay as written.

```json
[
 {"op": "block_table", "name": "RAILS", "rows": [{"net": "V3V3", "lo": 3.2, "hi": 3.4},
                                                  {"net": "V5", "lo": 4.9, "hi": 5.1}]},
 {"op": "block_alloc", "name": "R", "start": 10, "count_expr": "COUNT(RAILS)"},
 {"op": "operator.prompt", "text": "switch the board on", "n": 1, "source": "operator"},
 {"op": "block_for", "iter": {"kind": "table", "table": "RAILS"}, "vars": ["i", "row"], "body": [
   {"op": "wait", "duration": "${settle}", "duration_unit": "s", "n": 2, "source": "operator"},
   {"op": "operator.read", "quantity": "DC voltage", "location": "TP ${row.net}", "ref": "R + i",
    "device": "DMM1", "n": 3, "source": "operator"}]},
 {"op": "block_if", "branches": [
   {"condition": "{10} < 3.0 V", "body": [
     {"op": "operator.judge", "description": "the power LED", "target": "off", "ref": 2, "n": 4,
      "source": "operator"}]},
   {"condition": null, "body": [
     {"op": "operator.read", "quantity": "DC current", "location": "TP IIN", "ref": 3,
      "device": "DMM1", "n": 5, "source": "operator"}]}]},
 {"op": "block_do", "var": "k", "condition": "{4} < 1.0 V", "body": [
   {"op": "operator.prompt", "text": "press the reset button", "n": 6, "source": "operator"},
   {"op": "operator.read", "quantity": "DC voltage", "location": "TP VRST", "ref": 4,
    "device": "DMM1", "n": 7, "source": "operator"}]}
]
```

Seven steps; step 4 and step 5 are never both run. Every numbered step, with the blocks around
it:

```python
def numbered_steps(nodes, around=()):
    """(step, the ops of the blocks around it) for every numbered step of ``nodes``."""
    for node in nodes:
        op = node.get("op")
        if op in ("block_for", "block_do"):
            yield from numbered_steps(node["body"], around + (op,))
        elif op == "block_if":
            for branch in node["branches"]:
                yield from numbered_steps(branch["body"], around + (op,))
        elif "n" in node:
            yield node, around
```

## 4. Each step's results in a run

A run's results are its `phases` ([contract §5.5–§5.7](../procedo_server_contract.md)). For a
text test, the engine writes them so:

- **One phase per step run**, named `Step <n>`; in a loop each turn is a phase of its own, named
  with the loop's variables that turn: `Step <n> (<var>=<value>, …)`. A step that did not run
  has no phase; notes and `@` lines never have one. A run that ended early may end with a phase
  `Not reached` holding the checks it never got to.
- A **reading** (no `validators`) is named by its key (`10`, `Vout`, `Vrail[3]`). A **check**
  (with `validators`) is one criterion of `expected`, named by its line with the loop values
  filled in (`{10} in [3.2, 3.4] V`); it sits on the phase where its last reading was taken; a
  criterion over a range (`{1..3} < 5 V`) is one check per reading.
- A phase is `FAIL` when a check on it failed or could not be computed, `ERROR` when the step's
  own action failed, else `PASS`: "ran, nothing judged on it failed", not "checked".

The phases of a real run of RAILS_CHECK (times left out): steps 2 and 3 ran once per row, step 4
never, steps 6 and 7 twice; `{4} >= 1.0 V` judged only the last value of `{4}`, on the last turn.

```json
[
 {"ord": 1, "name": "Step 1", "attempt": 1, "outcome": "PASS", "measurements": []},
 {"ord": 2, "name": "Step 2 (i=0, row={'net': 'V3V3', 'lo': 3.2, 'hi': 3.4})", "attempt": 1, "outcome": "PASS", "measurements": []},
 {"ord": 3, "name": "Step 3 (i=0, row={'net': 'V3V3', 'lo': 3.2, 'hi': 3.4})", "attempt": 1, "outcome": "PASS", "measurements": [
   {"name": "10", "ord": 1, "value_num": 3.31, "units": "V", "outcome": "PASS"},
   {"name": "{10} in [3.2, 3.4] V", "ord": 2, "value_num": 3.31, "units": "V", "outcome": "PASS",
    "validators": ["3.2 <= x <= 3.4"], "limit_low": 3.2, "limit_high": 3.4}]},
 {"ord": 4, "name": "Step 2 (i=1, row={'net': 'V5', 'lo': 4.9, 'hi': 5.1})", "attempt": 1, "outcome": "PASS", "measurements": []},
 {"ord": 5, "name": "Step 3 (i=1, row={'net': 'V5', 'lo': 4.9, 'hi': 5.1})", "attempt": 1, "outcome": "PASS", "measurements": [
   {"name": "11", "ord": 3, "value_num": 5.02, "units": "V", "outcome": "PASS"},
   {"name": "{11} in [4.9, 5.1] V", "ord": 4, "value_num": 5.02, "units": "V", "outcome": "PASS",
    "validators": ["4.9 <= x <= 5.1"], "limit_low": 4.9, "limit_high": 5.1}]},
 {"ord": 6, "name": "Step 5", "attempt": 1, "outcome": "PASS", "measurements": [
   {"name": "3", "ord": 5, "value_num": 0.12, "units": "A", "outcome": "PASS"},
   {"name": "{3} < 0.5 A", "ord": 6, "value_num": 0.12, "units": "A", "outcome": "PASS",
    "validators": ["x < 0.5"], "limit_high": 0.5}]},
 {"ord": 7, "name": "Step 6 (k=0)", "attempt": 1, "outcome": "PASS", "measurements": []},
 {"ord": 8, "name": "Step 7 (k=0)", "attempt": 1, "outcome": "PASS", "measurements": [
   {"name": "4", "ord": 7, "value_num": 0.4, "units": "V", "outcome": "PASS"}]},
 {"ord": 9, "name": "Step 6 (k=1)", "attempt": 1, "outcome": "PASS", "measurements": []},
 {"ord": 10, "name": "Step 7 (k=1)", "attempt": 1, "outcome": "PASS", "measurements": [
   {"name": "4", "ord": 8, "value_num": 1.8, "units": "V", "outcome": "PASS"},
   {"name": "{4} >= 1.0 V", "ord": 9, "value_num": 1.8, "units": "V", "outcome": "PASS",
    "validators": ["1.0 <= x"], "limit_low": 1.0}]}
]
```

**Did step n pass, each time it ran:**

```python
import re

STEP = re.compile(r"Step (\d+)(?: \((.*)\))?")


def executions(run):
    """(n, the turn's text, ord, outcome) for every phase of a step, in the order run."""
    for phase in run["phases"]:
        found = STEP.fullmatch(phase["name"])
        if found:
            yield int(found[1]), found[2] or "", phase["ord"], phase.get("outcome")
```

Link a phase to its step by this number, never by a reading's key: a `ref` may be an
expression (`R + i` was `10`, then `11`).

**Telling the turns of a loop apart.** The run's JSON has no field for the step number or the
loop's values: only the phase's `ord` and the turn's text in its `name`. That text is for
people (each value as the engine's Python prints it: a table row is a whole dict), so do not
parse values out of it.

- Two turns with the same values have the same name: `@FOR j IN [7, 7]` gives `Step 3 (j=7)`
  twice, `attempt` 1 then 2. In a text run, `attempt` above 1 means "the same step again with
  the same loop values", not a retry. Only `ord` tells them apart.
- One step run once can give several phases: a guided run (the operator walks the test in the
  app) gives a step reading `{1..3}` over `TP VMONI[1..3]` one phase per reading, `Step 1`
  three times, `attempt` 1, 2, 3; a run of `test.py` gives it one phase holding the three.

The loop's values as data are in the run's record `results.json` (role `record`): its
`phases[k]`, the phase of `ord` `k + 1`, carries
`"procedo": {"step": 3, "loop": {"i": 1, "row": {"net": "V5", "lo": 4.9, "hi": 5.1}}, "text": "…"}`
(no `loop` outside a loop). A server holds those bytes only when the run's files are on an `api`
storage. Two turns with the same values have the same `loop` there too.
