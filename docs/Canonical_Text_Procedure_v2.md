# Canonical Text Procedure — Common LLM Authoring Rules

The universal grammar of every test procedure, whatever equipment it declares. Each equipment pack's own rule document adds its equipment line, its step verbs, its `## Meta` pin and its worked examples on top; this document names no pack.

## File structure

A canonical procedure is a Markdown file with these sections in order:

```
# <TEST_ID>

## Title
<optional one-line display name>

## Description
<optional multi-line description; blank lines delimit paragraphs>

## Meta
<key: value lines>

## Equipment
<one line per <ID> : <type> ...>

## Steps
<n>. <step>.

## Expected
<criterion>
```

- UTF-8, LF line endings. ASCII elsewhere, except units (`µs`, `kΩ`), `>` notes, `## Title` and `## Description` (and free text under `# <TEST_ID>`): the title and the description take any characters, as specifications are written in French (`é`, `è`, `°C`, `µ`, `±`, `≤`), but a tab or a control character (GRAM_TITLE_CONTROL / GRAM_DESCRIPTION_CONTROL).
- Single blank line between sections. Inside a section, blank lines are ignored, except inside the description (where they delimit paragraphs). Trailing blank lines at the end of any field are stripped on parse.
- No trailing whitespace on any line.

## Authoring surface

Section ownership is declared in the pack's `rules_index.json` (`section_ownership`): each section is owned by the **parser** or the **LLM**. By default the parser owns the `# <TEST_ID>` line, the optional `## Title` and `## Description` sections, and `## Meta`; the LLM authors `## Equipment`, `## Steps`, and `## Expected`. The LLM emits only LLM-owned sections. **Parser-owned sections are reconstructed by the parser** (autocreated or preserved) — if the LLM emits one anyway it is **ignored and regenerated, with no error**. Reconstructing `## Meta` is field-level: the parser preserves `format_version` from the prior procedure (defaulting to `"2.0.1"` when absent) and fills the pack-version keys (`rules_pack` and each loaded pack's pin) from the active bundle. Operator-supplied keys (`board`, `requirement`, `authored_by`) and any extra keys are also preserved, using placeholders only when none exists. The same map renders the prompt's list of sections to emit, so prompt and parser stay in sync; switching a section's owner is a one-line change to the map.

## Header

- Line 1: `# <TEST_ID>` — letter (any case), then `[A-Za-z0-9_-]*`. Length ≤ 64. Operator-only; the LLM rewriter leaves it unchanged.
- `## Title` (optional): single-line display name, any characters (accents, `°C`, `µ`, `±`, `≤`, a non-breaking space) but a tab or a control character, kept as written. The GUI shows `Title (TEST_ID)` when present and different from the id, else the bare id.
- `## Description` (optional): free-form prose, multi-line, any characters (accents, `°C`, `µ`, `±`, `≤`, a non-breaking space) but a tab or a control character, kept as written — never transliterated to ASCII; internal blank lines delimit paragraphs (preserved), edge blanks trimmed.
- Legacy: free-text directly under `# <TEST_ID>` (no `## Description`) is still read as the description and re-emitted as `## Description`; if both appear, `## Description` wins (warning).

## Meta

Flat `key: value` list. One pair per line, separator is `: ` (colon + single space), values are bare strings (no quotes), no newlines in values.

### Required keys

```
format_version: 2.0.1
board: <board-id>
rules_pack: rules_packager_base@<version>
```

Every pack pins itself, `<pack key>: <name>@<version>`, **required** when the procedure declares at least one of that pack's equipment types; omitted otherwise. That pack's doc names its key and name.

Each `<version>` is the one the project's active bundle ships for that pack: Sync Meta writes it, and it
changes with the bundle. The versions in this document are placeholders, never a reference to compare a
procedure's Meta with.

A bundle may override this required-keys list via its `rules/section_ownership.json` side-car (object-shape `meta` entry with `required_keys`).

`board:` is operator-only; the LLM rewriter leaves it unchanged.

### Optional keys

```
requirement: <bench-precondition-or-external-ref>
authored_by: <string>
```

`requirement:` is operator-only; the LLM rewriter leaves it unchanged.

Additional bench-private keys (`serial_number:`, `lab_id:`, …) are accepted and preserved verbatim. There is no `revision` key: a `revision:` line in an older procedure is one of these extras.

### Canonical key order

`format_version`, `board`, `rules_pack`, each pack's pin (in the bundle's order), `requirement`, `authored_by`, then any extras. Omit optional keys entirely when absent (do not emit empty values).

### Pack version pinning

`<pack_name>@<MAJOR>.<MINOR>.<PATCH>` (integers only; no pre-release or build-metadata suffix).

### Examples

With a requirement and one pack's pin (one such line per pack whose equipment the procedure declares):

```
## Meta
format_version: 2.0.1
board: BRD-MAIN-REV2
rules_pack: rules_packager_base@<version>
<pack key>: <name>@<version>
requirement: VOUT delivers 16 V and withstands a 4 A load without excessive drop.
```

