# Procedure JSON Schema v2.0.2

JSON shape produced by parsing canonical text and consumed by codegen. Macros (`@FOR`, `@IF`, `@TABLE`, `@ALLOC`) are **preserved as structured nodes** — not pre-expanded — so codegen can emit native Python `for`/`if` blocks and `_macros` round-trips cleanly.

## Top-level shape

```json
{
 "format_version": "2.0.1",
 "id": "<TEST_ID>",
 "title": "<optional one-line display name; omitted when empty>",
 "description": "<description text; may be empty or contain `\n` for multi-line>",
 "meta": { ... },
 "equipment": [ ... ],
 "steps": [ ... ],
 "expected": [ ... ],
 "_macros": { ... }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `format_version` | string | yes | Preserved format version (descriptive; not a pass/fail gate). Value is `"2.0.1"` for all 2.0.x procedures. |
| `id` | string | yes | Test ID, matches `^[A-Z][A-Z0-9_-]*$`, ≤ 64 chars. Equals header `# <id>`. |
| `title` | string | optional | Single line from `## Title`, any characters (accents, `°C`, `µ`, `±`, `≤`) but a tab, a control one (C0, DEL, C1 with NEL) or a line / paragraph separator (U+2028, U+2029); present only when non-empty. GUI shows `title (id)` when it differs from `id`. |
| `description` | string | yes | Description, any characters (accents, `°C`, `µ`, `±`, `≤`) but a tab, a control one (C0 but `\n`, DEL, C1 with NEL) or a line / paragraph separator (U+2028, U+2029); may be empty or contain embedded `\n` for multi-line. Mirrors header lines after `# <id>`. |
| `meta` | object | yes | See `## meta`. |
| `equipment` | array | yes | See `## equipment`. May be empty. |
| `steps` | array | yes | See `## steps`. May be empty. |
| `expected` | array | yes | See `## expected`. May be empty. |
| `_macros` | object | optional | Compiled macro definitions (`tables`, `allocations`, `lets`); present iff the source uses macros. |

## meta

```json
{
 "board": "<board-id>",
 "rules_pack": "rules_packager_base@<version>",
 "<pack key>": "<name>@<version>",
 "requirement": "<text>",
 "authored_by": "<text>"
}
```

`board`, `rules_pack` required unconditionally. Each pack's pin (`<pack key>`, named in that pack's doc) is required iff at least one of its equipment types is declared. `requirement`, `authored_by` optional. Additional bench-private keys allowed and preserved verbatim.

## equipment

Array of equipment instances. Each item:

```json
{
 "id": "<EQUIPMENT_ID>",
 "type": "dmm" | "<a pack's type>",
 "subtype": "<subtype>",       // a type with variants only
 ...                           // the rest of a pack's type: its doc and schema fragment
}
```

### Per-type shapes