Minimal (no pack's equipment, no requirement, no optional metadata):

```
## Meta
format_version: 2.0.1
board: BRD-MAIN-REV2
rules_pack: rules_packager_base@<version>
```

## Equipment

Each equipment line is `<ID> : <type> ...`; the pack owning `<type>` defines the rest of the line in its doc (the base declares `dmm`, see the DMM doc). Common rules:

- `<ID>`: matches `^[A-Z][A-Z0-9_]{0,31}$` and contains at least one digit or underscore. E.g. `DMM1`, `DMM_GENERIC`, `BENCH_2`, `IN3`. Bare names (`DMM`, `SUPPLY`, `LOAD`) rejected.
- The declared line is the identity authority: `<ID> : <type> ...` binds the id to its pack's grammar and to all tooling. The id itself is an opaque label — names like `DMM1` are a readability convention, never a typing mechanism. Never rename an id or infer a type from its name.
- An `<ID>` is declared in `## Equipment` if at least one step references it (and, when its pack's line lists channels, the channels the steps name). Generate new entries from the steps; never invent unused equipment — but PRESERVE existing operator-declared entries verbatim even when no step references them yet.

### Capabilities on an equipment line

A pack's equipment line may carry capabilities (per-channel ratings, a subtype); that pack's doc says which are required and how to derive them from the operator's prose and the steps. When neither gives a value the pack requires, add an `.llm_session.json:open_questions[]` entry asking the operator to confirm it.

### Bench-identification fields are forbidden

Fields like `visa`, `port`, `baud`, `timeout`, `remote`, `manual_override`, `channel` (as a top-level equipment field), and similar bench-wiring metadata **never** appear on equipment lines. They live as inventory constants in the generated `test.py`. Emitting them will fail validation.

## Steps

Every step follows: `<n>. <verb-clause>.`

- `<n>` is the 1-based step number, sequential and gap-free.
- `<verb-clause>` starts with one of the base verbs below, or with a verb of the pack owning the equipment the step names (that pack's doc lists its verbs and forms).
- Trailing `.` is mandatory.
- No `;`, no `and` joining channels — one action per step.

### Canonical verbs

| Verb | Used by | Operator-mediated? |
|---|---|---|
| `Connect` | manual wiring (this doc §Connect) | yes (implicit; no `Operator:` prefix) |
| `Disconnect` | manual de-wiring (mirror of `Connect`; same free-text shape) | yes (implicit; no `Operator:` prefix) |
| `Move` | manual relocation, typically `Move connection from <X> to <Y>.` | yes (implicit; no `Operator:` prefix) |
| `Wait` | `time.sleep` (this doc §Wait) | no |
| `Operator:` | qualifier prefix for operator-mediated reads/judgments (this doc §Operator:) | yes (always) |
| `{<ref>} =` | bind a fresh ref to a computed value (this doc §Ref-binding) | no |
| `Timestamp` | capture the clock's seconds (this doc §Timestamp) | no |
| `Call` | run a project function and bind its results (this doc §Call) | no |
| `<Verb> <EQUIP> ...` | a pack's step on the equipment `<EQUIP>` it declares: setpoints, readings bound to `{N}`, writes, queries. Its verbs, their forms and what each binds are in that pack's doc | no |

The base set is closed for v2.0.2. A step starting with none of the base verbs goes to the pack owning the first declared equipment id the step names; that pack reads it or refuses it (`GRAM_UNKNOWN_METHOD`, as for a step naming no declared id).

### Step order is preserved verbatim

The sequence of operator-written actions encodes timing and safety
dependencies the rewriter cannot infer (e.g. *"supply output ON before
the load is wired"* implies the operator wants the supply hot at that
specific moment, possibly because something downstream gates on it).

**The LLM rewriter MUST NOT reorder operator-emitted actions.** It may:

- **Renumber** the whole `## Steps` block to keep numbers gap-free
  (per §"Step renumbering when adding or removing steps").
- **Split** a multi-action operator sentence into multiple canonical
  one-action steps in their original relative order (a sentence
  setting a voltage, a current limit and an output → three
  one-setting steps in the same slot; each pack's doc shows its own).

### Operator content is never dropped

Every operator-emitted action that binds `{N}` or mutates instrument state MUST produce at least one canonical step.

1. Canonical verb exists → emit it.
2. No canonical verb → fall back to `Operator: <free-text>.` (manual action), `Operator: read <quantity> on <location> with <device> as {N}.` (measurement — `<device>` may be any declared id, including one a pack drives), or `Operator: judge <description> (target = "<expected>") as {N}.` (judgment, target carries the operator's expected value when prose names one).
3. Ambiguous → emit `Operator: <free-text>.` AND raise `open_questions[]`.

Excluded: notes (`> ...`), macro directives, empty lines.

`{N}` bindings (a pack step's `as {N}`, `Operator: read/judge`, `Timestamp`, `Call ... as`, ref-bind) are strictly never dropped — losing one breaks the matching `## Expected` criterion.

## Connect — manual wiring

```
<n>. Connect <free-text>.
```

- `Connect ` (verb + single space) is the literal prefix.
- `<free-text>` is operator-readable wiring instructions, preserved verbatim (leading/trailing whitespace stripped, internal whitespace preserved).
- Not machine-checkable: a `Connect` step is an instruction for the operator at the bench, not an automated action.

### Common wiring shapes

`<EQUIP>` below stands for any declared equipment id whose pack's line lists channels; each pack's doc shows its own wiring.

| Pattern | Form | Example |
|---|---|---|
| Source → connector | `<DEVICE> <CHAN> <POLARITY> to <CONN>` | `Connect <EQUIP> CH1 + to P4` |
| Source → connector pin | `<DEVICE> <CHAN> <POLARITY> to <CONN> pin <N>` | `Connect DMM1 + to P15 pin 29` |
| Probe → labeled TP (space) | `<DEVICE> <CHAN> to TP <NAME>` | `Connect <EQUIP> CH1 to TP VOUT_5V` |
| Probe → glued TP (underscore) | `<DEVICE> <CHAN> to TP_<NAME>` | `Connect <EQUIP> CH2 to TP_EN_SR` |
| Probe → glued TP (digit) | `<DEVICE> <CHAN> to TP<N>` | `Connect DMM1 + to TP9` |
| Device → device (non-probe) | `<DEVICE> <RESOURCE> to <free-text>` | `Connect <EQUIP> <RESOURCE> to fixture relay K3 control input` |
| Accessory parenthetical | `... (with <accessory>)` | `Connect <EQUIP> CH1 to TP VOUT_5V (with 10:1 probe)` |
| Via clause | `... (via <jack>)` | `Connect DMM1 - to P5 (via 6.3mm jack)` |

### Compound wiring

For inherent +/- pairs (DMM probes, a channel's + and - terminals), one
step with comma-separated leads is canonical:

```
Connect DMM1 + to TP VOUT, - to TP GND.
Connect <EQUIP> CH1 + to P4, - to P2.
```

For independent connections that do not form a +/- pair (e.g. two probe
channels on different signals), emit separate steps:

```
3. Connect <EQUIP> CH1 to TP VOUT.
4. Connect <EQUIP> CH2 to TP VREF.
```

### Test-point (TP) prefixing rule

A signal label is a **probe destination** whenever a measurement probe (a DMM lead, a probe channel, an I/O pin) attaches to a board signal that is not already a connector or glued TP id.

**MUST prefix any probe-destination signal label with `TP `** (capital T, capital P, single space, then the label). This lets the deterministic media extractor recognize the label as a board test point and produce a media entry for the GUI.

**Decision tree for the LLM:**

1. **Connector designator or pin?** (`P3`, `P3 pin 16`, `P6#2`) → DO NOT prefix. Already a PCB-physical reference.
2. **Already in glued TP form?** (`TP9`, `TP_VOUT`) → DO NOT prefix. Already canonical.
3. **A declared equipment id?** (`DMM1`, `BENCH_2`) → DO NOT prefix; if the destination really seems to be an equipment id used as a probe target, **raise an `open_questions[]` entry** rather than guess.
4. **A channel token inside an instrument?** (`CH1` after an equipment id: `<EQUIP> CH3`) → DO NOT prefix. Instrument-side identifier.
5. **Reference designator?** Letter(s) + digits — `C114`, `R12`, `U3`, `F1`, `P3`, `IC22`, etc. — optionally with a `.<n>` sub-index (`R356.1`) and/or ` pin <m>` → DO NOT prefix. PCB-physical reference, preserve verbatim.
6. **Otherwise (unfamiliar signal name, board net, node label)** → **PREFIX with `TP `**.

When in doubt, prefer prefixing — false positives produce harmless extra media entries; false negatives leave the operator without a board image.

**Operator-written content is preserved verbatim.** Every detail in the operator's wiring/setup instruction stays — characters in names, parenthetical signal annotations, dual-pin alternates, reference-ground pins, probe-point destinations, and units. Never silently strip, drop, normalize, or summarize.

**Reference-ground wiring steps are never omitted.** A probe channel's ground reference is a separate `Connect <EQUIP> <CH> reference to <connector pin>.` step. Dropping it is a structural loss, not a simplification.

| What the operator wrote | WRONG (lossy) | RIGHT (verbatim) |
|---|---|---|
| `Connect <EQUIP> CH3 to UART_RX` | `Connect <EQUIP> CH3 to UART_RX.` | `Connect <EQUIP> CH3 to TP UART_RX.` |
| `Connect DMM1 + to VOUT_5V` | `Connect DMM1 + to VOUT_5V.` | `Connect DMM1 + to TP VOUT_5V.` |
| `Connect DMM1 + to C114 pin 2` | `Connect DMM1 + to TP C114_pin_2.` | `Connect DMM1 + to C114 pin 2.` |
| `Connect probe CH2 to TP +HIGH_28V` | `Connect <EQUIP> CH2.` *(probe-point dropped)* | `Connect <EQUIP> CH2 to TP +HIGH_28V.` |
| `Connect probe CH2 to +HIGH_28V` | `Connect <EQUIP> CH2 to TP HIGH_28V.` *(leading `+` stripped)* | `Connect <EQUIP> CH2 to TP +HIGH_28V.` |
| `Connect <EQUIP> CH1 reference to P3 pin 20` | *(step omitted entirely)* | `Connect <EQUIP> CH1 reference to P3 pin 20.` |
| `Connect DMM1 + to P15 pin 29/30` | `Connect DMM1 + to P15 pin 29.` *(dual-pin alternate lost)* | `Connect DMM1 + to P15 pin 29/30.` |
| `Probe at U46.1 pin 5` | `Probe at U46 pin 5.` *(sub-designator stripped)* | `Probe at U46.1 pin 5.` |
| `Wait 100 ms` | `Wait 0.1 s.` *(unit normalized)* | `Wait 100 ms.` |
| `{1} < 20 mV` | `{1} < 0.020 V` | `{1} < 20 mV` |
| `Connect <EQUIP> CH1 reference to P15 pin 16/17 (GND_PWR)` | `Connect <EQUIP> CH1 reference to P15 pin 16/17.` *(ground-rail label stripped)* | `Connect <EQUIP> CH1 reference to P15 pin 16/17 (GND_PWR).` |

**Names are atomic** — every character including `+`, `-`, `.` stays. Preserve `+HIGH_28V`, `+VCC_5V0`, `BATT-`, `U46.1 pin 5` verbatim.

**Annotations in parentheses after a pack's resource** (`<RESOURCE> (<SIGNAL>)`) say WHICH signal: keep them where that pack's grammar takes one (its doc says so); never strip them silently.

**Parentheticals on `Connect` steps are always preserved verbatim in the free-text tail.** They serve two purposes: (a) accessories — `(with 10:1 probe)`, `(via 6.3mm jack)` — describe wiring hardware; (b) signal/ground labels — `(GND_PWR)`, `(+HIGH_28V)`, `(GND_SIG)` — name the net at the connection point. Both are operator content. **Never strip them** even when collapsing "pin A or pin B (LABEL)" to canonical `pin A/B`: keep the `(LABEL)` after the slash form.

**Probe-points are mandatory in wiring** — `Connect <EQUIP> <CH> to <TP/component>` with no destination is invalid. The destination carries the physical-location contract.

**Units are operator-as-written** — `20 mV` stays `20 mV`, never `0.020 V`. `100 ms` stays `100 ms`, never `0.1 s`. Do **not** SI-normalize.

## Operator: qualifier

Some actions are not driven by an instrument's API — DMM reads (always operator-mediated), qualitative judgments, manual setup steps. They use the `Operator: ` prefix:

```
<n>. Operator: <verb-clause>.
```

Two structured patterns and one universal fallback:

| Pattern | Form | Example |
|---|---|---|
| Read a value | `Operator: read <quantity> on <location> with <device> as {<ref>}.` | `Operator: read DC voltage on TP VOUT with DMM_GENERIC as {1}.` |
| Judge qualitative | `Operator: judge <description> (target = "<expected>") as {<ref>}.` | `Operator: judge UART signal integrity on TP UART_RX (target = "OK") as {2}.` |
| Manual action (universal fallback) | `Operator: <free-text>.` | `Operator: connect the alligator clips of the load to the DUT output.` |

`Operator: <free-text>.` is the **universal manual fallback** for any unstructured action that does not match `read` or `judge` and is not instrumented.

**`judge` is the yes/no/skip qualitative check** against `<expected>`. Use it for pass/fail/skip judgments that are not a numeric read (signal integrity, "did the surge trigger fire", visual inspection). Pair each judge with `{<ref>} operator decides "<expected>"` in `## Expected`.

**Note on `Connect`:** The `Connect` verb is always operator-mediated (wiring is manual) but does **not** take the `Operator:` prefix because it is a top-level verb. Always write `Connect <text>.` standalone, never `Operator: Connect ...`.

## Wait — time.sleep

```
<n>. Wait <duration> <unit>.
```

- `<duration>` is a number, written as every step value is (`2.5`, never `2,5`), or a `${...}`, read as every setpoint is (`Wait ${T} ms`); any other form (`1,5`, `2 min`) is `WAIT_UNIT`.
- `<unit>` is `s`, `ms`, `us`, `µs` or `ns`.
- This is the **only** form starting with `Wait`: waiting on an instrument (a captured frame, a settled reading) is that pack's own verb, in its doc.

## Ref-binding `{<ref>} = <expr>.`

```
<n>. {<ref>} = <expr> [<unit>].
```

- No equipment side effects — writes only to the result store.
- `<expr>` is an expression as a `## Expected` line writes one — refs `{N}` / `{BASE + i}`, numbers (`0x` hex too), `+ - * /` with the usual precedence, `& | << >>` on integers, `abs()`, parentheses, an aggregate of a ref range (`mean({1..3})`, `min`, `max`, `sum`, `count`, `rms`, as a criterion's) — and `${i}`, `${row.<key>}`, `${name}`, `COUNT(<table>)`. A number inside takes no unit.
- `<unit>` scales the whole value (an Expected line puts a unit on each number): `{2} = {1} * 2 mV` stores `{1} * 2 * 0.001` in SI and shows it in mV (`3 mV`).
- A lone ref or name is a copy and binds what its source binds: a text stays a text (`{6} = {5}`, then `{6} = "OK"`); a calculation on a text is `BIND_ON_TEXT`; a copy read as a number (arithmetic, a number criterion or condition) makes its typed source a number, and a text criterion on the same chain is then `EXP_TEXT_ON_NUMBER`.
- A minus sign before a name, a ref or a group is its own operation, written back as written (`-${K} * 2`, `2 * -{1}`; inside a bit operation in parentheses, `(-${K}) << 1`), in Binds, conditions, `@LET` and `## Expected` alike; a `#` is no comment: a comment goes on a `> note` line.
- `<ref>` must not be bound elsewhere (a pack step's `as {N}`, another bind step).

Example:

```
@FOR i IN 0..30
3. Operator: set the supply to ${i} V.
4. Operator: read DC voltage on TP VOUT with DMM1 as {1}.
5. {2} = ${i}.
@IF {1} > 5.0 V
@BREAK
@ENDIF
@ENDFOR
```

A PT100 temperature from an RTD converter's code, in one step:

```
7. Operator: read the RTD converter code on TP RTD_CODE as {5}.
8. {6} = ({5} * 400 / 32768 / 100 - 1) / 0.00385 degC.
```

## Timestamp — capture the clock's seconds

```
<n>. Timestamp as {<ref>}.
```

- Binds `{<ref>}` to the clock's seconds when the step runs (`time.time()`, a float, unit `s`): the difference of two is the time between them.
- `<ref>` must not be bound elsewhere.

Example — verify a charge phase fits a time budget:

```
1. Operator: switch the supply output on.
2. Timestamp as {1}.
... charge sequence ...
8. Timestamp as {2}.
9. {3} = {2} - {1}.
```

`## Expected`:

```
{3} <= 180 s
```

## Call — run a project function

```
<n>. Call <module>.<function>(<arg>, ...)[ (timeout = <t> <unit>)][ as <out>, ...].
```

- `Call analysis.rise_time({1..200}, 10 %, 90 %) as {201} us.` runs `rise_time` from `functions/analysis.py`; `a.b.f` is in `functions/a/b.py`.
- It gets and returns values; it never drives an instrument.
- No name part starts with `_` or is a Python keyword.
- Arguments: `{1}`, `{1..200}` (a list), `5`, `0x1F`, `50 mV`, `${n}`, `"fast"`, `TRUE`; keywords last: `limit=50 mV`.
- Only the unit table's units; for `dB` or `ppm` write none: `as {Thd}`, `{Thd} < -40`.
- `${n}` in a text is replaced: `"J${n}"`.
- No arithmetic in an argument; use a Bind first.
- Spacing is fixed; fixing it is spelling, not content, so do it even if the operator writes otherwise: `f(2, 5)`, `limit=50 mV`, `50 mV`, `(timeout = 5 s)`, `as {30} us`; else `CALL_FORM`.
- `as {30} us`: the function returns µs.
- Several outputs take a tuple, in order; `{H[1..10]}` takes a list of 10.
- An output range's bounds may be computed: `as {BASE..BASE+9}`.
- `(timeout = 5 s)` after `)` limits the call (default 60 s); `timeout=5 s` inside is the function's argument.
- Write a `Call` only where the operator names the function; else `Operator:`.

Example — ripple of a sweep, and a pass flag:

```
21. Call analysis.ripple({1..200}, limit=50 mV) as {202} V, {203}.
22. Call log.tag("J${i}", {Vrail[i]}).
```

`## Expected`:

```
{202} < 50 mV
{203} = 1
```

## Numeric and unit syntax

- Integers: bare digits (`5`, `100`); in an `## Expected` line an integer is exact at any size (`18446744073709551615`) and comes back as written.
- Numbers, in `## Expected` lines, Binds, conditions and macro values: digits 0-9 (any other digit where a number is read is `GRAM_DIGIT`; a text, a `# comment` and an `Operator:` step's words take any) with a fraction (`5.` is the integer 5; a final `.` ending a line is its period). Texts in `## Expected` lines, conditions and macro values: double-quoted, escapes `\"` and `\\` (a lone backslash is a finding); a step's quoted text (a judge's target, a sent payload) is kept as written.
- A step's value (a pack step's setpoint or timeout, a `Wait` duration) is checked for its form only — what an instrument takes is its driver's to say: a number and its unit, a space between or none (`5 mV`, `5mV`, `0x10 V`), or a `${...}` and the unit.
- Procedure text writes a number one way: digits with `.` decimals or hex, a prefix only with its unit (`5 mV`, `5 kV`); `5,5 V`, `5 k` or `.5 V` is a finding in a step.
- Hex, in `## Expected` lines, Binds, `@IF` / `@WHILE` conditions, macro values and setpoints: `0x` + hex digits (`0x1A00`, `0x1a00`), kept as written; a sign before it, or before any number, is its sign everywhere (`-0x10` is -16, `+0x10` 16, `+5` 5): a `-` is written back, a `+` is not (`{2} = +{1}.` is `{2} = {1}.`), a cell's and a list item's kept as written; the digits run to the first non-hex character, so a unit follows a space (`0x10 A`).
- Floats: integer part, dot, decimal part (`28.0`, `0.5`). Integers are accepted in float positions (`28 V` ≡ `28.0 V`; a step value is written back as a float). A `${...}` stands apart, its unit after a space: `${v} V`, never `${v}.0 V` or `${v}mV`.
- A number may take an exponent wherever one is read (`1e-05 A`, `2.5E3`, `-3e+2`); `0x1e5` is hex.
- `<value> <unit>` with one space, as the parser writes it back.
- **Operator-as-written units.** Whatever unit the operator wrote — `mV`/`V`/`kV`, `mA`/`A`, `µs`/`us`/`ms`/`s`, `kHz`/`MHz`, `kOhm`/`kΩ`, `degC` — is preserved verbatim. **Never SI-normalize.** `20 mV` stays `20 mV`, **not** `0.020 V`. `100 ms` stays `100 ms`, not `0.1 s`. `1 MHz` stays `1 MHz`, not `1000000 Hz`.

## Measurement IDs `{N}`

A `{N}` is a non-negative integer reference (`{0}`, `{1}`, `{2}`, …) or a name (`{Vout}`) that binds a reading (a pack step's `as {N}`, `Operator: read`, `Operator: judge`, `Timestamp`, a `Call` output, a Bind) to a result slot. Each reference must be unique across the procedure. The `## Expected` section consumes `{N}` references in its criteria.

### Range expansion `{A..B}`

For sweeps, use the inclusive integer range form:

```
{1..16} < 5.0 V
```

is equivalent to 16 separate Expected lines. The same range syntax in step lines pairs with a `TOKEN[A..B]` token range:

```
5. Operator: read DC voltage on TP VOUT with DMM1 as {1..3}.        # invalid: same op three times
5. Operator: read voltage on TP VMONI[1..3] with DMM_GENERIC as {1..3}.   # valid: paired token+id
```

When the body is identical for all entries (same op, same channel), the paired form is the right answer for measurement sweeps over indexed test points.

### Named measurements `{Vout}`, `{Vrail[i]}`

- A ref may be a name: `as {Vout}`, then `{Vout} < 5 V`; names and numbers mix.
- `{K}` is still arithmetic for a `@LET` or `@ALLOC` K, or a loop variable inside its loop.
- Never reuse a macro, `@TABLE`, loop variable or function as a name: `{mean}` is `REF_NAME_TAKEN`.
- An index is ref arithmetic: `{Vrail[i + 1]}` at `i = 2` is `Vrail[3]`.
- No space before `[` or in `[*]`.
- A name is plain or indexed, never both (`REF_NAME_SHAPE`).
- `{Vrail[3..7]}` is items 3 to 7, wherever a range goes.
- `{Vrail[*]}` is every item bound so far; it never binds or takes a screenshot.
- Otherwise a name behaves like a number.
- An operator's name becomes the ref: "measure Vout" → `as {Vout}`; else number new readings.

```
@FOR i IN 1..8
6. Operator: read DC voltage on TP VRAIL${i} with DMM1 as {Vrail[i]}.
@ENDFOR
```

`## Expected`:

```
{Vrail[3..7]} < 5.2 V
max({Vrail[*]}) < 5.3 V
```

## Notes `>`

```
> <free-text note>
```

A line beginning with `> ` (greater-than, single space) is a note. Notes:

- Are allowed only inside `## Steps` and `## Expected`.
- Have no step number; do not affect the numbering of surrounding steps.
- Are operator hints that survive round-trip (unlike a `# ...` comment ending an `## Expected` line, which is dropped).

Use notes to record context, rationale, or operator instructions that don't fit a step verb.

## Step renumbering when adding or removing steps

When the LLM inserts, removes, or reorders steps, the **whole `## Steps` section is renumbered** so step numbers remain sequential and gap-free. Macro directives (`@LET`, `@FOR`, etc.) and notes (`> ...`) keep their position but are not numbered. Do not leave gaps, do not duplicate numbers, do not annotate inserted steps — the canonical text shows only the renumbered output.

### Step numbers inside `@IF` / `@ELIF` / `@ELSE`

Step numbers are **globally unique** across the entire procedure. Continue counting through every branch — each step in every branch consumes a fresh, never-reused step number. Only one branch executes at runtime, so steps in the other branches simply don't run; this is normal and not a gap to fill.

Example (branches of unequal length, all step numbers globally unique):
```
@IF {1} < 4.8 V
46. Operator: judge brownout (target = "OK") as {12}.
47. Operator: read recovery time on TP VRECOV with DMM_GENERIC as {13}.
@ELSE
48. Operator: judge nominal (target = "OK") as {14}.
@ENDIF
49. Operator: switch the load off.
```

The `@IF` branch runs 46 then 47; the `@ELSE` branch runs 48. After `@ENDIF`, both paths converge at step 49. Refs `{12}`, `{13}`, `{14}` are likewise globally unique — do not reuse a ref across branches.

## `## Expected` section — closed criterion grammar

Each line: `<expr> <comparator> <expr>`; a unit follows a number (`{1} <= {2} * 2 + 800 mV`), never a group.

`<expr>`: `{N}` | numeric literal (decimal or `0x` hex) | `TRUE` / `FALSE` (upper case; 1 and 0). A ref range `{A..B}` and an aggregate `mean({A..B})` / `min` / `max` / `sum` / `count` / `rms` (root mean square) are criteria of their own (`{1..16} < 5 V`, `mean({1..3}) - {4} < 1`), never terms.
Terms may be joined with `+ - * /` (any combination of refs / literals) and, on integers, `& | << >>`, parenthesised when mixed with another operator (`({1} >> 8) & 0x1F`); `abs(<expr>)` is a term.

`<comparator>`:

| Form | Notes |
|---|---|
| `=` | Equality. With trailing `+/- <tol>` → tolerance (the nominal and an absolute tolerance both carry a unit or neither does; `%` fits either); without → strict equality. |
| `< <= > >=` | Inequality. |
| `in [lo, hi]` | Range; its left side may be arithmetic over refs and unit-less numbers (`{3} / 256 in [20, 30]`) or an aggregate (`max({1..8}) in [4, 5] V`). A bracket facing out leaves its bound out (`]lo, hi[`, `[lo, hi[`, `]lo, hi]`); `not in` inverts. |
| `one of [a, b, ...]` | Equal to any of two or more numbers (one trailing unit for all) or two or more quoted texts. |
| `matches /regex/` | Regex match (Python `re`; a pattern it cannot compile is a finding). |
| `contains "<text>"` | Substring test; `contains one of [...]` / `contains all of [...]` take two or more texts (any / every). |
| `monotonic increasing` (or `decreasing`/`strictly_increasing`/`strictly_decreasing`) | Sequence shape. |
| `operator decides "<text>"` | Operator-judged verdict. |

### Examples

```
{1} = 28 V +/- 5 %
{1} = 0x10 +/- 2
{1} = "OK"
{1} = '1'
{1} = TRUE
{1} < 5 V
{1} <= {2} + 0.8 V
{1} <= {2} + {3}
{1} - {2} <= 0.8 V
{1} in [27, 29] V
{1} matches /^OK\d+$/
{1} contains "0x4B"
{1} = 0x1A00
{1} in [0x0000, 0xFFFF]
({1} - {2}) * 0x10 in [0x0000, 0xFFFF]
({1} >> 8) & 0x1F = 0x04
abs({1} - {2}) <= 0.1 V
{1} one of [0x01, 0x02, 0x04]
{1} one of ["OK", "DONE"]
{1} not in ]1, 5[
{3} / 256 not in [20, 30]
{2} contains one of ["0x48", "0x4B"]
{2} contains all of ["0x4B", "0x4D", "0x4E", "0x4F"]
{1} < 1 or {1} > 2
({1} < 1 or {1} > 2) and {2} = "OK"
{1} operator decides "OK"
{100..132} monotonic increasing
mean({100..132}) <= 30 degC
{1..16} < 5 V        # sweep — expands to one criterion per ref
```

### Rules

- **The LLM rewriter MUST NOT invent criteria.** Tolerances or checks not in the operator's success conditions are forbidden. Qualitative checks become `Operator: judge ... as {N}.` + `{N} operator decides "<text>"`.
- **`=` paired with `+/-` is tolerance; `=` without `+/-` is strict equality.** No `==`.
- **`or` / `and` (lower case) join whole criteria**, one word per level (a mix takes parentheses); `not` exists only in `not in`.
- **A tolerance's nominal is a number or one `${...}`** (`${K * 2}`): no arithmetic outside the braces.
- **Bit operations (`& | << >>`) take integers**: no unit inside them, a constant shift by 0 to 512, no unit in their criterion; a side known now — numbers alone (`8 / 2`, `4.0`), a `${name}`, a Bind of no reading — counts when exactly whole, else `EXP_BITS_ON_DECIMAL`, as on a decimal reading (`Operator: read`, a pack's measured reading, `Timestamp`).
- **A number added to a reading carries its unit** when its criterion has one (`{1} + 500 mV < 3 V`; each of `or` / `and` on its own), as does one added to a number with a unit (`2 mV + 1 mV`); a factor holding no reading (`* 2`, `/ 256`, `* abs(-5)`) takes no unit.
- **Bounds low first**: `[5, 1]`, `]5, 5]` or `{5..1}` is `EXP_RANGE_INVERTED`.
- **A number criterion or condition on a text** (a device reply, a text `@LET` or cell, a copy of one) is `EXP_NUMBER_ON_TEXT`.
- **Text criteria (`contains`, `matches`, `= "..."`, `one of ["..."]`) only on text readings**; a typed `Operator:` answer is its text, unless a criterion, a condition or an aggregate reads it as a number; on a number (`Operator: read`, a pack's measured reading or bus read, a calculating `Bind`, a `Call` output with a unit, `Timestamp`) they are `EXP_TEXT_ON_NUMBER`: compare with `=`, `one of` or `in`.
- **Refs**: `{1}`, ranges `{1..16}`, arithmetic `{EN_BASE + i}`, names `{Vout}`, `{Vrail[3..7]}`, `{Vrail[*]}`. `{0}` is valid. A range with a variable bound (`{i..i+1}`) goes only in an aggregate or a `Call` argument.
- **`## Expected` may be empty** — heading still required.
- **Trailing `.` optional.**

## Macros

Macros are a compile-time layer for parametric repetition and lookup tables (`@IF` / `@WHILE` decide at run time). All macro inputs (`@LET` constants, loop indices, `@TABLE` rows, `COUNT(table)`) are resolvable at parse time. Runtime measurements, operator input, and external state are forbidden as macro inputs.

Directive lines start at column 0 with `@` (no leading whitespace), use uppercase directive names, have no terminating `.`, and appear inside `## Steps` and `## Expected`. The same macro grammar applies to both sections: `@FOR i, row IN <table>` works identically in `## Expected` to generate one criterion per row.

### Directive set

| Directive | Form | Closes? |
|---|---|---|
| `@LET` | `@LET <name> = <expr>` | (statement) |
| `@TABLE` | `@TABLE <name>` | yes; closed by `@ENDTABLE` |
| `@ROW` | `@ROW <table> <key>=<value> <key>=<value> ...` | (entry inside table) |
| `@ENDTABLE` | `@ENDTABLE` | (closes the most recent open `@TABLE`) |
| `@ALLOC` | `@ALLOC <name> START=<expr> COUNT=<expr>` | (statement; reserves a contiguous block of measurement IDs) |
| `@FOR` | `@FOR <var> IN <iterable>` *or* `@FOR <idx>, <row> IN <table>` | yes; closed by `@ENDFOR` |
| `@ENDFOR` | `@ENDFOR` | (closes the most recent open `@FOR`) |
| `@IF` | `@IF <condition>` | yes; closed by `@ENDIF` |
| `@ELIF` | `@ELIF <condition>` | (sub-header inside `@IF`) |
| `@ELSE` | `@ELSE` | (sub-header inside `@IF`) |
| `@ENDIF` | `@ENDIF` | (closes the most recent open `@IF`) |
| `@BREAK` | `@BREAK` | (statement; only inside a `@FOR` or `@DO` body, else `MAC_BREAK_OUTSIDE_LOOP`) |
| `@ABORT` | `@ABORT` | (statement; aborts the entire procedure at runtime, jumping to `## Expected` evaluation and cleanup) |
| `@DO` | `@DO <var>` | yes; predicate `@WHILE <condition>` then closed by `@ENDDO` |
| `@WHILE` | `@WHILE <condition>` | (continuation predicate inside `@DO`) |
| `@ENDDO` | `@ENDDO` | (closes the most recent open `@DO`) |

### Identifier case

All macro identifiers (`@LET` names, `@TABLE` names, `@ROW` keys, `@ALLOC` names, `@FOR` loop variables) accept the same pattern: `[A-Za-z_][A-Za-z0-9_]*`. Either case works at the parser level; pick by convention. Common house style (used in the examples below):

- **lowercase** — variables, loop vars, row keys (`per_pin`, `i`, `row`, `pin`, `loc`).
- **UPPER_SNAKE_CASE** — table names and `@ALLOC` block names (`EN_MAP`, `PIN_BASE`), to visually mark them as compile-time named regions.

Substitution (`${name}` and `${row.<key>}`) is case-sensitive against whatever you defined.

### Common mistakes (wrong → right)

| Wrong | Right | Why |
|---|---|---|
| `16. @LET per_pin = 6` | `@LET per_pin = 6` | **`@`-directives are never step-numbered.** They start at column 0 with `@`, no leading `<n>.` prefix. Notes (`> ...`) and macros are the two exceptions to step numbering — both keep their position relative to numbered steps but carry no number themselves. The renumbering pass skips them. |
| `17. @TABLE EN_MAP` / `18. @ROW EN_MAP idx=0 ...` / `26. @ENDTABLE` | `@TABLE EN_MAP` / `@ROW EN_MAP idx=0 ...` / `@ENDTABLE` | Same rule applies to every directive: `@TABLE`, `@ROW`, `@ENDTABLE`, `@FOR`, `@ENDFOR`, `@ALLOC`, `@IF`, `@ELIF`, `@ELSE`, `@ENDIF`, `@DO`, `@WHILE`, `@ENDDO`, `@BREAK`, `@ABORT`, `@LET`. None take a `<n>.` prefix. |
| `@FOR i IN 0 TO 32` | `@FOR i IN 0..32` | Range syntax is `..` (double-dot) only. There is **no** `TO` keyword in the macro grammar, regardless of case (`TO`, `to`, `To` all rejected). |
| `@FOR i IN 0..30 STEP 1` | `@FOR i IN 0..30` | `STEP` is **only** for float ranges and is mandatory there (`@FOR v IN 0.0..5.0 STEP 0.5`). On integer ranges the step is implicit `1`; writing `STEP 1` is rejected. |
| `@LET PER_PIN = 6` (then later `${PER_PIN}`) | `@LET per_pin = 6` (then `${per_pin}`) | Identifier case is permissive at the parser, but inconsistency between the `@LET` name and its `${...}` use is **not** — substitution is case-sensitive. House style is lowercase for `@LET` names; pick lowercase if unsure. |
| `@FOR i IN [1, 3, 5,]` (trailing comma) | `@FOR i IN [1, 3, 5]` | Bracketed lists do not allow a trailing comma. |
| `@IF condition AND other` (boolean operator outside a condition expression) | `@IF condition AND other` only inside the documented condition AST forms; otherwise split or use `@ELIF` | Conditions follow a closed AST (see Conditions section); free-form boolean prose is rejected. |

### `@LET` — compile-time variable

```
@LET per_pin = 6
@LET ambient_ref = 200
@LET threshold = per_pin * 8
```

A value written as a calculation — a name alone, or an operator (`+ - * / %`, bit operations, a sign; `^ ~ !` too) or a function call, with no two operands side by side and no quoted text — is one, computed with no unit by the engine (`2026-10-05` is 2011, `--5` is 5, `-0x10` is -16; quote it for the text; computed through a fraction, a decimal: `2.5 * 2` is 5.0, as a Bind's). Where an integer is needed — a ref, a `@FOR` count, a bit operation's operand — a value exactly whole counts as that integer (`8 / 2`, `F * 2` with `F = 2.5`), a fractional one is a finding, never truncated. A `@LET` reading a name none binds is `MAC_VAR_UNBOUND`, one the engine refuses (`2 ** 3`, `K -`, `K & 3 + 1`, `{1} + 1`, `K ^ 2`, `max(K, 1)`, `abs -K`) or cannot compute (`K / 0`) `MAC_LET_FORM`, and a use of a refused name says nothing more. `@LET X = K` and `@LET X = ${K}` are K's value, `@LET X = "K"` the text K, a lone word no `@LET` binds a text (`HIGH`). A quoted value is a text, never a number (`"5"`, `"0x10"`); anything else is a text as written (`3V3`, `5 mV`, `P10 pin 20`, `1.5.2`, `1,5`, `K.`, `IO#P42`). A name is defined once, whatever defines it — a `@LET`, `@TABLE`, `@ALLOC`, or a `@FOR` / `@DO` variable (sibling loops may share theirs; `@FOR i, i` names it twice) — and is no function's (`abs`, `mean`, `min`, `max`, `sum`, `count`, `rms`, in any case): one finding, the directive's.

### `@TABLE / @ROW / @ENDTABLE` — lookup tables

```
@TABLE EN_MAP
@ROW EN_MAP idx=0 loc="P10 pin 20" net=EN_0
@ROW EN_MAP idx=1 loc="P10 pin 21" net=EN_1
@ROW EN_MAP idx=2 loc="P10 pin 22" net=EN_2
@ENDTABLE
```

- `@ROW <table> <key>=<value> ...` defines a row: identifier keys, each once, `<key>=<value>` pairs apart by spaces. A value is never computed: a single number is a number (`5.` is 5; `0x10` and `+5` kept as written, read as their number where one is read), a quoted text a text (`"5"`, `"0x10"`), anything else a text as written (`IO#P42`, `5mV`, `1-2`, `3*4`, `2026-10-05`); a malformed row (`a=1 junk`, `a=1, b=2`, `a="x"y`, `a=- 5`, a key twice) is `MAC_ROW_FORM`. A `@FOR` list's items read alike, split at the commas outside quotes, spaces inside kept (`[P10 pin 1, P10 pin 2]`: two items; `[1,]`, `[ ]`, `[1]]`: `MAC_FOR_ITER`).
- Rows are accessed by index (0-based) or via `@FOR i, row IN <table>` (see below).
- Inside the loop body, `${row.<key>}` substitutes the value of that key for the current row.
- `COUNT(<table>)` returns the row count as an integer at compile time.

### `@ALLOC` — reserve a contiguous block of measurement IDs

```
@ALLOC en_base START=1 COUNT=per_pin*COUNT(en_map)
@ALLOC temp_base START=200 COUNT=4
```

Reserves a block of measurement IDs starting at `START`, length `COUNT`, each computed as a `@LET` is (`START=K / 2`), then cast to an integer, its fraction dropped (`7 / 2` is 3); below 0, or no number, is `MAC_ALLOC_FORM`. The block name (e.g., `EN_BASE`) is then usable in arithmetic ref expressions (see "Arithmetic in refs" below).

### `@FOR ... @ENDFOR` — loops

Iterable forms:

```
@FOR n IN 0..7                          # integer range, inclusive both ends
@FOR v IN 0.0..5.0 STEP 0.5             # float range with explicit step (11 iterations: 0.0, 0.5, ..., 5.0)
@FOR i, row IN en_map                   # multi-var on a table: i = index, row = row
@FOR i, v IN 16.0..13.0 STEP -0.2       # multi-var on a range: i = index, v = value
@FOR x IN [1, 3, 5, 7]                  # bracketed list of values
@FOR k IN pin_count                     # @LET-bound integer name (lowercase)
```

- **Integer range** `A..B` (inclusive both ends; default step = 1); a bound, as a float range's bounds and `STEP`, is a number, a `@LET` / `@ALLOC` name or a `${...}` (`1..N`, `0..${N - 1}`, `0.0..${V} STEP S`) the engine reads, an integer range's exactly whole.
- **Float range** `A.A..B.B STEP S.S` — `STEP` is mandatory and non-zero. Positive step counts up (`start <= stop`); negative step counts down (`start >= stop`). A sign/direction mismatch is rejected.
- **Multi-var** `i, x IN <table|range|list>` — `i` is the 0-based index, `x` is the row/value. Use `i` for refs, `x` for the value. Table: `@FOR i, row IN en_map` → `${row.vin}`. Range: `@FOR i, v IN 16.0..13.0 STEP -0.2` → a step value `${v} V` with `as {BASE + i}`.
- **Bracketed list** for non-sequential values.
- **Name reference** to a `@LET`-bound integer.

**Range syntax uses `..` only — never `TO`, never `to`.** A LLM-natural form like `@FOR i IN 0 TO 32` is rejected; the canonical form is `@FOR i IN 0..32`. There is no `TO` keyword anywhere in the macro grammar.

`@ENDFOR` closes the most recently opened `@FOR`; loops nest.

### `@IF / @ELIF / @ELSE / @ENDIF` — conditional

```
@IF <condition>
 body...
@ELIF <condition>
 body...
@ELSE
 body...
@ENDIF
```

- At most one `@ELSE` per `@IF`. `@ELIF` after `@ELSE` is rejected.
- Conditions combine these leaves with `AND` / `OR` / `NOT` (parenthesized):

| Form | Example |
|---|---|
| Numeric/string compare on a measurement ref | `{1} > 5.0 V`, `{2} == "OK"` |
| Compare two refs | `{3} > {1}` |
| Bound / unbound test | `{4} bound`, `{4} unbound` |
| Compare a macro variable or loop index | `${n} > 100`, `${i} <= 8` (always `${...}`; bare `i > 0` is invalid) |
| Compare a computed value | `{1} / 256 < 4`, `({1} >> 8) & 0x1F == 4`, `abs({1} - {2}) < 0.1 V` |
| A table's row count, a truth | `COUNT(pins) > 2`, `{1} == TRUE`, `FALSE` |
| An operand alone: true when not 0 | `{1}`, `${flag}`, `NOT {1}`, `@WHILE TRUE` |
| A parenthesised condition as a value (1 or 0) | `({1} > 5) == ({2} > 5)` |

A side is an expression as a `## Expected` line writes one, `${name}` and `COUNT(<table>)` too, compared exactly as a criterion is. A text compares with `==` / `!=` only, against a number by the number it reads as an operator's typed answer (`"5" == 5.0`, `"5,5" == 5.5`, `"8m" == 0.008`, `"0x10" == 16` are true), else unequal (`NC == 5` is false), whatever gives it — a reading, a copy, a `${name}`; a `#` is no comment; a condition outside these forms is `MAC_COND_FORM`.

#### `@FOR` + `@IF` — dispatching on the loop index

A complete `@IF / @ELIF` chain inside a `@FOR` already covers every iteration. No outer guard needed.

```
@FOR i IN 0..11
@IF ${i} <= 8
1. Operator: read voltage on TP CH${i} as {i+1}.
@ELIF ${i} == 9
2. Operator: read voltage on TP NORM_HIGH as {i+1}.
@ELIF ${i} == 10
3. Operator: read voltage on TP NORM_LOW as {i+1}.
@ELSE
4. Operator: read voltage on TP DEFAULT as {i+1}.
@ENDIF
@ENDFOR
```

Each `@IF` opens a block; the matching `@ENDIF` closes it. `@ELIF` and `@ELSE` are sub-headers within the same block — they do NOT open new blocks and do NOT need their own `@ENDIF`.

**Threshold-dispatched probe location** — for "use X when V≤Y, else Z" prose:

```
@FOR v IN 0..32
1. Operator: set the supply to ${v} V.
@IF ${v} <= 3
2. Operator: read temperature on TP R106 with DMM1 as {TEMP_BASE + v}.
@ELSE
3. Operator: read temperature on TP Q26.1 with DMM1 as {TEMP_BASE + v}.
@ENDIF
@ENDFOR
```

### `@BREAK` — exit a `@FOR` early

Valid inside a `@FOR` or a `@DO` body; exits the innermost enclosing loop. Outside a loop it is `MAC_BREAK_OUTSIDE_LOOP`.

### `@ABORT` — abort the procedure at runtime

```
@IF {<ref>} > <value> <unit>
@ABORT
@ENDIF
```

Stops procedure execution immediately and jumps to `## Expected` evaluation and cleanup. Use for unsafe conditions detected via measurements (over-temperature, over-current, brownout).

`@ABORT` may appear anywhere in `## Steps`: at top level, inside `@IF`/`@ELIF`/`@ELSE` bodies, or inside `@FOR` / `@DO` bodies. The user is responsible for the placement; an unconditional `@ABORT` at top level will terminate the procedure on the first run.

### `@DO ... @WHILE ... @ENDDO` — run-then-check loop

```
@DO <var>
  body...
@WHILE <condition>
@ENDDO
```

- Body always runs once before the predicate is evaluated.
- `@WHILE <condition>` is the **continuation** predicate, tested on the turn just done: loop continues while true, exits when false. Opposite sense from `@IF <cond> @BREAK`.
- `<var>` is the iteration index (0-based), exposed as `${var}` in the body.
- The predicate must eventually become false; an always-true predicate produces an infinite loop. Safety bound is the author's responsibility.
- `@BREAK` inside the body exits the `@DO`.
- A ref bound inside the body is overwritten each iteration; `## Expected` sees the last bound value only. Use `@FOR + @ALLOC` if every iteration must be retained.

Example — poll until a voltage threshold:

```
@DO i
3. Operator: set the supply to ${i} V.
4. Wait 100 ms.
5. Operator: read DC voltage on TP VOUT with DMM1 as {1}.
6. {2} = ${i}.
@WHILE {1} <= 5.0 V
@ENDDO
```

### `${...}` substitution

- **Any number may be `${...}`**: a name or a calculation of `@LET` / `@ALLOC` / `@TABLE` names and loop variables (`${V}`, `${row.vmax}`, `${i * STEP_A}`). Unknown name: `MAC_VAR_UNBOUND`; no number: `MAC_SUBST_FORM`.
- A reading's number names its variables bare (`{BASE + i}`).

**`${...}` may appear in:** test-point names (`TP T${n}`), resource ids, wholly or in part (`${row.pin}`, `P${i}`), any numeric value position (any pack's step values, `Wait` durations, timeouts), strings, free text, and condition operands. A ref names its variable bare: `{n}`, never `{${n}}` (`REF_FORM`).

Example — substitution in a numeric value position (every numeric position across every pack's grammar accepts a `${...}`):

```
@FOR t IN 1..5
1. Wait ${t} s.
@ENDFOR
```

Each pack's doc shows `${...}` in its own step values, and in resource ids where its steps take them.

**`${...}` MUST NOT appear in a step's leading number `<n>.`** A step inside a `@FOR` body keeps a single fixed step number across iterations.

### Arithmetic in measurement refs `{<expr>}`

Inside a `@FOR` body, refs may carry arithmetic expressions over loop indices and `@ALLOC`/`@LET` names:

```
@ALLOC EN_BASE START=1 COUNT=PER_PIN*COUNT(EN_MAP)
@FOR i, row IN EN_MAP
1. Operator: pull ${row.net} low at ${row.loc}.
2. Operator: read DC voltage on TP ${row.net} with DMM1 as {EN_BASE + PER_PIN*i + 0}.
@ENDFOR
```

Allowed operators: `+`, `-`, `*`, `/`, `%` (modulo), bit operations, parentheses. Operands: integer literals, `@LET`-bound names, `@ALLOC` block names, loop indices, `COUNT(<table>)`; any other form or name is `REF_FORM` wherever the ref is. The expression must give a non-negative integer in each static turn that reaches its step — no `@IF` branch, `@DO` or earlier `@BREAK` in the turn (`REF_FORM` otherwise; a Bind or a condition the same, a division by zero in any such turn a finding).

The same form is allowed in `## Expected` criterion left-hand sides:

```
@FOR i, row IN EN_MAP
{EN_BASE + PER_PIN*i + 0} < 1 V
{EN_BASE + PER_PIN*i + 3} > 20 V
@ENDFOR
```

### Worked example

```
@LET per_pin = 2
@TABLE pins
@ROW pins idx=0 net=EN_0 loc="P10 pin 20"
@ROW pins idx=1 net=EN_1 loc="P10 pin 21"
@ROW pins idx=2 net=EN_2 loc="P10 pin 22"
@ENDTABLE
@ALLOC pin_meas START=1 COUNT=per_pin*COUNT(pins)
@FOR i, row IN pins
46. Connect DMM1 + to ${row.loc}.
47. Operator: drive ${row.net} high.
48. Wait 1 s.
49. Operator: read DC voltage on ${row.loc} with DMM1 as {pin_meas + per_pin*i + 0}.
50. Operator: drive ${row.net} low.
51. Wait 1 s.
52. Operator: read DC voltage on ${row.loc} with DMM1 as {pin_meas + per_pin*i + 1}.
@ENDFOR
```

Steps 46–52 are fixed source-text step numbers; the runtime executes them once per row in `pins` (3 rows × 7 steps = 21 op executions). Refs `{1}` through `{6}` are bound. Note the **payload-only convention** in `@ROW`: the row stores what changes from turn to turn (`net=EN_0`), and what every turn shares stays in the step text; a pack's resource prefix stays in the step text the same way (that pack's doc shows it).

Use macros when:
- The same step pattern repeats over a parameter set (table + `@FOR`).
- Multiple values must be read or written across many operator-readable test points (`@TABLE` of pin locations).
- Measurement IDs need explicit allocation (`@ALLOC`) to keep refs predictable in `## Expected`.

For simple sweeps where one token and one ID iterate together, prefer the paired-range form `TOKEN[A..B]` with `{A..B}` (no macros needed).

## Limits

| Item | Limit |
|---|---|
| TEST_ID length | ≤ 64 characters |
| Measurement name | ≤ 32 characters, `[A-Za-z_][A-Za-z0-9_]*` |
| Equipment ID length | ≤ 32 characters |
| `model_hint="..."` length | ≤ 200 characters |
| Resource id payload (after `#`), in a pack step that takes one | 1–30 chars, `[A-Za-z0-9_.-:/]`; a `${...}` may stand for any part |

Each pack's doc gives the limits of its own forms (payloads, tokens).

## Refs bound inside conditionals

A ref bound inside a `@IF` / `@ELIF` / `@ELSE` body is bound only when its branch executes. The ref may still be referenced in `## Expected`.

Bind the ref in every branch (including `@ELSE`) when you want the criterion to always evaluate against a real measurement.

## Conformance checklist (cross-cutting)

- [ ] Sections in order: header, Meta, Equipment, Steps, Expected.
- [ ] Equipment lists exactly the instruments and channels referenced by steps — no more, no less.
- [ ] Every step starts with `<n>. ` and ends with `.`. Step numbers are sequential.
- [ ] First word of every step is a base verb from the table above, or a verb of the pack owning the equipment the step names.
- [ ] One action per step (no `;`, no `and` joining channels).
- [ ] `Connect` step probe destinations on board signals carry the `TP ` prefix.
- [ ] Refs, numbers or names, are unique; ranges use `{A..B}` or `{Vrail[A..B]}` with paired `TOKEN[A..B]` when bodies match.
- [ ] Notes use `> ` only inside `## Steps` / `## Expected`.
- [ ] No invented form: unmappable prose → `Operator: <free-text>.` or `.llm_session.json:open_questions[]`.
- [ ] No `{N}`-binding step dropped: unmappable operator prose emits an `Operator: read/judge` fallback; ambiguous intent additionally raises `open_questions[]`.

## Appendix: smoke test (universal grammar only)

A reference procedure exercising every **universal** v2.0.2 macro/syntax feature (`@FOR`/`@TABLE`/`@ALLOC`/`@LET`/`@IF`/`@ELIF`/`@ELSE`/`@ABORT`, `${var}`, `${row.<key>}`, range refs `{a..b}`, arithmetic refs `{BASE + i}`, condition operators, comment lines, `Operator:` mediated reads/judges, `Wait`). No pack's verb is used here — each pack's doc carries worked examples showing how the universal grammar composes with that pack's verbs.

```
# DEMO_UNIVERSAL_001
End-to-end smoke test exercising every universal v2.0.2 syntax feature. Pack-specific worked examples ship in each pack's rule doc.

## Meta
format_version: 2.0.1
board: BRD-DEMO
rules_pack: rules_packager_base@<version>
requirement: REQ-DEMO-001 — universal grammar parses cleanly.

## Equipment
DMM_GENERIC : dmm

## Steps
1. Operator: connect DMM_GENERIC + to TP VOUT_5V and - to TP GND.
> Bench precondition: ambient room temperature 22 +/- 2 degC, fixture in place.
2. Operator: confirm fixture is in place.
3. Wait 500 ms.
4. Operator: read DC voltage on TP VOUT_5V with DMM_GENERIC as {1}.
5. Operator: read ambient temperature on TP T_AMBIENT with DMM_GENERIC as {11}.
@LET fail_temp = 110
@TABLE TEMP_PROBES
@ROW TEMP_PROBES idx=0 loc="Q26"
@ROW TEMP_PROBES idx=1 loc="L101"
@ROW TEMP_PROBES idx=2 loc="R106"
@ROW TEMP_PROBES idx=3 loc="U200"
@ENDTABLE
@ALLOC TEMP_BASE START=20 COUNT=COUNT(TEMP_PROBES)
@FOR i, row IN TEMP_PROBES
6. Operator: read temperature on TP ${row.loc} with DMM_GENERIC as {TEMP_BASE + i}.
@IF {TEMP_BASE + i} > ${fail_temp} degC
@ABORT
@ENDIF
@ENDFOR
@FOR v IN 0.0..5.0 STEP 0.5
7. Wait ${v} s.
@ENDFOR
@IF {1} < 4.8 V
8. Operator: judge VOUT_5V brownout severity (target = "OK") as {12}.
@ELIF {1} > 5.2 V
9. Operator: judge VOUT_5V overshoot severity (target = "OK") as {13}.
@ELSE
10. Operator: judge VOUT_5V nominal behavior (target = "OK") as {14}.
@ENDIF
11. Operator: read voltage on TP VMONI[1..3] with DMM_GENERIC as {100..102}.
12. Operator: confirm fixture is back to safe state and disconnect probes.

## Expected
{1} = 5.0 V +/- 0.1 V
{11} in [20.0, 24.0] degC
{12} operator decides "OK"
{13} operator decides "OK"
{14} operator decides "OK"
@FOR i, row IN TEMP_PROBES
{TEMP_BASE + i} - {11} <= 50.0 degC
@ENDFOR
mean({TEMP_BASE..TEMP_BASE+3}) - {11} <= 30.0 degC
max({TEMP_BASE..TEMP_BASE+3}) <= 80.0 degC
{100..102} = 5.0 V +/- 5 %
```

## Appendix: bounded polling — operator-source vs canonical

A second worked example covering an idiom the rewriter LLM frequently mishandles: **bounded polling** — "read X every T until threshold Y or timeout Z". The operator's natural prose invents a form like *measure ... until X or timeout Y* and uses manual *measure timestamp* + *compute* arithmetic. The canonical rewrite combines `Timestamp as {N}.`, `@DO ... @WHILE ... @ENDDO`, the bind step `{N} = {a} - {b}.`, and `Operator: <free-text>.` for the manual actions that have no canonical mapping. Here no pack drives the supply, so its settings are `Operator:` steps too; a supply pack's doc shows the same idiom with that pack's steps.

### Operator-source (raw prose)

```
## Title
Capacitor charge time

## Description
Charge C1 from the bench supply and check VCAP rises above 4.5 V within 30 s.

## Test steps
- Connect the DMM + to TP VCAP and - to GND.
- Set the bench supply to 5 V / 1 A, output OFF, wired to TP VCAP.
- Discharge C1 by shorting TP VCAP to GND through R_BLEED then remove.
- Measure timestamp as {1}.
- Turn the supply output ON.
- Measure voltage on TP VCAP every 100 ms until it reaches 4.5 V or until 30 s have passed and store last value as {2}.
- Measure timestamp as {3}.
- Compute {3} - {1} as {4}.
- Turn the supply output OFF.

## Success conditions
- {2} >= 4.5 V
- {4} <= 30 s
```

### Canonical rewrite

```
# CAP_CHARGE_TIME_001
Charge C1 and verify VCAP rises above 4.5 V within 30 s.

## Meta
format_version: 2.0.1
board: BRD-DEMO
rules_pack: rules_packager_base@<version>
requirement: REQ-CAP-001 — VCAP must rise to 4.5 V within 30 s of supply enable.

## Equipment
DMM_GENERIC : dmm

## Steps
1. Connect DMM_GENERIC + to TP VCAP.
2. Connect DMM_GENERIC - to TP GND.
3. Operator: set the bench supply to 5 V / 1 A with its output OFF, wired to TP VCAP.
4. Operator: discharge C1 by shorting TP VCAP to TP GND through R_BLEED then remove the short.
5. Timestamp as {1}.
6. Operator: switch the bench supply output ON.
@DO charge_k
7. Wait 100 ms.
8. Operator: read DC voltage on TP VCAP with DMM_GENERIC as {2}.
9. Timestamp as {3}.
10. {4} = {3} - {1}.
@WHILE {2} < 4.5 V AND {4} < 30.0 s
@ENDDO
11. Operator: switch the bench supply output OFF.

## Expected
{2} >= 4.5 V
{4} <= 30.0 s
```

### Transforms applied

| Operator-source | Canonical |
|---|---|
| *set the bench supply to 5 V / 1 A, output OFF* | `Operator: <free-text>.` — no pack in this procedure drives the supply (output-OFF affirmation preserved verbatim) |
| *discharge C1 ... through R_BLEED then remove* | `Operator: <free-text>.` — no canonical mapping for arbitrary fixture manipulation |
| *measure timestamp as {N}* | `Timestamp as {N}.` |
| *compute {a} - {b} as {c}* | `{c} = {a} - {b}.` |
| *measure ... every T until X or timeout Y, store last value as {v}* | `@DO ... Wait T. + Operator: read ... as {v}. + Timestamp as {t}. + {elapsed} = {t} - {t0}. @WHILE {v} < X AND {elapsed} < Y. @ENDDO` |
| *turn the supply output ON/OFF* | `Operator: switch the bench supply output ON.` / `OFF.` |

`{1}` (charge start) and `{3}` (last per-iter timestamp) are info-only — the rewriter is **not required** to add a criterion in `## Expected` for every bound ref; only the values that drive a pass/fail decision need a criterion.

## Appendix: preserve-verbatim worked example

A short procedure demonstrating verbatim preservation of: leading-`+` probe points, dual-pin alternates, ground-rail labels, via clauses, sub-designators with `.`, and operator-written units (`mV`, `ms`). No pack drives the supply or the load here, so their wiring is free text and their settings are `Operator:` steps; each pack's doc shows the same care with its own steps (a probe channel's reference-ground wiring, for one).

```
# RAIL_28V_OV_001
Verify the +28 V rail and its 5 V regulator at nominal load. Check overvoltage trip above 30 V.

## Meta
format_version: 2.0.1
board: BRD-A1
rules_pack: rules_packager_base@<version>

## Equipment
DMM_GENERIC : dmm

## Steps
1. Connect the bench supply + to P4, - to P2.
2. Connect the electronic load + to P15 pin 29/30, - to P15 pin 27 (via 6.3mm jack).
3. Connect DMM_GENERIC + to TP +HIGH_28V, - to P3 pin 20 (GND_PWR).
4. Operator: set the electronic load to 1 A and switch it on.
5. Operator: set the bench supply to 28.0 V / 5 A and switch it on.
6. Wait 100 ms.
7. Operator: read DC voltage on TP +HIGH_28V with DMM_GENERIC as {1}.
8. Connect DMM_GENERIC + to U46.1 pin 5, - to TP GND.
9. Operator: read DC voltage on U46.1 pin 5 with DMM_GENERIC as {2}.
10. Connect DMM_GENERIC + to TP +VCC_5V0, - to TP GND.
11. Operator: read AC voltage on TP +VCC_5V0 with DMM_GENERIC as {3}.
12. Operator: set the bench supply to 32.0 V.
13. Wait 100 ms.
14. Operator: judge the overvoltage trip on TP +HIGH_28V (target = "TRIPPED") as {4}.

## Expected
{1} = 28.0 V +/- 1 V
{2} = 5.0 V +/- 5 %
{3} < 50 mV
{4} operator decides "TRIPPED"
```

Pattern coverage in this example:

| Failure mode the example fixes | Hit by |
|---|---|
| Dual-pin alternate `pin 29/30` | step 2 |
| Via clause `(via 6.3mm jack)` | step 2 |
| Probe-point with leading `+` (`+HIGH_28V`, `+VCC_5V0`) | steps 3, 7, 10, 11, 14 |
| Ground-rail label kept after the pin `(GND_PWR)` | step 3 |
| Component sub-designator with `.` (`U46.1`) | steps 8, 9 |
| Operator-written `mV`/`ms` (no SI-normalization) | steps 6, 13 (`100 ms`), criterion `{3}` (`50 mV`) |