**dmm** (the base's own type):
```json
{ "id": "DMM_GENERIC", "type": "dmm" }
```

No channels (single-probe). Optional `model_hint` (≤ 200 characters).

**A pack's type**:
```json
{ "id": "<EQUIPMENT_ID>", "type": "<type>", "subtype": "<subtype>", ... }
```

`subtype` required when the type has variants. Every other field (channels, ratings with their paired `_unit`) is the pack's schema fragment's (`equipment_oneOf_refs`); the pack's doc shows the shape.

## steps

Array of op nodes. Each op carries:

| Field | Type | Notes |
|---|---|---|
| `n` | integer | 1-based step number, globally unique across the entire procedure (including all macro branches). `0` allowed for legacy compatibility. |
| `op` | string | Discriminator (see "Op enumeration" below). |
| `source` | string | One of `"operator"`, `"inserted"`. Inserted ops carry a `rationale` string. |
| (op-specific fields) | — | Per op below. |

Macros (`block_for`, `block_if`, `block_table`, `block_alloc`) are also valid step entries (see "Macro nodes" below).

### Op enumeration

| `op` | Verb | Driver |
|---|---|---|
| `connect` | `Connect` | universal |
| `disconnect` | `Disconnect` | universal |
| `move` | `Move` | universal |
| `wait` | `Wait` | universal |
| `operator.read` | `Operator: read ... as {N}` | universal |
| `operator.read_diff` | `Operator: read <quantity> between <A> and <B> with <ID> as {N}` | universal |
| `operator.judge` | `Operator: judge ... as {N}` | universal |
| `operator.prompt` | `Operator: <free-text>.` | universal |
| `timestamp` | `Timestamp as {N}.` | universal |
| `bind` | `{N} = <expr> [<unit>].` | universal |
| `call` | `Call <module>.<function>(<arg>, ...)[ (timeout = <t> <unit>)][ as <out>, ...].` | universal |
| `<namespace>.<verb>` | per the pack's doc | the pack owning the type |
| `note` | `> <text>` | universal |

### Per-op shape highlights

**`connect`**: `{n, op:"connect", text:"<verbatim>", source}`. Free text preserved. `disconnect` and `move` have the same shape.

**`wait`**: `{n, op:"wait", duration:float | "${name}", duration_unit:"s"|"ms"|"us"|"µs"|"ns", source}`. Operator-as-written value + unit; not SI-normalized. `duration` is polymorphic: a float for a literal, or a substitution-expression string preserved verbatim for codegen (`"${t}"` from `Wait ${t} s.`).

**`operator.read`**: `{n, op:"operator.read", quantity:"DC voltage"|"AC voltage"|"DC current"|..., location:"<text>", device:"<DMM_ID>", ref:int, source}`.

**`operator.read_diff`**: `{n, op:"operator.read_diff", quantity:"DC voltage"|"AC voltage"|"DC current"|"AC current"|"resistance", location_a:"<text>", location_b:"<text>", device:"<DMM_ID>", ref:int, source}`.

**`operator.judge`**: `{n, op:"operator.judge", description:"<text>", target:"<text>", ref:int, source}`.

**`operator.prompt`**: `{n, op:"operator.prompt", text:"<free-text>", source}`.

**`timestamp`**: `{n, op:"timestamp", ref, source}`.

**`bind`**: `{n, op:"bind", ref, expr:<BindExpr>, unit?:"<unit>", source}`. `expr` is an `expr` criterion's side with no unit on a number, plus `{"kind": "name", "name": "i"}` for `${i}` / `${row.key}`, `{"kind": "count", "table": "T"}` for `COUNT(T)` and `{"kind": "aggregate", "func": "mean", "ref": {"kind": "range", "lo": 1, "hi": 3}}` (`"func": "rms"` too; over a name: `"ref": "Vrail[*]"`); `unit`, of the one unit table, is the value's; a minus sign before a ref, a name or a group is `{"kind": "neg", "arg": <node>}`.

**`call`**: `{n, op:"call", function, args:[{value, unit?, name?}], timeout?, timeout_unit?, outputs?:[{ref, unit?}], source}`. `value` is a Bind node; a name range is a `ref` (`"Vrail[*]"`), `TRUE` a `lit` `true`. `timeout` is a float or a `"${name}"`, with `timeout_unit` a time unit (`"s"|"ms"|"us"|"µs"|"ns"`). An output `ref` is an int or the text written (`"H[1..10]"`); `unit` is what the function returns. Keys appear only when written.

**A pack's op** (`<namespace>.<verb>`): `{n, op:"<namespace>.<verb>", device:"<ID>", ..., source}`; its fields are its schema fragment's (`step_oneOf_refs`), shown in the pack's doc. A value field is polymorphic as `wait.duration` is and carries a paired `_unit`; a field holding a reading is named `ref`, `sent_ref` or `received_ref`.

### `note` step

**`note`**: `{op:"note", text:"<text>"}`. No `n` field; preserved in source order between numbered steps.

### Macro nodes inside `steps[]`

**`block_for`** (a `@FOR ... @ENDFOR` block):
```json
{
 "op": "block_for",
 "iter": { "kind": "range", "start": 0, "stop": 7 },                     // integer range (a bound: a number or a name as written, "N")
 // or:    { "kind": "range_float", "start": 0.0, "stop": 5.0, "step": 0.5 }
 // or:    { "kind": "list", "values": [1, "\"a\"", "1-2"] }   // a number, or a text as written (quotes kept)
 // or:    { "kind": "table", "table": "EN_MAP" }
 // or:    { "kind": "name", "name": "PIN_COUNT" }
 "vars": ["i"] | ["i", "row"],     // single var; or [index, value] for table, range, range_float, or list iter
 "body": [ ...nested step nodes... ]
}
```

**`block_if`** (`@IF ... @ELIF ... @ELSE ... @ENDIF`):
```json
{
 "op": "block_if",
 "branches": [
   { "condition": "<cond-expr>", "body": [ ... ] },         // @IF
   { "condition": "<cond-expr>", "body": [ ... ] },         // @ELIF (zero or more)
   { "condition": null, "body": [ ... ] }                   // @ELSE (optional)
 ]
}
```

**`block_table`** (`@TABLE ... @ENDTABLE`):
```json
{
 "op": "block_table",
 "name": "EN_MAP",
 "rows": [
   { "idx": 0, "loc": "\"P10 pin 20\"", "net": "EN_0" },   // a cell: a number, or its text as written
   ...
 ]
}
```

**`block_alloc`** (`@ALLOC <name> START=<expr> COUNT=<expr>`):
```json
{
 "op": "block_alloc",
 "name": "EN_BASE",
 "start": 1,                               // digits: an int; a calculation: its text as written ("K / 2")
 "count_expr": "PER_PIN*COUNT(EN_MAP)"     // expression preserved as text; resolved at codegen
}
```

**`abort`** (`@ABORT`):
```json
{ "op": "abort" }
```

**Note**: `@LET` does NOT appear as a step node — it is collected into `_macros.lets` and substituted inline.

### Refs and arithmetic expressions

Where a step has a ref field (`ref`, `sent_ref`, `received_ref`, or a pack op's other ref field its doc names), the value can be:
- An integer (literal): `42`
- An expression string: `"EN_BASE + PER_PIN*i + 0"` — resolved at codegen against `_macros.lets`, `_macros.allocations`, and enclosing loop variables.

The schema accepts both `int` and `string` for these fields. A string of `@LET` / `@ALLOC` names or loop variables is arithmetic, resolved by codegen; any other is a name, as written (`"Vrail[i + 1]"`).

## expected

Array of criteria. Each item:

```json
{
 "kind": "<criterion-kind>",
 "ref": <int | string | range>,
 ... kind-specific fields ...
}
```

### Kind enumeration

| Kind | Form | Fields |
|---|---|---|
| `eq` | `{1} = 28.0 V`, `{1} = TRUE` | `target_value` (`true` for `TRUE`), `target_unit?` |
| `eq_text` | `{1} = "OK"` | `target_text` |
| `contains` | `{1} contains "0x4B"` | `target_text` |
| `contains_any` / `contains_all` | `{2} contains one of ["0x48", "0x4B"]` / `contains all of [...]` | `texts: [str, ...]` (2+) |
| `eq_digital` | `{1} = '1'` | `target_value (0\|1)` |
| `lt` | `{3} < 5.0 V` | `limit_value, limit_unit?` |
| `lte` | `{5} <= 50 degC` | `limit_value, limit_unit?` |
| `gt` | `{4} > 0 mA` | `limit_value, limit_unit?` |
| `gte` | `{4} >= 0 mA` | `limit_value, limit_unit?` |
| `range` | `{6} in [27.0, 29.0] V`, `{6} not in ]1, 5[` | `lo_value, hi_value, unit?, lo_open?, hi_open?, negated?` (each flag `true`, present only when set) |
| `tolerance_abs` | `{7} = 28.0 V +/- 0.5 V`, `{7} = 0x10 +/- 2` | `nominal_value, unit, tol_value, tol_unit` (`unit` and `tol_unit` both set or both `null`) |
| `tolerance_pct` | `{8} = 28.0 V +/- 5 %`, `{8} = 100 +/- 5 %` | `nominal_value, unit (null when none), tol_pct` |
| `one_of` | `{1} one of [0x01, 0x02, 0x04]`, `{1} one of [1.5, 2.5] V` | `values: [{value, literal?}, ...]` (2+), `unit` |
| `one_of` (text) | `{2} one of ["OK", "DONE"]` | `texts: [str, ...]` (2+) |
| `regex_match` | `{1} matches /^OK\d+$/` | `pattern` |
| `operator_decision` | `{9} operator decides "OK"` | `verdict_text` |
| `diff` | `{a} - {b} <= 50 degC` | `lhs_ref, rhs_ref, op, limit_value, limit_unit?` (op = `=`, `<`, `<=`, `>`, `>=`) |
| `monotonic` | `{100..132} monotonic increasing` | `mode: "increasing"\|"decreasing"\|"strictly_increasing"\|"strictly_decreasing"` |
| `aggregate` | `mean({100..132}) <= 30.0 degC`, `rms({Vrail[*]}) in [4, 5] V` | `func: "mean"\|"min"\|"max"\|"sum"\|"count"\|"rms"`, `range_lo, range_hi` or `ref` (`"Vrail[*]"`), `op, limit_value, limit_unit?` (op = `=`, `<`, `<=`, `>`, `>=`, `in`; `in` has `lo_value, hi_value` and the `range` flags instead of `limit_value`) |
| `expr` | `{1} <= {2} + 0.8 V` (general ref-arithmetic), `({1} >> 8) & 0x1F = 0x04` | `lhs, op, rhs` (a unit only on a `lit`; lhs/rhs are ExprNode AST: ref \| lit \| binop \| abs \| neg \| str \| char; binop `op` is `+ - * /` or, on integers, `& \| << >>`; `abs({1} - {2})` is `{"kind": "abs", "arg": <node>}`, `-{1}` `{"kind": "neg", "arg": <node>}`) |
| `expr` (op `in`) | `{3} / 256 in [20, 30]` (range on arithmetic) | `lhs, op: "in", lo_value, hi_value, unit?` (bounds and flags as `range`) |
| `logic` | `{1} < 1 or {1} > 2`, `({1} < 1 or {1} > 2) and {2} = "OK"` | `op: "or"\|"and", parts: [<criterion>, ...]` (2+, any kind; same-`op` nesting flattened). |
| `template` | `{BASE + i} = ${V0 + K * i} V +/- 5 %` (a line with a `${...}`) | `text`: the line as written |

A number keeps the type it was spelled with (`5` → `5`, `5.0` → `5.0`, exact at any size); a hex spelling keeps its integer in the value field and the spelling in a sibling `HexLiteral` field — `target_literal`, `limit_literal`, `lo_literal` / `hi_literal`, `nominal_literal`, `tol_literal`, `tol_pct_literal`, ExprNode and `one_of` value `literal` (`{1} = 0x1A00` → `target_value: 6656, target_literal: "0x1A00"`), present only when the text had `0x`. A unit-less `limit_unit` / `unit` is `null`.

### `ref` field on criteria

- Single ref: `42` or `"EN_BASE + i"` (string expression).
- Range ref: `{ "kind": "range", "lo": 100, "hi": 102 }` for `{100..102}`.
- Aggregate criteria use `range_lo`/`range_hi` (both can be expressions).

### Macro nodes inside `expected[]`

`expected[]` accepts the same `block_for` / `block_if` / `block_table` / `block_alloc` nodes as `steps[]`. Codegen emits per-iteration criteria.

## `_macros` block (top-level provenance)

```json
{
 "lets": { "PER_PIN": 6, "fail_temp": 110 },
 "tables": { "EN_MAP": [ {row}, ... ] },          // duplicates `block_table.rows` for fast lookup
 "allocations": { "EN_BASE": { "start": 1, "count_expr": "PER_PIN*COUNT(EN_MAP)" } }
}
```

Optional. Present iff the source uses `@LET`, `@TABLE`, or `@ALLOC`. Codegen reads this map to resolve `${var}` substitutions and `{<expr>}` ref arithmetics.

## Step ordering and renumbering

Steps appear in source order. `n` values are globally unique (no duplicate `n` across branches of `block_if` or iterations of `block_for`). Renumbering happens at canonical-text emit time, not in JSON.

## Round-trip identity

The text → JSON → text path round-trips byte-exact (modulo whitespace normalization). The text → JSON → code path is forward-only; no inspector.

## Validator codes (cross-cutting)

Codes emitted by the parser/validator on rule violations. This list is informational; the parser implementation is the source of truth.

| Code | Meaning |
|---|---|
| `GRAM_HEADER_INVALID` | Header line 1 malformed (not `# <TEST_ID>`, or a test ID off its form). |
| `GRAM_META_REQUIRED_MISSING` | Required Meta key absent. |
| `GRAM_PACK_VERSION` | Pack version not `<name>@<MAJOR>.<MINOR>.<PATCH>`. |
| `REF_DUPLICATE` | One reading bound by two steps each static turn reaches (no `@IF` branch, `@DO` or earlier `@BREAK`): the same `{N}`, `{B}` with `B = 5` and `{5}`, `{10 + i}` and `{12}` or `{11 + i}`, two loops overlapping, an `@ALLOC`'s and a literal. Steps in branches or `@DO`s may each bind it. |
| `REF_DANGLING` | A criterion tests a ref no step binds (a step in any branch counts); for a screenshot's `for {X}` it is a warning. |
| `REF_FORM` | A ref the engine cannot read (`{10 +}`, `{Vrail${i}}`, `{Vrail [i]}`, a name over 32 characters), wherever it is; `as {1`, `as {1} V`, `as 1}`; one it computes to no reading's number from whole numbers, turn by turn: `{${v}}` (a ref names its variable bare), `{i ** 2}`, `{NOPE + i}`, `{i / 2}` at an odd `i`, `{i - 1}` at `i = 0`, `{F}` with `F = 2.5`, `as {}` — in a static turn that reaches its step (no `@IF` branch, `@DO` or earlier `@BREAK`); `[*]` that binds or names a screenshot (`for {Vrail[*]}`); a step's range not of whole numbers, or on a step other than `Operator: read` or `Call` (a pack step's `as {1..3}`). |
| `REF_NAME_TAKEN` | A name that is a loop variable outside its loop, a `@TABLE`, a function in any case (`{mean}`), or a macro as a stem (`{K[1]}`). |
| `REF_NAME_SHAPE` | A name used both plain and indexed (`{Vout}`, `{Vout[2]}`). |
| `GRAM_DIGIT` | A digit not 0-9 (`٣`, `１`) in a number where one is read — never in a text: a word (`x١`), a quoted text, an Expected `# comment` or `/regex/`, an `Operator:` step's words. |
| `EXP_FORM_UNKNOWN` | An Expected line matching no criterion form (the message says what is wrong). |
| `EXP_TEXT_ON_NUMBER` | A text criterion (`contains`, `contains one of / all of`, `matches`, `= "..."`, `one of ["..."]`, inside `or` / `and` too) on a ref a step binds as a number (a copy Bind binds what its source binds), or on a typed answer, or a copy of one, read as a number elsewhere. |
| `EXP_NUMBER_ON_TEXT` | A number criterion or condition (`<`, `in`, arithmetic, bit operations; a condition's `==` / `!=` compares any two values) on a ref every step that can bind it binds as a text: a device reply (`received as`), a text `@LET` or cell, a copy of one (one finding a line). |
| `EXP_BITS_ON_DECIMAL` | A bit operation (`& \| << >>`) on a ref a step binds as a decimal reading (`Operator: read`, a pack's measured reading, `Timestamp`) or a Bind computes to a fraction (`{2} = 2.5.`), or on a side of numbers alone that is no whole number (`2.5`, a `@LET`'s, a loop turn's). |
| `EXP_RANGE_INVERTED` | A range holding no number: bounds `[5, 1]`, `]5, 5]`, a ref range `{5..1}`, or one a loop turn computes so (`mean({10..i})` at `i = 1`). |
| `CALL_FORM` | A `Call` off its form: spacing (`f(2,5)`, `50mV`), `true`, a `_` or keyword name, arithmetic in an argument, an unknown or per-division unit. |
| `CALL_FUNCTION_UNKNOWN` | A `Call` names no function in `functions/` (warning). |
| `CALL_ARGS` | A `Call` passes arguments its function does not take (warning). |
| `CALL_IMPORT_MISSING` | A function file imports a package the bundle lacks (warning). |
| `CALL_UNIT_MISMATCH` | A `Call` output's unit differs from the unit its function declares (warning). |
| `MAC_BLOCK_UNCLOSED` | `@FOR`/`@IF`/`@TABLE` not closed. |
| `MAC_BREAK_OUTSIDE_LOOP` | `@BREAK` not inside a `@FOR` or a `@DO`. |
| `MAC_VAR_UNBOUND` | A `${...}` or a `@LET` reading a name no `@LET`, `@ALLOC`, `@TABLE` or loop binds there (a column no row of its table has; one only some rows have is a run's). |
| `MAC_SUBST_FORM` | A `${...}` that is no name nor calculation, or gives no number (a text, `${K / 0}`); in a step's text, a warning. |
| `MAC_COND_FORM` | An `@IF` / `@ELIF` / `@WHILE` condition outside the condition grammar (the message says what). |
| `BIND_EXPR_INVALID` | A Bind whose right-hand side is no expression (the message says what). |
| `BIND_UNIT_UNKNOWN` | A Bind's unit is no known unit. |
| `BIND_ON_TEXT` | A Bind computes with a ref a step binds as text — a copy of a text too (copy it as it is instead). |
| `REF_READ_BEFORE_BOUND` | A Bind, a condition or a `Call` argument reads a literal ref or `[*]` no step before it, nor one in a loop around it, can bind (none for a ref tested `bound` / `unbound`). |
| `MAC_LET_FORM` | A `@LET` value written as a calculation the engine refuses (`2 ** 3`, `K -`, `K & 3 + 1`, `{1} + 1`, `K ^ 2`, `max(K, 1)`) or cannot compute (`K / 0`), a malformed quoted text, or a name already defined (a `@LET`, `@TABLE`, `@ALLOC`), or a function's (`abs`, `max`). |
| `MAC_ALLOC_FORM` | An `@ALLOC` whose `COUNT=`, cast to an integer (its fraction dropped), is below 0 or no number, or whose name is already defined or a function's. |
| `MAC_ROW_FORM` | A malformed `@ROW`: pairs not apart by spaces (`a=1 junk`, `a=1, b=2`), a value past its closing quote (`a="x"y`), a value missing (`a=- 5`), a key twice or no identifier (`a-b=1`). |
| `MAC_FOR_ITER` | A `@FOR` over nothing it can walk: two variables over no `@TABLE` declared before it, or a malformed list (`[1,]`, `[ ]`, `[1]]`). |
| `MAC_FOR_FORM` | A `@FOR` line not `@FOR <var> IN <iterable>` or `@FOR <i>, <row> IN <table>`, or a variable another name already is (a `@LET`, `@TABLE`, `@ALLOC`, an enclosing loop's, the header's own: `@FOR i, i`) or a function's. |
| `MAC_FOR_RANGE_DIR` | A `@FOR` float range whose `STEP` is 0 or runs away from its end. |
| `MAC_DO_FORM` | A `@DO` line not `@DO <var>`, or a variable another name already is, or a function's. |
| `MAC_DO_NO_WHILE` | A `@DO` block with no `@WHILE` before its `@ENDDO`. |
| `MAC_DO_DUPLICATE_WHILE` | A `@DO` block with two `@WHILE`s. |
| `MAC_DIR_FORM` | A macro directive not `@UPPERCASE args` at column 0 (`@for`: its block is skipped, one finding), or text after one taking none (`@ENDFOR i`, `@ELSE x`). |
| `MAC_STRAY_DIRECTIVE` | A closing or continuing directive (`@ENDIF`, `@ELSE`, `@ENDFOR`, ...) with no opener. |
| `MAC_TABLE_NAME` | A `@TABLE` name that is no identifier, already defined, or a function's. |
| `MAC_TABLE_BODY` | A line other than `@ROW` inside `@TABLE`. |
| `MAC_ROW_MISMATCH` | A `@ROW` naming another table than its `@TABLE`. |
| `WAIT_UNIT` | A `Wait` not a number (`.` decimals) or a `${name}`, and a time unit. |
| `TIMESTAMP_FORM_INVALID` | A `Timestamp` not `Timestamp as {<ref>}`. |
| `GRAM_UNKNOWN_METHOD` | A step no verb reads (`Operator:read`, with no space, too). |
| `GRAM_LINE_UNRECOGNIZED` | A line of `## Steps` or `## Expected` no form reads. |
| `GRAM_UNKNOWN_SECTION` | A `## ` heading of no known section. |
| `GRAM_ORPHAN_LINE` | A line outside any section. |
| `GRAM_TITLE_CONTROL` | A `## Title` holding a tab, a control character (C0, DEL, C1) or a line / paragraph separator (U+2028, U+2029). |
| `GRAM_TITLE_MULTILINE` | A `## Title` over more than one line (warning: the rest is ignored). |
| `GRAM_DESCRIPTION_CONTROL` | A `## Description` (or free text under `# <id>`) holding a tab, a control character (C0, DEL, C1) or a line / paragraph separator (U+2028, U+2029). |
| `GRAM_HEADER_FREETEXT_WITH_DESC` | Free text under `# <id>` beside a `## Description` (warning: the section wins). |
| `GRAM_META_NO_COLON` | A `## Meta` line with no `:`. |
| `GRAM_META_SEPARATOR` | A `## Meta` separator other than `: `. |
| `GRAM_META_DUPLICATE_KEY` | A `## Meta` key twice. |
| `META_DEPRECATED_KEY` | A deprecated `## Meta` key (warning: rename it). |
| `META_CONFLICTING_KEYS` | A deprecated and a current `## Meta` key with different values (warning: the current wins). |
| `EQP_LINE_INVALID` | An `## Equipment` line not `<ID> : <type> [params]`. |
| `EQP_ID_BARE` | An equipment id with no digit or underscore. |
| `EQP_ID_DUPLICATE` | An equipment id declared twice. |
| `EQP_TYPE_UNKNOWN` | An equipment type no pack declares. |
| `EQP_SUBTYPE_MISSING` | An equipment type that needs a `subtype=` without one. |
| `EQP_SUBTYPE_INVALID` | A malformed subtype token. |
| `EQP_SUBTYPE_UNKNOWN` | A subtype its type does not know. |
| `EQP_DMM_NO_PARAMS` | A `dmm` line with parameters. |
| `EQP_PACK_UNREGISTERED` | An equipment type whose pack is not loaded (a bundle wiring fault). |
| `SCHEMA_VIOLATION` | JSON the schema refuses (the message says where). |
| `SCHEMA_UNAVAILABLE` | No schema to validate against (warning). |
| `NAME_STRIPPED` | A name the rewrite stripped (warning; name fidelity). |
| `NAME_TRUNCATED` | A name the rewrite truncated (warning; name fidelity). |
| `RECON_UNSUPPORTED_OWNER` | A reconstruct of a section the parser owns. |
| `SYNCEQP_BLOCKED` | Sync Equipment stopped: an `## Equipment` line does not parse. |
| EQP_NO_EMITTER | Sync Equipment stopped: no loaded pack writes a device's ## Equipment line. |
| `SYNCEQP_TYPE_CONFLICT` | Sync Equipment kept a declared type the steps contradict (warning). |
| `SYNCEQP_TYPE_AMBIGUOUS` | Sync Equipment matched several profiles of one type and took the first (warning). |
| `SYNCEQP_NO_PROFILE` | Sync Equipment left out a device whose steps need a profile and none matches its id (warning). |
| `SYNCEQP_MEASURE_ONLY_CHANNEL` | Sync Equipment found a channel no rating-bearing step rates (warning). |
| (a pack's own codes) | See that pack's doc. |
