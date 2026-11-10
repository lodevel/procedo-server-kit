# Procedo server contract

What a Procedo server must receive, send back and refuse. How it stores anything (a database,
files, a cloud bucket) is its own business. Any server that answers the calls below under these
rules is a correct one.

> **What this contract does not ask.** No database (SQL or not), language, framework, operating
> system or hosting. Every rule below is something the app sees: what the server sends back, what
> it refuses, and what the app reads back later. How the server gets there inside is free. The
> reference server of the conformance kit (§8) keeps everything in memory: it is an example to
> compare answers against, not a design to copy. Section 7 (file storages the app writes to itself)
> matters only to a server that offers them. Many calls are optional (6.1): a server lists in
> `/info` those it offers, and answers the others `501 unsupported`.

Levels: **MUST** means the app cannot work without it. **SHOULD** means one feature degrades
without it. Every rule stated without a level is a MUST.

Contents:

0. [Before you start: what you must, may and choose](#0-before-you-start-what-you-must-may-and-choose)
1. [What the server is for](#1-what-the-server-is-for)
2. [Conventions](#2-conventions)
3. [Address, headers, discovery, authentication, transport](#3-address-headers-discovery-authentication-transport)
4. [Errors: how a server says no](#4-errors-how-a-server-says-no)
5. [The objects](#5-the-objects)
6. [The calls](#6-the-calls)
7. [File storages the app reaches itself](#7-file-storages-the-app-reaches-itself)
8. [Conformance: checking your server](#8-conformance-checking-your-server)
9. [The machine-readable twin](#9-the-machine-readable-twin)

---

## 0. Before you start: what you must, may and choose

**You must** answer these core calls, each as its section says:

- `GET /info`: [3.3](#33-get-apiv1info-discovery)
- S1 `GET /storages`: [6.6](#rule-S1.storages)
- P1 `GET /projects`: [6.3](#rule-P1.light-with-path)
- P2, P2b `GET /projects/{project}` · `POST …/read`: [6.3](#rule-P2.whole)
- P7 `GET /projects/{project}/tests/{test}`: [6.3](#rule-P7.one-test)
- P9 `POST /projects/{project}/changes`: [6.3](#rule-P9.shapes)
- P10 `POST /projects/{project}/releases/{test_id}`: [6.3](#rule-P10.only-named-files)
- E1 `POST /projects/{project}/runs`: [6.4](#rule-E1.creates-project-board)
- E3 `GET /projects/{project}/runs/{run_id}`: [6.4](#rule-E3.run)
- E4 `GET /projects/{project}/boards`: [6.4](#rule-E4.list)
- E5 `GET /projects/{project}/boards/{serial}`: [6.4](#rule-E5.board)
- E7 `POST /projects/{project}/runs/{run_id}/annotation`: [6.4](#rule-E7.statuses)
- E8 `GET /projects/{project}/artifacts`: [6.4](#rule-E8.filters)
- E9 `GET /projects/{project}/artifacts/{kind}/{name}`: [6.4](#rule-E9.generation)
- E10 `POST /projects/{project}/artifacts`: [6.4](#rule-E10.report-already)
- B1, B2 `POST`/`GET /storages/{store_id}/blobs/{owner}/{sha256}`: [6.6](#rule-B1.upload)

**You may** offer any other call, or not: the [table of 6.1](#61-every-call-at-a-glance) says
what the app does without each.

**You choose:**

- who registers the boards: [`boards_registered`](#rule-3.3.info-boards-registered);
- your users, and whether a run or release naming nobody is taken: [U1](#rule-U1.users),
  [the absent name](#rule-U1.unnamed-choice);
- no imports: [`import`](#rule-6.1.import-unlisted-501); the kit then skips the import's cases;
- no project creation: [P11](#rule-6.1.create-unlisted-501); the kit then skips the cases that need
  a fresh project (adapt the kit to check them on projects of your own);
- who may be a manager, so who deletes: [E11–E13](#rule-6.1.manager-calls-need-a-manager);
- your own refusals and codes: [4.1](#41-the-envelope), [409, kind `invalid`](#rule-4.3.409-own-codes);
- no P3: [P2](#rule-P2.token-empty-without-P3);
- a compressed part of a multipart request: [2.12](#rule-2.12.plain-json-part);
- your storages, and the ones projects declare: [7.1](#71-your-own-file-storages),
  [S2–S4](#rule-7.3.declared-never-cleaned);
- your limits and retention period: [`/info` `limits`](#33-get-apiv1info-discovery);
- purging on your own schedule too: [E13](#rule-E13.not-expired-kept);
- a page size of your own: [E2 `limit`](#rule-E2.limit-cursor);
- a body's `400` against a `404`: [2.13](#213-order-of-checks).

**Check it** with the conformance kit ([8.2](#82-run-the-procedo-server-conformance-kit-against-your-server)). To read
the tests and runs the app sends, see `docs/reading_a_test.md` in the kit.

---

## 1. What the server is for

Procedo is a desktop application for electronics test labs. People write test procedures in
it, and test benches (PCs wired to instruments) run them on boards. **The app is the server's
only client.** One server holds, for one company:

- **projects**: folders of files (test procedures, Python functions, configuration, documents,
  a bundle of instrument support);
- **tests** inside a project, and their **releases** (frozen, numbered versions of a test);
- **evidence**: every **run** of a test on a board (its record, measurements, pictures, logs),
  the **boards** themselves, comments and hand-changed verdicts, and **reports** and
  **exports** made from the runs;
- a **library** of installable items (bundles, configurations, templates, equipment packs).

Everything is JSON over HTTPS, with two methods: `GET` reads, `POST` writes. A file is named in
the JSON by its sha256; its bytes travel as binary parts of the same request, travel alone when
large, or sit in a file storage (a share, FTPS, SFTP) that the app reads and writes itself.

Words used below:

| Word | Meaning |
|---|---|
| the app | The Procedo application, on any PC. |
| a bench | One PC running the app, known by its `machine_id`. |
| a login | Whatever a bearer token authenticates. Per person, per bench or one per lab: the company chooses; the API never assumes either. |
| the outbox | The app's queue of writes made while offline. It sends them later, byte for byte as first built. |

---

## 2. Conventions

### 2.1 Design rules

Everything below follows from these.

1. <a id="rule-2.1.two-methods"></a>**Two methods.** `GET` reads, `POST` writes. No `PUT`, `DELETE`, `PATCH` or `HEAD`.
2. **JSON carries the structure; a file is a reference.** Every file (a picture, a log, an
   archive, a PDF) is named in the JSON by its sha256, its size and the **storage** holding its
   bytes. The exception: a project's text documents travel inside the JSON as text (2.9).
3. **Bytes never travel as base64.** When the storage is the server itself (backend `api`),
   the bytes are binary parts of the same request or answer. Any other storage is read and
   written by the app directly, and the API carries only references.
4. **One JSON request per user action.** Open a project, sync it, push its changes, push a run,
   read one run, load a board's results, load one test or a list of tests: one request each.
   A file over 1 MiB adds one request of its own (6.6).
5. **Content addressed; nothing is sent twice.** A blob already in its storage is never sent
   again. A reader lists what it holds (`have`) and the server leaves those parts out. A writer
   sends the bytes first, then the JSON. A reference to bytes the storage lacks is refused and
   nothing is written.
6. **Every item has a version; an older one never overwrites a newer one.** A write names the
   version it replaces. When the item changed meanwhile, that item alone is refused, with what
   the server holds now. The other items of the request still apply.
7. **A run is written once.** Nothing a bench sends changes or removes a stored run. Only a
   manager deletes it, softly, and can restore it.
8. **Every write can be replayed.** Benches queue writes offline and send them later. The same
   request sent again gives the same result and never makes a second copy.
9. **A change token per project.** `?since=<token>` answers only what changed since (P3). P3 is
   optional: a server without it answers an empty token, and the app reads the whole project
   each time (P2).
10. **Paging only where a search is unbounded** (runs across boards and dates, artifacts across
    boards), with an opaque cursor.
11. **A missing file never hides what refers to it.** A run whose picture is gone still opens;
    the picture is reported missing.

### 2.2 Field names, unknown fields, absent and `null`

| Item | Rule |
|---|---|
| Field names | `snake_case`, ASCII. |
| <a id="rule-2.2.unknown-fields-kept"></a>Unknown fields | **Kept.** A server stores every field it does not know and returns it unchanged. <a id="rule-2.2.unknown-null-absent"></a>One sent as `null` is "not known", as any field's `null` in a request: it is not kept and reads back absent. Inside an unknown field's value, everything is kept as sent, `null` included. |
| <a id="rule-2.2.no-null-in-answers"></a>Absent and `null` | In a request, an optional field may be absent or `null`: both mean "not known". An answer leaves out a field it has no value for, never sends `null`, except where `null` is itself a value: a pointer's `run_id`, an entry's `text`, `value` and `replaces`, a draft's `replaces`, a list's `cursor`, E5's `pointer` and E11's `pointer`. A store-then-read-back treats absent and `null` as the same. |
| Empty string | `""` never means "not known". |
| <a id="rule-2.2.answer-fields-ignored"></a>*answer* fields | Fields marked *answer* in the tables of section 5 are set by the server. When a client sends them, they are ignored (except in an import, 6.7). |

### 2.3 Identifiers

| Id | Form | Rules |
|---|---|---|
| `project_id` | UUID, lower case | Made by the app. The project's `config/config.json` holds the same. |
| `test_id` | UUID, lower case: `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` | A test's hidden id (2.5). |
| `run_id` | `YYYY-MM-DDTHH-MM-SSZ_<16 hex>`: `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z_[0-9a-f]{16}$` | The run's start in UTC, then exactly 16 lower-case hex digits (64 random bits). Made by the bench. Unique within its project. `(project_id, run_id)` is a run's key everywhere. |
| `machine_id` | 8 lower-case hex digits: `^[0-9a-f]{8}$` | A bench's identity, made once per PC, never typed. |
| `store_id` | integer | Given by the server to each storage. |
| `entry_id` | string (a UUID) | Made by the app when a person saves an annotation or board entry, so a replay is the same entry. Unique within its run (annotation) or board (board entry), not across them. |

### 2.4 Names and case

The **names** are: a test's name in a project (`test`, its folder's name), `serial`,
`part_number`, `board_revision`, an artifact's `name`, and the names in a project path. Each
picks out a thing.

- <a id="rule-2.4.nfc"></a>A name is stored in Unicode **NFC**. One sent in another form is stored as its NFC form.
- Names are compared by NFC, then Unicode full case folding (Python's `str.casefold()`).
- <a id="rule-2.4.case-first-spelling"></a>A name that differs from a stored one only by case **is** the stored one. The server keeps
  the first spelling it met, and every answer gives that spelling. So a name read back may
  differ in case from the one sent, and in nothing else.
- <a id="rule-2.4.record-kept-as-sent"></a>A test's name kept **as a record** picks out nothing: a Run's and a Release's `test`, a
  report's `tests`. It is stored in NFC as sent and never respelled. A search compares it by
  case folding.
- <a id="rule-2.4.exact-exceptions"></a>Exceptions compared exactly: a board field's `name` (5.11), a library item's
  `<id>@<version>` (5.16), a declared storage's `identity` (5.15), and the paths that never
  travel (5.2).

### 2.5 The test id

Every test has a hidden `test_id`. The user never sees it.

- The app makes it when a test is created. Duplicating a test makes a new one; renaming keeps it.
- It is kept in the test's folder, in `tests/<test>/test.json`, as `"test_id"` beside
  `"active"`: `{"active": "text", "test_id": "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17"}`.
- <a id="rule-2.5.test-id-from-test-json"></a>**The server reads it there and nowhere else**, as it reads the test's `format` from the same
  file (5.3).
- It is unique within its project.
- <a id="rule-2.5.filed-under-id"></a>A test's **releases**, its **runs** and its **current-run marks** are filed under its
  `test_id`, never its name. A rename moves none of them. A deleted test leaves all three under
  its id. Any name no test holds now may be taken by a new test, which has its own id and none
  of the old test's releases, runs or marks.
- <a id="rule-2.5.malformed-test-id-400-first"></a>A `{test_id}` in a path (P8, P8b, P10, E6), or E2's `test_id` filter, that is not a lower-case
  UUID (a test's name, say) is refused with `400 bad_request`. This check comes **before** the
  project's existence is looked at.

### 2.6 The board

<a id="rule-2.6.board-three-parts"></a>A board is its `part_number`, `board_revision` and `serial` **together**. Two boards may share a
serial. A new revision is a new board, with no results of its own.

| Rule | |
|---|---|
| Optional parts | `part_number` and `board_revision` may be absent: the project made them optional, or the result is a format-1 one (5.5). |
| Comparison | Each of the three is compared as a name (2.4). |
| <a id="rule-2.6.empty-value-400"></a>No empty value | None of the three is ever `""` (absent means none). The one exception: a project-level export's `serial` is `""` (5.10). A server refuses `""` with `400 bad_request`. |
| <a id="rule-2.6.blanks-400"></a><a id="rule-2.6.filter-matches-nothing"></a>No blanks around | None of the three begins or ends with a blank. A server refuses one that does with `400 bad_request` wherever a call names one board or sends one. In a list or search filter, such a value is not refused: it matches nothing, as any value no board has. |
| <a id="rule-2.6.query-empty-is-none"></a>In a query | A query parameter naming one of them with an empty value means "none". |
| The "no part number" board | The board with neither a part number nor a revision. It holds every format-1 result and the runs of a project that made both optional. |
| `_unsorted` | A run made with "no serial" chosen at the bench carries the serial `_unsorted`. For a server it is a board like any other: nothing to do. |

### 2.7 Times

- <a id="rule-2.7.time-format"></a>**UTC, ISO 8601, with `Z`**: `2026-10-26T22:20:42Z` (pattern
  `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$`).
- Each field has one fixed precision: seconds, except a Phase's `started` and `ended` and
  `/info`'s `server_time`, which carry milliseconds: `2026-10-26T22:20:42.727Z`.
- Within one field, string order is time order. The pointer rule relies on it (5.13).
- A run's record file (`results.json`) keeps its own time forms (epoch milliseconds). It
  travels as a file and is never rewritten.

### 2.8 Hashes, versions, numbers, own fields

| Item | Rule |
|---|---|
| <a id="rule-2.8.digest-forms"></a>Hashes | `sha256`, every `*_sha256`, `fingerprint` and `content`: 64 lower-case hex digits, bare (`^[0-9a-f]{64}$`). |
| Versions | `version`, `replaces` (of a file), a part's name, an entry of `have`, `details.missing`: the same digest written `sha256:<hex>` (`^sha256:[0-9a-f]{64}$`). |
| Numbers | JSON numbers. A value JSON cannot hold (NaN, infinity) is never written as a number (5.7). |
| <a id="rule-2.8.own-fields-strings"></a>Project's own fields | The values of a run's `extra`, a report's `fields` and a board's `details` are strings (`{"Customer": "ACME"}`). Another type is refused: `400 bad_request`. |
| <a id="rule-2.8.format-version-400"></a>`format_version` | An integer on every top-level object: Project, Test, Release, Run, Annotation, Artifact, Board, BoardResults, Bench, Storage, LibraryItem. It is `2` for Run, Artifact, Board and BoardResults (a Run or an Artifact may also be `1`, 5.5), `1` for the others. Nested objects (File, Bundle, Draft, Phase, Measurement, RunFile, TestPointer, BoardEntry) carry the version of the object holding them. A new optional field does not change it. A value the object does not have: `400 bad_request` with `details: {"format_version": <sent>, "known": <highest taken for that object>}`. |

### 2.9 Text documents and references

<a id="rule-2.9.text-or-reference"></a>A project file under `tests/`, `functions/`, `config/` or `scenarios/` that is valid UTF-8
travels **inside the JSON as `text`**, whatever its size. Every other file (an archive,
`documents/`, the bundle archive, a stray file at the root), and any file under those four
folders that is not UTF-8, is a **reference** to a storage.

The rule depends on the path and the bytes only, so a file never switches form without changing.

<a id="rule-2.9.wrong-form-400"></a>A file sent in the wrong form refuses **the whole request** (`400 bad_request`, nothing written):

- text outside those four folders;
- a reference under them whose bytes are UTF-8 (a server checks the bytes it holds or receives
  in an `api` storage; another storage's it cannot check);
- <a id="rule-2.9.text-size-version-400"></a>a text whose UTF-8 bytes are not the `version` and `size` it states.

### 2.10 Project paths

A project file's path is its `/` path in the project folder.

- <a id="rule-2.10.path-case"></a>Paths compare as names (NFC, then case folding). Two paths that differ only by case are one
  file. A file written under another case of a held path replaces it and keeps the held
  spelling. A new file takes the spelling of the folders it lands in. <a id="rule-2.10.move-case-respells"></a>A move whose destination
  differs only by case changes the spelling.
- <a id="rule-2.10.path-names-400"></a>Every name in a path must be one every PC (Windows included) creates as spelled:
  - not empty, `.` or `..`;
  - no `/`, `\`, control character, or any of `<>:"|?*`;
  - no trailing dot or blank;
  - not a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`),
    whatever its extension and case;
  - at most 255 characters.
- The whole path has no blank at either end.
- Another path refuses the whole request: `400 bad_request`.

### 2.11 Query parameters

<a id="rule-2.11.unknown-query-param"></a>A call **refuses** a query parameter it does not define. It never ignores one: `400
unsupported_filter` on the two searches (E2, E8), `400 bad_request` on every other call.

### 2.12 Compression

- <a id="rule-2.12.gzip-json-body"></a>A plain JSON body is sent with `Content-Encoding: gzip` when the other side accepts it.
- <a id="rule-2.12.plain-json-part"></a>In a multipart body, up or down, no part carries a `Content-Encoding`: the `json` part
  is plain JSON, a file part the file's bytes. Many multipart parsers drop a part's headers, so a
  gzipped part would be unreadable to them. A server may ignore a request part's
  `Content-Encoding` (reading its bytes as they are) or refuse the request, `400 bad_request`.
- A multipart request body is sent plain as a whole too, never with a `Content-Encoding`. A
  server need not accept one that carries it: it may refuse the request, `400 bad_request`.
- <a id="rule-2.12.no-gzip-multipart-answer"></a>A server never compresses a whole multipart answer.
- <a id="rule-2.12.digest-decoded"></a>Any digest of the JSON (the replay check of E1) is of its bytes with every encoding undone.

### 2.13 Order of checks

A request can break several rules at once. The server answers for the **first** one it breaks,
in this order:

1. **Transport**: plain `http` (3.1, 3.6).
2. <a id="rule-2.13.too-large-first"></a>**The body's size, from the headers alone**: `411 length_required`, then `413 too_large`
   (3.6). Nothing is looked at before it, neither the token nor the path: a request over its
   limit is `413`, even when it names a project or a storage the server does not hold.
3. <a id="rule-2.13.token-first"></a>**The token**: `401` (3.4). A request without a valid token is `401`, whatever else is
   wrong with it (an undefined query parameter included).
4. **The call**: `405` / `501 unsupported` for a method or a call the server does not take (4.3),
   an optional call it does not list in `/info` included (6.1).
5. <a id="rule-2.13.parameters-before-item"></a>**The request's form**: `400 bad_request` (`unsupported_filter` on E2 and E8) for a
   malformed id in the path (2.5), a query parameter the call does not define (2.11) or
   gives twice, E17's `test` missing or empty, a request `Content-Type` the call does not take,
   a body that does not parse, a part over 1 MiB. An undefined query parameter is `400` even
   when the path names nothing the server holds.
6. **An import not taken**: `501 unsupported` for an `"import": true` in the body of a call that
   takes one, on a server not listing `import` (6.1). It needs the body read, so it comes after
   5, and before the login's roles: whichever login sends it.
7. <a id="rule-2.13.role-before-item"></a>**The login's roles**: `403` (3.5). A login whose roles do not allow the call is `403`, even
   when what it names does not exist.
8. **The call's own checks**, as its section states them: what the request names (`404`) comes
   before what it asks against what the server holds (`409`, `422`). A body field's own `400`
   comes after 7; against a `404` its order is the server's choice (the app never sends one).

One call differs, as 6.1 says: **P9** looks for its project (7, then the `404` of a project known
only by its evidence) before it reads its body, so its body's form (5) comes after them.

---

## 3. Address, headers, discovery, authentication, transport

### 3.1 Address

| Item | Rule |
|---|---|
| <a id="rule-3.1.base-slash"></a>Base address | The user configures **one** address per server, for example `https://serve.com/testprocedure/`. Every request is that base + `api/v1/…`. The app adds exactly one trailing `/` before appending, so `…/testprocedure` and `…/testprocedure/` are the same server. Nothing else is configured and nothing is found by probing. |
| <a id="rule-3.1.path-prefix"></a>Path prefix | **A base with a path prefix MUST work.** Being mounted under a sub-path behind a reverse proxy is the normal case. The server serves whatever prefix it is reached through, never needs to know its external address, and emits nothing that could contradict it. |
| API prefix | `/api/v1`. Only the major version is in the path. |
| <a id="rule-3.1.no-absolute-url"></a><a id="rule-3.1.opaque-continuation"></a>No URLs in answers | No answer body or header carries an absolute URL of this API. A `cursor`, a change `token` and every other continuation value is an **opaque token**, not a link. The app sends it back on the base it already has. |
| <a id="rule-3.1.segment-decoding"></a>Path segments | A segment carrying a name (`{test}`, `{serial}`, an artifact's `{name}`, a library item's `{id}` and `{version}`) is that name in UTF-8, percent-encoded on its own. The server splits the path on literal `/` first, then percent-decodes each segment exactly once. So a name holding `/` (sent as `%2F`) stays one segment. |
| <a id="rule-3.1.query-plus"></a>Query values | `+` is a literal plus, never a space (a part number may hold `/`, `#` or `+`). |
| <a id="rule-3.1.plain-http"></a>Plain HTTP | Only a test server on the loopback address may serve plain `http`. The app reaches it only with an explicit "insecure" setting. Every other rule stands. |

In this document, paths are written without the `/api/v1` prefix (`/projects` means
`<base>api/v1/projects`), except in section 3.

### 3.2 Headers on every answer

| Header | Rule |
|---|---|
| <a id="rule-3.2.x-bench-store"></a><a id="rule-3.2.x-bench-store-own-process"></a><a id="rule-4.3.5xx-signed"></a>`X-Bench-Store: v1` | On **every** answer, every error included. It MUST be set by the API process itself, not by a proxy. |
| <a id="rule-3.2.x-request-id"></a>`X-Request-Id` | On every answer, 2xx included. Echoed in the error envelope's `request_id`. |
| <a id="rule-3.2.date"></a>`Date` | On every answer (RFC 9110). The app reads it to measure clock skew. |

**Why `X-Bench-Store` matters.** The app treats a `404` as **proof** that an item is absent ("no
run with that id"). A captive portal, a misrouted proxy or an expired gateway also answers 404.
So a 404 **with** `X-Bench-Store: v1` means absent; a 404 without it means the app never reached
the server, and it goes offline instead of believing the item is gone.

### 3.3 `GET /api/v1/info`: discovery

Authenticated. With a valid token:

```
GET /testprocedure/api/v1/info
Authorization: Bearer <token>

HTTP/1.1 200 OK   Date: Thu, 05 Nov 2026 12:00:00 GMT   X-Bench-Store: v1   X-Request-Id: 7c1f…
{"server": "bench-store-01", "api_versions": ["1"], "server_time": "2026-11-05T12:00:00.317Z",
 "limits": {"max_upload_bytes": 67108864, "max_file_bytes": 268435456, "retention_days": 90},
 "calls": ["P3", "E2", "E6", "E15", "U1"], "boards_registered": false,
 "user": {"name": "jdoe", "roles": ["bench"], "token_expires": "2026-12-31T00:00:00Z"}}
```

<a id="rule-3.3.info-fields"></a>It answers exactly these fields, nothing else:

| Field | Meaning | Level |
|---|---|---|
| `server` | The server's name, shown to the user. | MUST |
| <a id="rule-3.3.info-api-version-1"></a>`api_versions` | The major versions served, as strings. The app speaks `"1"` and refuses a server that does not list it. | MUST |
| `server_time` | The server's clock, UTC, milliseconds. The app warns the user above 120 s of skew. | MUST |
| `limits.max_upload_bytes` | The largest JSON request, its parts included (6.6). Integer ≥ 1. | MUST |
| `limits.max_file_bytes` | The largest file sent alone (B1). Integer ≥ 1. The app checks a file against it before sending. | MUST |
| `limits.retention_days` | How long a deleted run or artifact is kept before a purge removes it, in days. 90 unless the server's administrator set another. Integer ≥ 1. | MUST |
| <a id="rule-3.3.info-calls"></a>`calls` | The **optional** calls this server offers, by their ids in 6.1 (`"P3"`, `"E2"`, `"U1"`, …), and `"import"` when it takes imports (6.1, 6.7): each at most once, in any order, `[]` when it offers none. A core call is never listed: every server takes them all. An optional call not listed answers `501 unsupported` (6.1). | MUST |
| <a id="rule-3.3.info-boards-registered"></a>`boards_registered` | Whether the server **registers its boards itself**. `false` (the usual case): any serial a bench runs becomes a board with its first run (E1). `true`: someone adds the boards on the server before they are tested. Each board of a project is then registered or not, and <a id="rule-E4.registered-listed"></a>E4 lists exactly the registered boards, no more, no less, one no run was pushed to yet included. A bench's write naming a board that is not registered is refused `409 unknown_board`, kind `invalid` (4.4), nothing written: <a id="rule-E1.unknown-board"></a>a run (E1), <a id="rule-E10.unknown-board"></a>a report or a draft (E10), <a id="rule-E15.unknown-board"></a>a board entry (E15); the outbox sets it aside. <a id="rule-3.3.listed-taken"></a>On a registered board none of them is ever refused so: a board E4 lists and a bench's write refuses `unknown_board` is a broken server. The app then lets a bench pick a board only from E4's list. <a id="rule-E1.unknown-board-exempt"></a>Not refused so: an imported run (`"import": true`, 6.7), <a id="rule-3.3.import-any-call"></a>and an imported report, draft or board entry alike (E10, E15), <a id="rule-3.3.import-registers"></a>whose board, when new, becomes registered (E4 lists it from then on, and a bench's write on it is taken), the one way a board is registered through these calls (a server not listing `import` registers its boards its own way only); a replay of a run or a report already stored. | MUST |
| `user.name` | The login's name. | MUST |
| <a id="rule-3.3.info-roles"></a>`user.roles` | The login's roles, from `bench`, `publisher`, `importer`, `manager`, `reader` (3.5); `importer` only on a server listing `import` in `calls`. | MUST |
| <a id="rule-3.3.info-token-expires"></a>`user.token_expires` | When the token stops working (time). The app warns within 14 days of it. | SHOULD |

<a id="rule-3.3.info-401-public"></a>**Without a token it still answers**, and that answer matters: `401`, `WWW-Authenticate:
Bearer`, `X-Bench-Store: v1`, and an error envelope (4.1) whose `details` holds
`{"api_versions": ["1"], "server": "<name>"}` and nothing else (no limits, no user). That is the
whole public surface. It lets the app tell "not a Procedo server" (no `X-Bench-Store`) from "a
Procedo server, needs a login" from "a Procedo server not speaking API v1", before it has a
credential. A malformed, expired or revoked token gets the same answer with
`WWW-Authenticate: Bearer error="invalid_token"`.

<a id="rule-3.3.info-403"></a>A valid token that may not use this server: `403`, never `401`.

`/info` doubles as the credentials test in the app's settings, and tells the app the login's
roles, the server's optional calls and whether it registers its boards, so it can grey out a button instead of failing in front of
an operator. Whether a write is allowed is still proved by the write itself.

### 3.4 Authentication

**A per-user personal access token in `Authorization: Bearer <token>`, over TLS.** One scheme, no
negotiation: not Basic, not mutual TLS as a client requirement, not a browser login flow (the app
uploads unattended).

| Rule | Level |
|---|---|
| <a id="rule-3.4.header-only"></a>The app sends the token on every call. It appears **only** in the `Authorization` header: never in a URL, a query string, a redirect or an error body. | MUST |
| <a id="rule-3.4.no-token-401"></a>No token at all: **401** with `WWW-Authenticate: Bearer` (no error code in that header, as RFC 6750 §3.1 says), on every call. The envelope's `code` is `invalid_token`. | MUST |
| <a id="rule-3.4.bad-token-401"></a>A malformed, expired or revoked token: **401** with `WWW-Authenticate: Bearer error="invalid_token"`; `code` `invalid_token` or `expired_token`. | MUST |
| On `/info`, both 401s carry the public `details` of 3.3. | MUST |
| A valid token without the right: **403**. **Never the other way round**: a 403 for a bad password sends the user to an access list; a 401 for a permission problem opens a credentials dialog that cannot help. | MUST |
| The app never stores the token in a project file; it keeps it in the PC's credential store, per server address and user. A server never expects it anywhere but the header. | MUST |

How tokens are issued, stored, scoped, rotated or revoked is the server's business.

### 3.5 Logins and roles

A login holds one or more roles. <a id="rule-3.5.every-login-reads"></a>**Every login reads**, whatever its roles: a role only adds
writes. "any" in the call tables means any login. The bench a run or change came from is told by
the data itself (`machine_id`), never by the login.

| Role | May |
|---|---|
| `bench` | Every read. P9, P10, P11. E1, E6, E7, E10, E15. L3 for an item with no code. S2, S3, S4. B1. |
| `publisher` | Every read. L3 for any item, code included. B1 for owner `library`. |
| <a id="rule-import.never-restore-purge"></a>`importer` | Every read. With `"import": true` in the body: E1, E6, E7, E10, E15, P10, and the deletes of E11 and E12, keeping the server-set fields sent (6.7). B1 for a project's file. **Never** a restore or a purge. The right to move data in: the administrator gives it to the account that does the move, beside its other roles, and takes it back afterwards or not, as they choose. Only on a server listing `import` in `/info`'s `calls` (6.1): <a id="rule-3.5.importer-only-with-import"></a>one not taking imports grants it to no login, and its `/info` never names it. |
| <a id="rule-3.5.manager-only-no-push"></a>`manager` | Every read. E11, E12, E13. A login holding only this role cannot push a run. A server where no login can hold it does not offer E11–E13 (6.1). |
| `reader` | Every read. |

- <a id="rule-3.5.write-needs-role"></a>A write needs a role that allows it, else `403 forbidden`.
- An importer's write without `"import": true` needs the role the call names (`bench` for E1),
  else `403 forbidden`.
- <a id="rule-3.5.importer-beside-bench"></a>A login may hold `importer` beside `bench`: with
  `"import": true` it keeps the server-set fields sent (6.7), without it it writes as a bench.
  The app moves a project's data in with the login it always signs in with, once the
  administrator has given that login the `importer` role.
- <a id="rule-3.5.not-a-manager"></a>A delete, restore or purge (E11–E13) from a login that is not a manager (nor an importer
  sending `"import": true` on a delete): `403 not_a_manager`.
- The app asks for a manager's login at the moment of a delete, restore or purge, uses it for
  that one request and never keeps it. So a manager's request is never queued: one that cannot
  reach the server fails at once.

### 3.6 Transport

| Item | Requirement | Level |
|---|---|---|
| <a id="rule-3.6.tls"></a>TLS | `https` only (except a loopback test server, 3.1). A corporate private CA is expected; the app is configured with a CA bundle, and its certificate check cannot be switched off. | MUST |
| <a id="rule-3.6.proxy"></a>Proxy | Whatever reverse proxy fronts the API must pass `Authorization` through untouched, allow at least the body sizes `/info` advertises, not rewrite `Date`, and neither strip nor add `X-Bench-Store`. It **streams a request's body** to the API as it arrives, never holding a whole upload first (nginx: `proxy_request_buffering off`), so the answer to an upload reaches the app within 30 s of its last byte. It may buffer answers, as long as each answer's first byte reaches the app within 30 s. These are outcomes the app sees, not settings: README hand check 12. `Expect: 100-continue` and the `413` before the body (below) are the API process's; a proxy that answers `100 Continue` itself is allowed, and the body then travels before the API's `413`. | MUST |
| Client timeouts | The app connects within 5 s, then allows a **stall** of 30 s that restarts on every byte of progress, so a large transfer on a slow link is not cut at 30 s. | (the app's side) |
| <a id="rule-3.6.keep-alive"></a>Keep-alive | HTTP/1.1 persistent connections, idle timeout ≥ 60 s. The app keeps about 4 idle connections per server. HTTP/2 welcome, not required. One connection serves at least five sequential requests. | MUST |
| <a id="rule-3.6.length-required"></a>`Content-Length` | Required on a request with a body. A chunked body with no declared length: `411 length_required`. | MUST |
| <a id="rule-3.6.too-large-before-body"></a>Size limits | Enforced with `413 too_large` **before** the body is read: `limits.max_upload_bytes` for a JSON request (parts included), `limits.max_file_bytes` for a file sent alone (B1). `Expect: 100-continue` is honoured, so a 413 does not cost the body. | MUST |
| Range | B2 only: `Accept-Ranges: bytes`, `206`, `Content-Range`, `416`. A JSON request or answer has no `Range` and no resumable form. | MUST |
| Compression | `Content-Encoding: gzip` on a plain JSON body, both ways (2.12). | SHOULD |
| No compression | Never on a multipart body, as a whole or a part, up or down (2.12); never on a file's bytes (B1, B2). | MUST |

### 3.7 What the app depends on

| # | Guarantee | Why |
|---|---|---|
| G1 | **Absent, refused and empty are three different answers**: a 404 with `X-Bench-Store`, a 403, and a 200 with an empty list. | A refusal read as "empty" shows a full project as holding nothing; an unproved 404 reads as "this run was never pushed". |
| <a id="rule-3.7.G2-all-or-nothing"></a>G2 | **A step the contract calls all-or-nothing is exactly that** (a run push; one P9 change for its path). | A half-stored run lists a test as run with no record; a half-written change leaves a file neither old nor new. |
| <a id="rule-3.7.G3-one-clock"></a>G3 | **One clock, moving forward, not rewritten by a proxy.** `Date`, `server_time` and every time the server sets (`pushed_at`, `received_at`, `released_at`, `mtime`, …) come from the same clock. | The app corrects the bench's clock by the offset it reads from `Date`; the retention period is counted on the server's clock; the pointer rule compares times. |

### 3.8 Advice (not normative)

- **Logging.** One line per accepted and refused write: server time, request id, login, client
  address, the bench's `machine_id` from the body, the call, the bytes, the outcome. That answers
  an auditor's question: who put this here, from which bench, when. Append-only and shipped off
  the machine is tamper-evident enough.
- **Health.** Alert on free space, on the 5xx rate and on **clock drift**: the pointer rule and
  the retention period are times.
- **Security.** Keep tokens out of access logs. Cache no authorization decision for more than a
  minute, so a revocation bites. Rate-limit per token, not per address: all the benches of a lab
  sit behind one NAT. A storage the server reaches on disk: resolve links and refuse anything
  outside its root.

---

## 4. Errors: how a server says no

A server may refuse anything it likes: a quota, a size, a permission. This contract does not say
what a server must allow. <a id="rule-4.3.own-refusals"></a>It says **how a refusal is phrased**, so the app can stop without
losing anything and show the user one clear sentence.

### 4.1 The envelope

<a id="rule-4.1.envelope"></a>Every non-2xx answer carries this body, `Content-Type: application/json`, with `X-Bench-Store`
and `X-Request-Id`:

```json
{"error": {"code": "quota_exceeded", "kind": "denied", "request_id": "7c1f9a02",
           "message": "This project is over its 2 GB quota; ask the server's owner.",
           "path": "documents/datasheet.pdf",
           "retry_after_s": null, "details": {}}}
```

| Field | Req. | Meaning |
|---|---|---|
| `code` | yes | A machine code. A server may invent any code for its own refusals; the ones this contract uses are in 4.4. |
| `kind` | yes | One of the kinds of 4.2. The app acts on it, so an unknown `code` still behaves right. When `kind` is missing or unknown, the app falls back on the HTTP status. |
| `request_id` | yes | The answer's `X-Request-Id`. |
| `message` | yes | **One human sentence. The app shows it to the user**, with `path` when there is one. It is the server's chance to explain itself. |
| `path` | no | The file or item concerned. |
| `retry_after_s` | no | Integer seconds or `null`. |
| `details` | no | An object: what 4.4 names for each code (`missing`, `current`, `changed`, `version`, `large`, `max_upload_bytes`, `max_file_bytes`, `content_sha256`, `format_version`, `known`). |

### 4.2 Kinds

| `kind` | What the app does |
|---|---|
| `invalid` | This one item is wrong. The outbox **sets that entry aside and keeps going**. |
| `not_found` | Proved absence. Not an error state: "no report for this board". |
| `auth` | Credentials rejected: the credentials editor opens. |
| `denied` | The server answered and refused: the message and path are shown; the outbox stops and its entries wait. |
| `unreachable` | The app never reached the server: offline banner, back off, retry later. |
| `timeout` | A transfer stalled: retry with the remaining budget. |
| `corrupt` | The bytes are wrong: set the entry aside, do not retry with the same bytes. |
| `foreign` | A storage marked for another server (7.2). The app concludes it itself; no status means it. |
| `cancelled` | The user stopped it; nothing was written. |
| `internal` | The server broke in a way it could not name. |

### 4.3 Status → kind

| HTTP | `code` | `kind` | Notes |
|---|---|---|---|
| <a id="rule-4.3.400-invalid"></a>400 | `bad_request`, `unsupported_filter` | `invalid` | A malformed request; a name or path a PC cannot hold; a part over 1 MiB (`details.large`); a request `Content-Type` the call does not take; a query parameter the call does not define (`unsupported_filter` on E2 and E8, `bad_request` elsewhere). The API uses neither `406` nor `415`. |
| <a id="rule-4.3.401-auth"></a>401 | `invalid_token`, `expired_token` | `auth` | With `WWW-Authenticate` as in 3.4. |
| <a id="rule-4.3.403-denied"></a>403 | `forbidden`, `not_a_manager`, or any code for the server's own rules | `denied` | A valid token without the right. **Never for a bad token.** |
| <a id="rule-4.3.404-signed"></a>404 | `not_found` | `not_found` | Only **with** `X-Bench-Store`; without it the app reads `unreachable`. |
| <a id="rule-4.3.405-501"></a>405 / 501 | `unsupported` | `invalid` | A method or call this server does not take, an optional call it does not list in `/info` included (6.1); never retried. |
| <a id="rule-4.3.408"></a>408 | `request_timeout` | `timeout` | |
| <a id="rule-4.3.409-invalid"></a>409 | `missing_files`, `changed`, `same_as_release`, `deleted`, `version_exists`, `exists`, `unknown_operator`, `unknown_board` | **`invalid`** | One bad queue entry: `invalid` sets it aside and the queue keeps flushing. (`denied` here would stall every bench's uploads behind it.) |
| <a id="rule-4.3.409-own-codes"></a>409 | any other code, for the server's own rule on one entry (`unknown_project`, `unknown_test`, …) | **`invalid`** | The server refuses this one entry by a rule of its own (a project or test it does not know, say): the outbox sets that entry aside and carries on with the others, as for the codes above. |
| <a id="rule-4.3.409-run-conflict"></a>409 | `run_conflict` | `corrupt` | |
| <a id="rule-4.3.410-gone"></a>410 | `gone` | `not_found` | P3's `since` token cannot be answered from; the app opens the project in full. A server without P3 never answers it: its tokens are `""`, and the app sends no `since` (P2). |
| 411 | `length_required` | `invalid` | A body with no `Content-Length`. |
| 413 | `too_large` | `invalid` | Over `max_upload_bytes` or `max_file_bytes`. |
| <a id="rule-4.3.416"></a>416 | `range_not_satisfiable` | `corrupt` | B2: the copy held is wrong; the app drops it and downloads whole. |
| <a id="rule-4.3.422"></a>422 | `digest_mismatch` | `corrupt` | The bytes did not survive the wire; the app sends them again once, then sets the entry aside. |
| 429 | `rate_limited` | **`unreachable`** | The app backs off. `Retry-After` required. |
| <a id="rule-4.3.500-internal"></a>500 | `internal` | `internal` | |
| 502 / 503 | `unavailable` | **`unreachable`** | The offline path engages; work is queued. `Retry-After` required on 503. |
| 504 | `gateway_timeout` | `timeout` | The write may have landed: the app sends the same request again, which the server answers as a replay. |
| 507 | `no_space`, `quota_exceeded` | `denied` | The server is up; an offline banner would mislead. `Retry-After` required. |

<a id="rule-4.3.retry-after"></a>**`Retry-After`** MUST be delta-seconds (`Retry-After: 30`), never an HTTP date. It is required
on 429, 503 and 507.

### 4.4 The codes this contract uses

| HTTP | `code` | `kind` | When | `details` | The app |
|---|---|---|---|---|---|
| <a id="rule-4.4.details"></a>409 | `missing_files` | `invalid` | A write references a file whose bytes are not where it says (not a part, not in the storage, held only by another project, or held but damaged). Nothing written. | `missing`: the versions | Puts them there and sends the same request again. An answer, not a failure. |
| 409 | `run_conflict` | `corrupt` | A run with this `run_id` exists in the project with different content. | | Sets the entry aside. |
| 409 | `changed` | `invalid` | P10: a file version named is no longer the server's. An import: a release number or report generation already taken by other content. | `current`: what the server holds now (P10: the test's documents now). `changed` (P10): the paths whose version is not the server's. | Shows the conflict. |
| 409 | `same_as_release` | `invalid` | P10: `content` equals a release's under the same `test_id`. | `version`: that release | "Nothing to release: the files are exactly vN." |
| 409 | `deleted` | `invalid` | An artifact push to a name a manager deleted (not a replay); a push of a run a manager purged; an annotation entry for a run a manager deleted. | | Sets the entry aside, says why. |
| 409 | `version_exists` | `invalid` | L3: a library version held with other content. | `content_sha256`: the stored one | "Publish it under a new version." |
| 409 | `exists` | `invalid` | P11: a project with that `project_id` or name is held. L3: an item differing from a held one only by case. | | Says it is on the server already. |
| 409 | `unknown_operator` | `invalid` | A server listing U1: E1's `operator` or P10's `released_by` is none of its users (6.8). Nothing written. | | Sets the entry aside, says the name is none of the server's users. |
| 409 | `unknown_board` | `invalid` | E1, E10, E15 (not an import) on a server whose `/info` says `boards_registered: true`: a run, a report or a draft, a board entry on a board that is not registered, which E4 does not list (3.3). Nothing written. | | Sets the run aside, says the board is not on the server. |
| 409 | any other (the server's own) | `invalid` | One entry refused by a rule of the server's own (4.3). | | Sets that entry aside, shows the message; the others go on. |
| 400 | `bad_request` | `invalid` | Among others: a part over 1 MiB; a path or project name a PC cannot hold; a P11 project breaking the test-id rules (5.3); a wrong `format_version`; a malformed `{test_id}`. | `large` (parts over 1 MiB); `format_version`, `known` | Sends the large file alone (B1) then the request again; refuses the name; says which test. |
| 400 | `unsupported_filter` | `invalid` | A query parameter E2 or E8 does not define. | | Says the server cannot answer that search. |
| 413 | `too_large` | `invalid` | A JSON request over `max_upload_bytes`, or a file alone over `max_file_bytes`. | `max_upload_bytes` or `max_file_bytes` | Says the file is too large for the server. |
| 416 | `range_not_satisfiable` | `corrupt` | B2: a range past the file's end. Header `Content-Range: bytes */<size>`. | | Downloads the file whole. |
| 403 | `not_a_manager` | `denied` | A delete, restore or purge from a login that is not a manager. | | "This login is not a manager." |
| 403 | `forbidden` | `denied` | A write the login's roles do not allow. | | |
| 410 | `gone` | `not_found` | `since` names a token the server cannot answer from. | | Opens the project in full (P2 with `have`). |
| 501 | `unsupported` | `invalid` | A call this server does not take: an optional call it does not list in `/info` (6.1), any call the contract does not have. | | Never retries it. |
| 404 | `not_found` | `not_found` | An item the server does not hold; P10 (not an import) naming a `test_id` no test holds. | | Says it is not on the server. |
| 422 | `digest_mismatch` | `corrupt` | A part's or upload's bytes do not match its name. | | Sends it again once, then sets it aside. |

### 4.5 Refusals inside one request

<a id="rule-4.5.refusal-inside-200"></a>A refusal of one item inside a request is **not** an error. P9 answers `200`, and each change
says `saved`, `changed` or `refused`; a refused one carries its `kind` and `message`. E6, E7, E10
and E15 likewise answer `200` with `changed` for a conflict.

---

## 5. The objects

In the tables, **Req.** is "required in what the client sends". Fields marked *answer* are set by
the server (2.2). Every object keeps unknown fields (2.2).

### 5.1 File

One file of a project, listed under its path.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| <a id="rule-5.1.file"></a>`version` | string | yes | `sha256:<hex>` of the bytes. |
| `size` | integer ≥ 0 | yes | Bytes. |
| `mtime` | time | *answer* | When the server stored this version. Shown, never compared. |
| `text` | string | one of the two | The content, for a text document (2.9). |
| `store_id` | integer | one of the two | Otherwise: the storage holding the bytes. For an `api` storage, a part of the same body (6.6); for another, a blob the app reads there (7.2). |

```json
"tests/VOUT1_ripple_2A/procedure.json": {"version": "sha256:9f2c…", "size": 4120,
  "mtime": "2026-11-04T08:13:20Z", "text": "{\n  \"id\": \"VOUT1_ripple_2A\", …"}
"AX-2210.tgz": {"version": "sha256:41ab…", "size": 2621440, "mtime": "2026-11-03T10:00:00Z",
  "store_id": 2}
```

### 5.2 Project

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| <a id="rule-5.2.project-answer"></a>`project_id` | string | yes | The project's UUID; `config/config.json` holds the same. |
| `name` | string | no (P11: yes) | The project's name, as the app names its folder (rules below). |
| `path` | string | *answer* | Its `name` as first stored, in NFC. In P1's list only. |
| `token` | string | *answer* | The change token: opaque. `""` from a server that does not offer P3 (P2). |
| `updated` | time | *answer* | The last change to its files. |
| `created_at`, `created_by` | time, string | *answer* | When and by whom it was created. |
| `files` | map path → File | yes | Every file of the project by its `/` path, except those listed below. `config/config.json` is the project's configuration. |
| `bundle` | Bundle | no | The project's bundle; absent: none. |
| `releases` | map test_id → list of Release | *answer* | Every release of the project, keyed by its test's `test_id`, each Release with its documents, oldest first. A deleted test's releases included. `{}`: none. Not in P1's list. |
| `drafts` | list of Draft (with `test`) | no | P11 only: the drafts of its tests, as P9 sends them. |
| <a id="rule-5.2.project-missing"></a>`missing` | map path → string | *answer* | Files of an `api` storage the server cannot give, by path (the bundle's archive as `bundle/`): `not_found` or `damaged`. Absent when none. A missing file never hides the project. |
| <a id="rule-5.2.project-store-id"></a>`store_id` | integer | no | The storage this project's new files go to; absent: the server's active one. Set by the server's administrator. |

**Paths that never travel as files.** A project's files are every file of its folder **except**:

- at the root: `evidence/`, `serial_numbers/`, `exports/`, `results/`, `reports/` (the evidence,
  which has its own calls), `.trash/`, `bundle/` (it travels as the Bundle), `releases/` (it
  travels as the Releases);
- at any depth: `__pycache__/`, `.venv`, `.media_cache`, `.bundle_installed.json`, `.project_id`,
  and a folder `.<name>.deleting` (or `.deleting2`, `.deleting3`…) or `.<name>.copying` (or
  `.copying2`…), a test the app is deleting or copying;
- a file named `.project_id.lock`, `.evidence-rename.json`, `.llm_session.json`,
  `.llm_chat_history.json`, `bench_unsafe.json`, `bench_preflight.json` or
  `bench_fingerprints.json`, and any `*.part` or `*.bak`.

These names compare **exactly**. <a id="rule-5.2.never-travel-p11"></a><a id="rule-5.2.never-travel-p9"></a>A request sending such a path: P11 `400 bad_request`; P9
refuses that change alone (`refused`, kind `invalid`).

The files include the whole configuration (everything under `config/`), loaded, synced and
written with the same calls and versions as the tests. A project's evidence is not in its files.

**The name.** <a id="rule-5.2.project-name-400"></a>P11's `name` must be one folder name on every PC: the rules of a path's names
(2.10), no blank at either end. Otherwise `400 bad_request`. <a id="rule-5.2.project-name-unique"></a>It is stored in NFC and compared by
case folding: a second project of the same name is `409 exists`. A project's `name` is unique on
its server; the app shows and records a server's project by its name alone.

**Bundle.** The `bundle/` folder travels as **one zip archive**, never file by file.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `bundle_id`, `bundle_version` | string | yes | The bundle's id and version. |
| `bundle_sha256` | string | no | The bundle's own content hash. |
| <a id="rule-5.2.bundle-archive-kept"></a>`archive` | File | yes | The zip of the folder. Its bytes are kept as sent and never repacked, so its version stays the same. |

```json
{"format_version": 1, "project_id": "0b8f3c52-4d1e-4a8b-9f27-6c3e5d1a7b90",
 "name": "AX-2210 engineering", "token": "c41.7", "updated": "2026-11-04T08:13:20Z",
 "files": {
   "config/config.json": {"version": "sha256:5d0e…", "size": 9812, "mtime": "2026-11-04T08:13:20Z",
     "text": "{\n  \"project_id\": \"0b8f3c52-…\", …"},
   "tests/VOUT1_ripple_2A/procedure_text.md": {"version": "sha256:77de…", "size": 3020,
     "mtime": "2026-11-04T08:13:20Z", "text": "# VOUT1_ripple_2A\n…"},
   "documents/datasheet.pdf": {"version": "sha256:a913…", "size": 1882112,
     "mtime": "2026-11-03T10:00:00Z", "store_id": 2}},
 "bundle": {"bundle_id": "baseline", "bundle_version": "3.0.0", "bundle_sha256": "c0de…",
   "archive": {"version": "sha256:3b7a…", "size": 1782000, "mtime": "2026-11-03T10:00:00Z",
               "store_id": 2}},
 "releases": {"3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17": [ …Release with its documents… ]}}
```

### 5.3 Test and Draft

A test is the folder `tests/<test>/` of a project.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| `test` | string | yes | The test's name: its folder's. |
| `test_id` | string | yes | Its hidden id, as its `test.json` holds it. |
| <a id="rule-5.3.format-from-test-json"></a><a id="rule-5.3.format-neither-is-code"></a>`format` | string | yes | `text` or `code`, read from `test.json`'s `"active"`. A `test.json` that names neither is `code`. |
| <a id="rule-5.3.test-files-with-functions"></a>`files` | map path → File | no | Its documents: every file under `tests/<test>/`, **plus every file under `functions/`** (a server reads no code to tell which it imports). Absent in a list without documents. |
| <a id="rule-5.3.latest-release"></a>`latest_release` | integer ≥ 1 | no | The highest release under its `test_id`; absent: none. |
| `draft` | Draft | no | Its current files' fingerprints, as the last client that changed them sent them; absent: not known. |
| `missing` | map path → string | *answer* | As a Project's, for its files. |
| `updated` | time | *answer* | The last change to its files. |

**The two test-id rules.** The server keeps these true of every project it holds:

1. A test folder holding any file holds a `test.json` that is a JSON object (a byte-order mark
   before it allowed) whose `test_id` is a lower-case UUID.
2. No two tests of one project hold the same `test_id`.

- <a id="rule-5.3.test-id-rules-p9"></a>**P9** checks each change **in the order sent**, against the project as it is at that point.
  A change after which either rule would fail is refused alone (`refused`, kind `invalid`),
  nothing written for it, and the next change is checked as usual. (So the app sends a new
  test's `test.json` before its other files, and deletes it after them. A folder move carries
  it along.)
- <a id="rule-5.3.test-id-rules-p11"></a>**P11** checks the project whole: a project failing either rule is refused, `400
  bad_request`, nothing written.
- <a id="rule-5.3.other-id-another-test"></a>A `test.json` written with another `test_id` makes its folder another test. What was
  released, run and marked current under the former id stays there.

Whether a test's files are a release, a draft from a release or a never-released draft is worked
out by the app from `draft.content` and the releases' `content`. **The server never computes a
hash of a test.**

**Draft** (nested in Test).

| Field | Type | Req. | Meaning |
|---|---|---|---|
| <a id="rule-5.3.draft-fields"></a>`test_id` | string | yes | Its test's id; a draft naming another id than its test's `test.json` is not kept. |
| `based_on` | integer ≥ 1 | no | The release (under the same `test_id`) it started from; absent: never released. |
| `fingerprint` | string (hex) | yes | What runs, hashed. Never `""`: a test with nothing that runs sends no Draft. |
| `content` | string (hex) | yes | Every document hashed, the test's name taken out. |
| `updated_at` | time | *answer* | When the server stored it. |
| `updated_by` | string | no | Who saved (the PC's user name). |
| `machine_id` | string | no | The bench that sent it. |

In P9 and P11, each draft also carries `test` (the folder `tests/<test>/` it describes).

<a id="rule-5.3.draft-kept-when"></a>**When a draft is kept** (P9, P11 carry one draft per test whose documents the request changes,
as the app computed them). A draft is kept when **all** of these hold:

- its test exists after the request;
- its `test_id` is the one that test's `test.json` then holds;
- every change of the request under `tests/<test>/` and under `functions/` was saved.

Otherwise it is not kept. A test whose files a request changed (written or deleted under its
folder) without a draft kept for it **loses the draft it held**: the test has none until a client
sends one. A change under `functions/` drops no draft (the app sends the drafts of the tests it
reaches). <a id="rule-5.3.draft-moves-no-token"></a>A draft has no result of its own in P9's answer, and moves no change token: P3 does not
report drafts.

```json
{"format_version": 1, "test": "VOUT1_ripple_2A", "test_id": "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17",
 "format": "text", "latest_release": 4, "updated": "2026-11-04T08:13:20Z",
 "draft": {"test_id": "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17", "based_on": 4, "fingerprint": "8c1d…",
           "content": "e07a…", "updated_at": "2026-11-04T08:13:20Z", "updated_by": "jdoe",
           "machine_id": "a41f7c02"},
 "files": {
   "tests/VOUT1_ripple_2A/procedure_text.md": {"version": "sha256:77de…", "size": 3020, "text": "…"},
   "tests/VOUT1_ripple_2A/test.json": {"version": "sha256:faa8…", "size": 70,
     "text": "{\"active\": \"text\", \"test_id\": \"3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17\"}\n"},
   "functions/rails.py": {"version": "sha256:4c19…", "size": 1830, "text": "…"}}}
```

### 5.4 Release

<a id="rule-5.4.release-fields"></a>A released version of a test. Made by P10, **numbered by the server, never changed**. Filed
under its test's `test_id`.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| `test_id` | string | yes | The id of the test released. The releases of one id are one list, numbered from 1: a rename keeps them, a deleted test's stay under its id, a new test (whatever its name) starts at 1. |
| `test` | string | yes | The test's name when it was released. A record, never changed. |
| <a id="rule-5.4.numbering-per-id"></a>`version` | integer ≥ 1 | *answer* | 1, 2, … within its `test_id`; given by the server, never by a client (except an import). |
| `format` | string | yes | `text` or `code`. |
| `fingerprint` | string (hex) | yes | What runs, hashed. A run of this release records it as `definition_sha256`. |
| `content` | string (hex) | yes | The documents hashed, the test's name taken out. |
| <a id="rule-5.4.document-names"></a>`documents` | map name → File | no | The documents by their names in the test: exactly those P10 named. A path under `tests/<test>/` is named without that folder (`procedure.json`, `openhtf/…`); a `functions/` path is kept whole (`functions/rails.py`). Absent in a list. |
| `note` | string | no | What changed, as the person wrote it. |
| `released_at` | time | *answer* | When the server made it. |
| `released_by` | string | no | Who released (the PC's user name). |
| `pc` | string | no | That PC's name. |
| `machine_id` | string | no | The PC it was released from. |
| <a id="rule-5.4.numbered-before-releases"></a>`numbered_before_releases` | boolean | no | `true`: a number a test carried before releases existed, kept so the next one follows it; its documents were not kept. |
| <a id="rule-5.4.release-missing"></a>`missing` | map name → string | *answer* | As a Project's, for its documents, by name. |

A test whose last file is removed (P9) leaves its releases under its `test_id` and loses its
draft. Nothing reserves its name.

```json
{"format_version": 1, "test_id": "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17", "test": "VOUT1_ripple_2A",
 "version": 4, "format": "text", "fingerprint": "6211f7f4…", "content": "d3b9…",
 "note": "Ripple limit widened to 45 mV", "released_at": "2026-11-04T08:13:20Z",
 "released_by": "jdoe", "machine_id": "a41f7c02",
 "documents": {"procedure.json": {"version": "sha256:9f2c…", "size": 4120, "text": "…"},
               "test.py": {"version": "sha256:1b07…", "size": 22310, "text": "…"}}}
```

### 5.5 Run and RunSummary

One execution of one test on one board. **Written once.**

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 2, or 1 for a run recorded without the board identity (below). |
| <a id="rule-5.5.run-project-id"></a>`project_id` | string | no | When sent, it must equal the path's. **Every answer carries it**, so a run found across projects (E2) names its own. |
| `project_name` | string | no | The project's name, used only when this push is the first the server sees of the project. |
| <a id="rule-5.5.run-summary"></a>`run_id` | string | yes | 2.3. |
| `serial` | string | yes | The board's serial. |
| `part_number` | string | no | The board's part number; absent: none. |
| `board_revision` | string | no | The board's revision; absent: none. |
| `test` | string | yes | The test's name when it ran: a record, kept as sent (NFC), never changed. |
| `test_id` | string | yes | The test that ran. The run is filed under it, never under the name. |
| `firmware_version` | string | no | The firmware on the board when it ran (the run's, not the board's). |
| <a id="rule-5.5.lot-format-2-400"></a>`lot` | string | no | **Format 1 only**: the lot such a run recorded, kept as it was. A format-2 run that sends it is refused: `400 bad_request` (the lot is the board's, 5.11). |
| `extra` | object (string values) | no | The project's own fields entered at each run, `{name: value}`. A search by it may be slower. |
| `started` | time | no | When the run started. |
| `ended` | time | when `complete` | When the run was saved. |
| `verdict` | string | no | `PASS`, `FAIL`, `ERROR`, `ABORTED`, `PARTIAL` or `SKIP`; absent for a run that produced no record. |
| `complete` | boolean | yes | `true` when the run produced its record. An incomplete run is kept and listed, and **never becomes current**. |
| `operator` | string | no | The operator chosen at the run's start (a plain name, not proved). |
| `machine_id` | string | yes | The bench that ran it. |
| `station_id` | string | no | That bench's station name (else its hostname). |
| `test_version` | integer ≥ 1 | no | The release that ran (under `test_id`), or the release a draft came from; absent: a draft of a never-released test. |
| `draft` | boolean | yes | `true` when a draft ran; its documents are then among its files (role `definition`). |
| `definition_sha256` | string (hex) | no | The fingerprint of what ran. |
| `bundle_id`, `bundle_version` | string | no | The bundle it ran on. |
| `app_version` | string | no | The app's version. |
| `bench_values` | object | no | The bench values it ran with, `{constant: value}`. |
| <a id="rule-5.5.run-full"></a>`phases` | list of Phase | yes | Every phase of the record, in order; `[]` for an incomplete run. |
| `files` | list of RunFile | yes | Every file of the run, the record included. |
| `pushed_at` | time | *answer* | When the server stored it. |
| <a id="rule-5.5.run-current"></a>`current` | boolean | *answer* | It is the current run of its `test_id` on its board (5.13). |
| <a id="rule-5.5.run-annotation"></a>`annotation` | Annotation | *answer* | What people wrote about it; <a id="rule-5.5.annotation-absent"></a>absent when it has no entry. |
| <a id="rule-5.5.run-missing"></a>`missing` | map name → string | *answer* | Files of an `api` storage the server cannot give: `not_found` or `damaged`. Absent when none. A missing file never hides the run. |
| <a id="rule-5.5.deleted-fields"></a>`deleted_at` | time | *answer* | When a manager deleted it. |
| <a id="rule-5.5.deleted-by-login"></a>`deleted_by` | string | *answer* | The manager's **login**, as the server authenticated it. Never a field a client sends. |
| `deleted_by_name` | string | *answer* | The person's name as typed at the bench (the delete's `deleted_by_name`); absent when none was sent. |
| `deleted_bench` | string | *answer* | The bench the delete came from (its `machine_id`). |
| `deleted_station` | string | *answer* | That bench's station name then (the delete's `station`); absent when none was sent. |
| `deleted_reason` | string | *answer* | Why, as the manager wrote it. |
| `deleted_was_current` | boolean | *answer* | It was its test's current run when deleted. |

<a id="rule-5.5.run-summary-light"></a>**RunSummary** (in lists) is the Run without `phases`, `files`, `missing`, `bench_values` and
`extra`. It keeps `annotation`.

<a id="rule-5.5.format-1-board"></a>**A format-1 run** was recorded without the board identity. It belongs to the "no part number"
board, whatever `part_number` and `board_revision` it recorded: those stay as recorded (shown as
information) and never move it to another board. Whatever names a board names its board, not what
it recorded: E1's answer gives no `part_number` or `board_revision` for it, and E2's
`part_number` and `board_revision` filters find it only with both empty.

```json
{"format_version": 2, "run_id": "2026-10-26T22-20-42Z_7f3a1c9e04b2d8a6", "serial": "SN0042",
 "part_number": "AX-2210", "board_revision": "B", "test": "T1",
 "test_id": "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17", "firmware_version": "2.4.1",
 "extra": {}, "started": "2026-10-26T22:20:42Z", "ended": "2026-10-26T22:20:43Z",
 "verdict": "FAIL", "complete": true, "operator": "AL", "machine_id": "a41f7c02",
 "station_id": "BENCH-3", "test_version": 4, "draft": false,
 "definition_sha256": "6211f7f4…", "bundle_id": "baseline", "bundle_version": "3.0.0",
 "app_version": "5.1.0", "bench_values": {"PSU1_VISA": "TCPIP0::10.0.0.9::INSTR"},
 "phases": [ …Phase… ],
 "files": [
   {"name": "results.json", "role": "record", "size": 18233, "sha256": "5e8f…",
    "mimetype": "application/json", "store_id": 2},
   {"name": "step_4_SCOPE1.png", "role": "attachment", "size": 48211, "sha256": "0e6a…",
    "mimetype": "image/png", "store_id": 2},
   {"name": "console.log", "role": "log", "size": 2310, "sha256": "aa41…",
    "mimetype": "text/plain", "store_id": 2},
   {"name": "functions/rails.py", "role": "functions", "size": 1830, "sha256": "4c19…",
    "mimetype": "text/x-python", "store_id": 1}]}
```

### 5.6 Phase

One phase of the run's record (a step, a loop turn, the record's own first and last phases).

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `ord` | integer | yes | Its place in the record, from 1. |
| `name` | string | yes | As the record names it (`Step 5 (i=2)`). |
| `attempt` | integer | yes | 1 for the phase's first run in this record, 2 for its first retry, and so on. Every attempt is kept, each with its own measurements. |
| `outcome` | string | no | `PASS`, `FAIL`, `SKIP`, `ERROR`. |
| `started`, `ended` | time (ms) | no | From the record. |
| `measurements` | list of Measurement | yes | In the record's order; `[]` for none. |

A repeated phase holds the same names twice: a measurement belongs to its phase by the phase's
`ord`, never its name.

### 5.7 Measurement

A reading or a check. A reading has no criterion; a check judges readings against its criterion.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | string | yes | As the record names it. Unique within its phase. |
| `ord` | integer | yes | Its place among the run's measurements, from 1. |
| `value_num` | number | no | The value when it is a number. A boolean, a list or a text leaves it absent. |
| `value_text` | string | no | The value when it is a text; also the exact digits of an integer a double cannot hold, and `NaN`, `Infinity` or `-Infinity`. |
| `units` | string | no | The unit's suffix (`V`, `s`, `pct`). |
| `outcome` | string | no | `PASS`, `FAIL`, `UNSET`, `PARTIALLY_SET`, `SKIPPED`. |
| `validators` | list of string | no | The criterion as displayed (`27.8 <= x <= 28.2`). |
| `criterion_kind` | string | no | One of `range`, `max`, `min`, `equals`, `tolerance`, `one_of`, `contains`, `matches`, `operator_decision`, `monotonic`, `aggregate`, `and`, `or`, `expr`. |
| `limit_low`, `limit_high` | number | no | The criterion summed up as one pair of limits, in `units`, when one pair does. |
| `criterion` | object | no | The whole criterion as the app's grammar holds it, with its own `kind`. Stored as sent. |

A value is never written twice (`value_num` or `value_text`), except the exact digits of an
integer too large for a double: both (the number for range searches, the text exact).

`criterion_kind` and the limits, from the criterion's own `kind`:

| `criterion.kind` | `criterion_kind` | `limit_low` | `limit_high` |
|---|---|---|---|
| `range`, `range_abs_each` | `range` | lower bound | upper bound |
| `range` with `negated` | `range` | — | — |
| `lt`, `lte`, `lt_each`, `lte_each` | `max` | — | the limit |
| `gt`, `gte`, `gt_each`, `gte_each` | `min` | the limit | — |
| `eq`, `eq_digital`, `eq_text` | `equals` | the target (numbers only) | the target (numbers only) |
| `tolerance_abs`, `tolerance_pct` | `tolerance` | nominal − tolerance | nominal + tolerance |
| `one_of` | `one_of` | — | — |
| `contains`, `contains_any`, `contains_all` | `contains` | — | — |
| `regex_match` | `matches` | — | — |
| `operator_decision` | `operator_decision` | — | — |
| `monotonic` | `monotonic` | — | — |
| `aggregate` | `aggregate` | — | — |
| `logic` with `op: and` | `and` | the intersection, when every part bounds the same reading | idem |
| `logic` with `op: or` | `or` | — | — |
| `expr`, `expr_in`, `diff` | `expr` | — | — |

The limits are already in the measurement's unit. Whether a bound is open is in `criterion`, not
in the limits. A run may carry `validators` and the limits without `criterion_kind` or
`criterion`. The server computes none of these: it stores them as sent.

```json
{"name": "{1} = 28.0 V +/- 0.2 V", "ord": 2, "value_num": 27.96, "units": "V", "outcome": "PASS",
 "validators": ["27.8 <= x <= 28.2"], "criterion_kind": "tolerance", "limit_low": 27.8, "limit_high": 28.2,
 "criterion": {"kind": "tolerance_abs", "ref": 1, "nominal_value": 28.0, "unit": "V",
               "tol_value": 0.2, "tol_unit": "V"}}
```

### 5.8 RunFile

One file of a run: a reference.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| <a id="rule-5.8.runfile-bytes"></a>`name` | string | yes | Its `/` path in the run (`step_4_SCOPE1.png`, `functions/rails.py`). |
| <a id="rule-5.8.runfile-role-mimetype"></a>`role` | string | yes | `record`, `log`, `functions`, `definition` or `attachment` (below). |
| `size` | integer ≥ 0 | yes | Bytes. |
| `sha256` | string (hex) | yes | Of its bytes, bare. |
| <a id="rule-5.8.mimetype-absent"></a>`mimetype` | string | no | The file's type. Absent: not known. The server keeps it absent, never filling in a default: every answer leaves it out. A reader takes such a file as `application/octet-stream`, and so is its part's `Content-Type` (6.2). |
| `store_id` | integer | yes | The storage holding its bytes. Files of one run may sit in different storages. |

| Role | Which files |
|---|---|
| `record` | `results.json`: the run's record. Attachments it once held inside (base64) are sent as files of their own. |
| `log` | `console.log`, `interactive_log.jsonl`. |
| `functions` | `functions/…`: the project's functions the run ran with. |
| `definition` | `definition/…`: the documents a draft ran with. |
| `attachment` | Every other file. |

### 5.9 Annotation and AnnotationEntry

What people wrote about a run: comments and verdicts changed by hand. **Never replaced as a
whole**: each comment or override someone saves is an entry, added to the run's list and never
changed, so one person's save never erases another's.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| `run_id` | string | yes | The run. |
| <a id="rule-5.9.entries-order"></a>`entries` | list of AnnotationEntry | yes | Every entry, in the order received. |
| `data` | object | *answer* | What is current, computed from the entries (below). |

**AnnotationEntry**

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `entry_id` | string | yes | A UUID made by the app; unique within its run. |
| `kind` | string | yes | `comment` or `override`. |
| `key` | string | yes | `test` (the whole run) or a check's stable key. |
| `text` | string or null | yes for a comment | A comment's text; `null` clears it. |
| `value` | string or null | yes for an override | An override's verdict, one of `PASS`, `FAIL`, `ERROR`, `ABORTED`, `PARTIAL`, `SKIP` (another value: `400 bad_request`); `null` clears it. |
| `replaces` | string or null | yes | The `entry_id` this person saw for that `kind` and `key` when editing; `null`: none. |
| `by` | string | yes | Who, as typed or signed in. |
| `machine_id` | string | yes | The bench. |
| `bench` | string | no | The bench's station name. |
| `saved_at` | time | yes | When the person saved, on the bench's clock. Shown, never compared. |
| <a id="rule-5.9.received-at"></a>`received_at` | time | *answer* | When the server took it, on its clock. Absent for an imported entry whose source recorded none. |
| <a id="rule-5.9.current-flag"></a>`current` | boolean | *answer* | `true` for the entry a reader sees for its `kind` and `key` now; `false` for every other. Computed on every read from the order received, never stored as sent. |

**Which entry is current.** Take the entries in the order received. Each `(kind, key)` slot has
at most one current entry:

- an entry for a slot with no entry yet becomes current, whatever its `replaces`;
- an entry whose `replaces` is the slot's current `entry_id` becomes current;
- any other entry is kept, not current (someone else saved that slot meanwhile).

<a id="rule-5.9.data"></a>**`data`** is, for each slot, its current entry, in this shape:

```json
{"version": "2.0",
 "comments":          {"test": V, "criteria": {"<key>": V}},
 "verdict_overrides": {"test": V, "criteria": {"<key>": V}}}
```

`comments` holds `kind: comment`, `verdict_overrides` holds `kind: override`; key `test` goes
under `"test"`, every other key under `"criteria"`. Each `V` is
`{"text" (comment) | "value" (override), "by", "at", "entry_id"}` of the slot's current entry,
`at` being its `saved_at`. A slot whose current entry clears it (`null`) is absent, and so is
`"test"` with no value. With nothing set:
`{"version": "2.0", "comments": {"criteria": {}}, "verdict_overrides": {"criteria": {}}}`.

```json
{"format_version": 1, "run_id": "2026-10-26T22-20-42Z_7f3a1c9e04b2d8a6",
 "entries": [
   {"entry_id": "5b0e…", "kind": "comment", "key": "test", "text": "VREF probe on the wrong pad",
    "replaces": null, "by": "AL", "machine_id": "a41f7c02", "saved_at": "2026-10-27T07:41:10Z"},
   {"entry_id": "c91d…", "kind": "override", "key": "criterion range 9", "value": "PASS",
    "replaces": null, "by": "AL", "machine_id": "a41f7c02", "saved_at": "2026-10-27T07:41:10Z"}]}
```

### 5.10 Artifact

A report, an export or a report draft. `kind` + the board + `name` identify it. A format-1
artifact belongs to the "no part number" board, as a format-1 run does.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 2, or 1 for an artifact recorded without the board identity. |
| `kind` | string | yes | `report` (a board's HTML report), `export` (a project-level export: Word, Markdown, one-file record), `draft` (a report draft being edited). |
| <a id="rule-5.10.serial-by-kind-400"></a>`serial` | string | yes | The board's serial; `""` for an export, always project-level. An export with a serial, or a report or draft without one: `400 bad_request`. |
| `part_number`, `board_revision` | string | no | The rest of the board; absent: none, and always absent for an export. |
| `name` | string | yes | Its file name (`2026-09-20_full-qual.html`). |
| <a id="rule-5.10.artifact-fields"></a>`work_order` | string | no | A report's or draft's work order. |
| `fields` | object (string values) | no | A report's or draft's own fields. A search by it may be slower. |
| `info` | object | no | Its record (a report's JSON record, an export's metadata). Kept verbatim, **key order included** (below). |
| `content` | object | draft only | A draft's content; a draft has no file. Kept as `info` is. |
| `sha256` | string (hex) | yes | A report's or export's file. For a draft: its version (below). |
| `size`, `store_id` | integer | yes, not for a draft | A report's or export's file, a reference as a RunFile's. A draft has neither. |
| `generated` | time | no | When it was made: a report's record (`info`) writes the same time, in UTC. |
| `verdict` | string | no | A report's overall verdict. |
| `operator` | string | no | Who made it. |
| `tests` | list of string | no | The tests a report covers (names as records, 2.4). |
| `machine_id` | string | yes | The bench that sent it. |
| `updated_at` | time | yes | When it was saved on the bench. Shown, never compared. |
| <a id="rule-5.10.generations"></a>`generation` | integer ≥ 1 | *answer* | A report or export: 1, then 2 for the next one sent under the same name, and so on. |
| `replaces` | string or null | draft only | The draft's `sha256` as this bench last read it; `null`: new. |
| <a id="rule-5.10.artifact-missing"></a>`missing` | map name → string | *answer* | `{"<name>": "not_found" \| "damaged"}` when its bytes are in an `api` storage and the server cannot give them; absent otherwise. |
| `deleted_at`, `deleted_by`, `deleted_by_name`, `deleted_bench`, `deleted_station`, `deleted_reason` | | *answer* | As a Run's. An artifact has no `deleted_was_current`. |

**A draft's version.** A draft's `sha256` is the sha256 of its `content` written as canonical
JSON: keys sorted by code point at every level, no blank between tokens, non-ASCII characters
written as themselves, UTF-8 (Python's `json.dumps(content, sort_keys=True, separators=(",",
":"), ensure_ascii=False)`). <a id="rule-5.10.draft-version"></a>**The app computes it and sends it; the server stores and answers
it, and never computes it.**

**`info` and `content` are JSON values, not files.** The server stores and returns each as the
JSON object sent. <a id="rule-5.10.info-key-order"></a>The app rebuilds file bytes from the object read back, so **a server keeps the
key order of `info` and `content`, at every level**. A store that reorders keys (for example,
PostgreSQL's `jsonb`, unlike its `json`) is not a correct store for these two.

An artifact has no `mimetype`. A part carrying its file has a `Content-Type` from its name's
extension (6.6).

```json
{"format_version": 2, "kind": "report", "serial": "SN0042", "part_number": "AX-2210",
 "board_revision": "B", "name": "2026-09-20_full-qual.html", "work_order": "WO-7",
 "sha256": "7a0c…", "size": 41233, "store_id": 2, "generated": "2026-09-20T09:14:41Z", "verdict": "FAIL",
 "operator": "jdoe", "tests": ["dcdc_efficiency"], "machine_id": "a41f7c02",
 "updated_at": "2026-09-20T09:14:52Z",
 "info": {"version": "1.0", "metadata": {"generated": "2026-09-20T09:14:41Z", "scenario": "full-qual",
          "operator": "jdoe", "overall_verdict": "FAIL"}, "tests": [ … ]}}
```

### 5.11 Board and BoardEntry

A board of a project (2.6).

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 2 |
| `serial` | string | yes | As first stored. |
| `part_number` | string | no | As first stored; absent: none. |
| `board_revision` | string | no | As first stored; absent: none. |
| <a id="rule-5.11.lot-details"></a>`lot` | string | *answer* | The lot it was built in: the current `lot` entry's value; absent when none. |
| `details` | object (string values) | *answer* | The project's fields kept with the board, `{name: value}`, from the current `field` entries. A search by it may be slower. |
| `entries` | list of BoardEntry | *answer* | Every entry, oldest first. **Only in E5's answer**, not in lists. |
| <a id="rule-5.11.board-times"></a>`created_at` | time | *answer* | When the server first saw it. |
| `last_ended` | time | *answer* | Its latest run's `ended`, counting only runs not deleted. |

- <a id="rule-5.11.board-created"></a>A board comes into being with its first run (E1), its first entry (E15) or its first report or
  draft (E10), whichever comes first. Its first spelling is that item's. On a server registering
  its boards (3.3) a bench's write creates no board: the board is registered first (by its
  administrator, or by an import, which creates it).
- <a id="rule-5.11.board-never-removed"></a>**A board is never removed.** One whose runs are all deleted is still listed (E4). On a
  server registering its boards, a board its administrator unregisters is no longer listed (E4
  lists exactly the registered boards, 3.3).
- The lot and the details are **entries, never replaced**, exactly as an annotation's (5.9).
  Each correction is an entry, so the history keeps the old value, who changed it and when.

**BoardEntry**

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `entry_id` | string | yes | A UUID made by the app; unique within its board. |
| `kind` | string | yes | `lot`, or `field` for one of the project's fields. |
| <a id="rule-5.11.field-name-exact"></a>`name` | string | for `field` | The field's name, **compared exactly** (not by case folding). A `lot` entry has none. |
| `value` | string or null | yes | The value; `null` clears it. |
| `replaces` | string or null | yes | The `entry_id` this person saw for that `kind` and `name`; `null`: none. |
| `by` | string | yes | Who. |
| `machine_id` | string | yes | The bench. |
| `bench` | string | no | The bench's station name. |
| `saved_at` | time | yes | The bench's clock. Shown, never compared. |
| `received_at` | time | *answer* | As an AnnotationEntry's. |
| `current` | boolean | *answer* | As an AnnotationEntry's, for its slot. |

An entry's slot is its `kind` and, for `field`, its `name`. Which entry is current follows the
rule of 5.9. `lot` is the current `lot` entry's value; `details` holds each `field` slot's current
value; a slot whose current entry is `null` is absent.

```json
{"format_version": 2, "serial": "SN0042", "part_number": "AX-2210", "board_revision": "B",
 "lot": "L2611-04", "details": {"Customer": "ACME"},
 "created_at": "2026-10-26T22:21:05Z", "last_ended": "2026-10-26T22:20:43Z",
 "entries": [
   {"entry_id": "e2a7…", "kind": "lot", "value": "L2611-04", "replaces": null, "by": "AL",
    "machine_id": "a41f7c02", "saved_at": "2026-10-26T22:19:58Z", "current": true},
   {"entry_id": "9c41…", "kind": "field", "name": "Customer", "value": "ACME", "replaces": null,
    "by": "AL", "machine_id": "a41f7c02", "saved_at": "2026-10-26T22:19:58Z", "current": true}]}
```

### 5.12 BoardResults

A board's results in one answer (E5).

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 2 |
| `board` | Board | yes | The board, with its `entries`. |
| <a id="rule-5.12.tests-by-test-id"></a><a id="rule-5.12.runs-newest-first"></a>`tests` | list of `{test_id, pointer, runs}` | yes | One item per `test_id` that has at least one run on this board not deleted, **ordered by `test_id`** (byte order of the lower-case UUID). `pointer`: its TestPointer, or `null`. `runs`: its runs not deleted, as RunSummary, newest first by `run_id`, each with the `test` name it recorded. |
| <a id="rule-5.12.include-current"></a>`current_runs` | list of Run | no | With `include=current`: each test id's current run in full (with the parts of its `api`-stored files). |

<a id="rule-5.12.deleted-left-out"></a>A deleted run is left out. <a id="rule-5.12.all-deleted-not-listed"></a>A `test_id` whose runs are all deleted is not listed.

### 5.13 TestPointer and the pointer rule

Which run is the current one of a test on a board. <a id="rule-5.13.one-mark-per-test-id"></a>A board has **one mark per `test_id`**, never
per name.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `test_id` | string | yes | The test. |
| `run_id` | string or null | no | The current run; `null`: demoted (no current run), `ended` kept. |
| `ended` | time | no | That run's `ended`; kept when demoted. |
| `machine_id` | string | no | The bench that moved it last. |
| `updated_at` | time | *answer* | When it moved. |

**The pointer rule** (applied on every run push; an import's only on a server not offering E6,
6.7):

- <a id="rule-5.13.newer-complete-current"></a>a **complete** run becomes current when its `test_id` has no pointer on that board, or when its
  `ended` is **strictly later** than the pointer's `ended`;
- <a id="rule-5.13.incomplete-never-current"></a><a id="rule-5.13.tie-or-older-leaves"></a>a tie, an older run and an incomplete run leave the pointer as it is (an import on a server
  not offering E6 alone breaks a tie by the greater `run_id`, 6.7);
- <a id="rule-5.13.demoted-keeps-ended"></a>a demoted pointer keeps its `ended`, so only a newer run revives it;
- <a id="rule-5.13.set-by-hand-ended"></a>setting a run current by hand (E6) gives the pointer that run's `ended`, so a run pushed later
  that ended after it takes over, as after any push.

<a id="rule-5.13.delete-hands-on"></a><a id="rule-5.13.handoff-tiebreak"></a><a id="rule-5.13.handoff-none"></a>**Deleting the current run** hands the pointer to the newest remaining <a id="rule-5.13.handoff-complete-only"></a>complete run (never an incomplete one) not deleted,
of the same `test_id` on the same board: the latest `ended`, then the greatest `run_id`. When none
remains, the test has **no pointer** on that board any more, as before its first run, and the next
complete run pushed becomes current.

### 5.14 Bench

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| `machine_id` | string | yes | Its identity. |
| <a id="rule-5.14.label"></a>`label` | string | *answer* | Its station name (else its hostname): the one the latest request naming it gave, a run's `station_id` (E1) or an entry's `bench` (E7, E15). A request naming none leaves it as it is. Absent until a request names one. Never `""`; no length limit of its own. |
| <a id="rule-5.14.bench"></a>`first_seen`, `last_seen` | time | *answer* | The first and last request that named it (by `machine_id` in the body). The server's own bookkeeping, imports included; never imported. |

### 5.15 Storage

Where files' bytes are. Listed by S1.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| `store_id` | integer | yes | Its number; every file reference names one. |
| `backend` | string | yes | `api`, `filesystem`, `ftps` or `sftp`. |
| <a id="rule-5.15.storage-fields"></a>`params` | object (string values) | yes | The backend's parameters, **never a secret**: `filesystem` `{root}`; `ftps` `{host, port, base_path, user, tls, passive, insecure, ca_bundle}`; `sftp` `{host, port, base_path, user, auth}`; `api` `{}`. |
| `identity` | string | yes | The storage as the app names it: `ftps://host:port/base_path`, `sftp://host:port/base_path`, or the folder's path. (`api` for the server itself.) |
| `active` | boolean | yes | New files go here, unless a project names its own (`Project.store_id`). One storage is active at a time. Always `false` for a declared storage. |
| `declared` | boolean | yes | `true`: a project's own storage, declared by the app (S2); the server never cleans it. `false`: one of the server's. |
| `reachable` | boolean | yes | The server itself reads this storage, so it checks references into it. `false`: it assumes they hold. |

Older files stay readable where they are, in a storage no longer active.

```json
{"instance_id": "6f1d2b8e-0c47-4e3a-9a55-2b7e1c9d0f31",
 "storages": [
   {"format_version": 1, "store_id": 1, "backend": "api", "params": {}, "identity": "api",
    "active": false, "declared": false, "reachable": true},
   {"format_version": 1, "store_id": 2, "backend": "sftp", "active": true, "declared": false,
    "reachable": true, "identity": "sftp://files.lab.local:22/srv/procedo",
    "params": {"host": "files.lab.local", "port": "22", "base_path": "/srv/procedo",
               "user": "bench", "auth": "key"}}]}
```

### 5.16 LibraryItem

One version of one item of the company's library: something a PC can install.

| `kind` | What it is | Its manifest (where the app reads its id and version) |
|---|---|---|
| `bundle` | A bundle: rules, defaults, the Python wheels a project runs on. | `bundle.json`: `id`, `version` |
| `config` | A project configuration template. | `config.json`: `manifest.name`, `manifest.version` |
| `report` | A report template package (HTML). | `schema.json`: `package_id`, `package_version` |
| `export` | An export template package (Word, Markdown). | `schema.json`: `package_id`, `package_version` |
| `capability` | An equipment pack: grammar, schema, code generation and its driver library. | `pyproject.toml`: `project.name`, `project.version` |
| `wizard` | An authoring wizard (a Python dialog). | `skill.md` front matter |
| `skill` | A chat skill. | `skill.md` front matter |
| `tool` | Python an AI assistant calls. | `tools.json` |
| `package` | A package holding several of the above. | `package.json` |

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `format_version` | integer | yes | 1 |
| <a id="rule-5.16.item-roundtrip"></a>`kind` | string | yes | One of the kinds above. |
| `id` | string | yes | The item's id. |
| `version` | string | yes | Its version. `<id>@<version>` within its `kind` is its identity, **compared exactly**. |
| `content_sha256` | string (hex) | yes | The digest of the item's files, its manifest left out: every file but the manifest, in the byte order of its `/` path, each adding its path, a NUL, its own sha256 and a NUL. Equal on both sides: the same item. (A bundle's manifest calls it `bundle_sha256`.) The server stores it, never computes it. |
| `content_bytes` | integer ≥ 0 | no | The size of the files that digest counts. |
| `manifest` | object | no | The item's manifest, **verbatim, never read**: the server does not check that its id and version are the item's. Absent in L1's list. |
| <a id="rule-5.16.contains-code-400"></a><a id="rule-5.16.code-needs-publisher"></a>`contains_code` | boolean | yes | `true` for an item holding Python a PC would run. The publisher's word: the server opens no archive. It refuses only `false` for the four kinds that always hold code (`capability`, `wizard`, `tool`, `package`): `400 bad_request`. Only a `publisher` login publishes an item with `true`. The installing PC looks in the archive itself and treats one holding Python as code whatever the flag says. |
| `archive` | File | yes | The version's folder as one zip; a part or a storage reference like any file. Over 1 MiB it is sent alone first (B1, owner `library`). |
| `missing` | map | *answer* | `{"archive": "not_found" \| "damaged"}` when its archive is in an `api` storage and the server cannot give it; absent otherwise. |
| `published_at` | time | *answer* | When the server took it. |
| `published_by` | string | *answer* | The login the server authenticated; never a field a client sends. |
| `machine_id` | string | no | The PC it was published from. |

- <a id="rule-5.16.created-once"></a>**A version is created once.** Publishing an `<id>@<version>` the server holds is never an
  overwrite: with the same `content_sha256` it is a no-op (`already`); with another it is
  `409 version_exists` (`details.content_sha256` the stored one).
- <a id="rule-5.16.case-twin-409"></a>**A case-only twin is refused.** An `<id>@<version>` that differs from a held one of the same
  kind only by case is `409 exists` (a PC keeps an item in a folder `<id>/<version>/`, and a
  Windows disk cannot hold both).

```json
{"format_version": 1, "kind": "bundle", "id": "baseline", "version": "3.0.0",
 "content_sha256": "c0de…", "content_bytes": 1702400, "contains_code": true,
 "published_at": "2026-11-03T10:00:00Z", "published_by": "jdoe", "machine_id": "a41f7c02",
 "manifest": {"id": "baseline", "version": "3.0.0", "bundle_sha256": "c0de…", "packs": [ … ]},
 "archive": {"version": "sha256:3b7a…", "size": 1782000, "mtime": "2026-11-03T10:00:00Z", "store_id": 1}}
```

---

## 6. The calls

All paths are under `<base>api/v1`, with `Authorization: Bearer <token>`. `{project}` is a
`project_id`.

### 6.1 Every call at a glance

**Core** calls every server takes; **optional** ones only a server listing them in `/info`'s
`calls` (below).

| # | Call | Who | Offer | Section |
|---|---|---|---|---|
| — | `GET /info` | any | core | 3.3 |
| P1 | `GET /projects` | any | core | 6.3 |
| P2 | `GET /projects/{project}` | any | core | 6.3 |
| P2b | `POST /projects/{project}/read` (without `since`) | any | core | 6.3 |
| P3 | `GET /projects/{project}?since=…` · `POST /projects/{project}/read` with `since` | any | optional | 6.3 |
| P4, P5 | `GET /projects/{project}/tests` (P5 with `names`) | any | optional | 6.3 |
| P6 | `POST /projects/{project}/tests/read` | any | optional | 6.3 |
| P7 | `GET /projects/{project}/tests/{test}` · `POST …/tests/{test}/read` | any | core | 6.3 |
| P8 | `GET /projects/{project}/releases/{test_id}` | any | optional | 6.3 |
| P8b | `GET /projects/{project}/releases/{test_id}/{version}` · `POST …/{version}/read` | any | optional | 6.3 |
| P9 | `POST /projects/{project}/changes` | bench | core | 6.3 |
| P10 | `POST /projects/{project}/releases/{test_id}` | bench; importer with `import` | core | 6.3 |
| P11 | `POST /projects` | bench | optional | 6.3 |
| E1 | `POST /projects/{project}/runs` | bench; importer with `import` | core | 6.4 |
| E2 | `GET /runs` | any | optional | 6.4 |
| E3 | `GET /projects/{project}/runs/{run_id}` · `POST …/runs/{run_id}/read` | any | core | 6.4 |
| E4 | `GET /projects/{project}/boards` | any | core | 6.4 |
| E5 | `GET /projects/{project}/boards/{serial}` · `POST …/boards/{serial}/read` | any | core | 6.4 |
| E6 | `POST /projects/{project}/boards/{serial}/tests/{test_id}/current` | bench; importer with `import` | optional | 6.4 |
| E7 | `POST /projects/{project}/runs/{run_id}/annotation` | bench; importer with `import` | core | 6.4 |
| E8 | `GET /projects/{project}/artifacts` | any | core | 6.4 |
| E9 | `GET /projects/{project}/artifacts/{kind}/{name}` | any | core | 6.4 |
| E10 | `POST /projects/{project}/artifacts` | bench; importer with `import` | core | 6.4 |
| E11 | `POST /projects/{project}/runs/{run_id}/delete` · `…/restore` | manager; importer with `import` (delete only) | optional | 6.4 |
| E12 | `POST /projects/{project}/artifacts/delete` · `…/restore` | manager; importer with `import` (delete only) | optional | 6.4 |
| E13 | `POST /projects/{project}/purge` | manager | optional | 6.4 |
| E14 | `GET /benches` | any | optional | 6.4 |
| E15 | `POST /projects/{project}/boards/{serial}/entries` | bench; importer with `import` | optional | 6.4 |
| E16 | `GET /projects/{project}/operators` | any | optional | 6.4 |
| E17 | `GET /projects/{project}/readings?test=<name>` | any | optional | 6.4 |
| L1 | `GET /library` | any | optional | 6.5 |
| L2 | `GET /library/{kind}/{id}/{version}` · `POST …/{version}/read` | any | optional | 6.5 |
| L3 | `POST /library/{kind}` | bench (no code); publisher (any) | optional | 6.5 |
| S1 | `GET /storages` | any | core | 6.6 |
| S2 | `POST /storages` | bench | optional | 7.3 |
| S3 | `GET /storages/{store_id}/to_delete` | bench | optional | 7.3 |
| S4 | `POST /storages/{store_id}/deleted` | bench | optional | 7.3 |
| B1 | `POST /storages/{store_id}/blobs/{owner}/{sha256}` | bench, importer (a project's); bench, publisher (`library`) | core | 6.6 |
| B2 | `GET /storages/{store_id}/blobs/{owner}/{sha256}` | any | core | 6.6 |
| U1 | `GET /users` | any | optional | 6.8 |
| import | `"import": true` in the body of E1, E6, E7, E10, E15, P10 and the deletes of E11 and E12 | importer | optional | 6.7 |

"importer with `import`" and "importer (a project's)" hold only on a server listing `import`:
one that does not grants no login the role `importer` (3.5).

**Core and optional calls.** <a id="rule-6.1.core-calls"></a>Every server takes the **core**
calls, marked so in the table and listed in [0](#0-before-you-start-what-you-must-may-and-choose). With them alone the app opens, syncs and pushes a project, releases a test, pushes
runs with their files, reads a board's runs back, comments a run and changes its verdict by hand
(E7), and makes, pushes, lists and opens reports, exports and drafts (E8, E9, E10). Every other
call is **optional**: a server offers those it chooses, by their ids above, and lists them in `/info`'s `calls` (3.3). <a id="rule-6.1.unlisted-501"></a>An optional
call the server does not list answers `501 unsupported`, kind `invalid` (4.3), as a call it does
not have: never `404` (proof that an item is absent) and never served. A call it lists follows
every rule of its section. The app sends an optional call only to a server listing it.

**Imports are optional too**, by the id `import`: only a server listing it in `calls` takes data
moved in as it was (6.7). A server that does not list it grants no login the role `importer`
(3.5) and <a id="rule-6.1.import-unlisted-501"></a>answers an `"import": true` on **every** call
that takes one (E1, E6, E7, E10, E15, P10, the deletes of E11 and E12) `501 unsupported`, kind
`invalid`, as an optional call it does not list: nothing written, whichever login sends it. The
app never sends one there.

What each group of optional calls gives the app; the app offers a feature only when the server
lists every call of its group:

| Optional calls | What the app does with them | Without them |
|---|---|---|
| P11 | **Creating projects** (owner, 2026-11-10: optional): a project made on a PC published to the server (New Project on it, Move or Copy Project onto it). <a id="rule-6.1.create-unlisted-501"></a>A server not listing P11 answers it `501 unsupported`, kind `invalid`, nothing created (P1 does not list it, P2 answers `404`). | No project is created on this server through the app: it greys publishing a project there, saying why. Its projects come into being by the server's own means; a run pushed to a project it does not hold still makes an evidence-only project (6.1). |
| P3 | Syncs a project by what changed since its last read. | Each sync pass reads the whole project (P2); every token is `""` (P2). |
| P4, P5, P6 | Lists a project's tests; reads several tests in one request. | Reads a test by its name (P7), or the whole project. |
| P8, P8b | Lists a test's releases and reads one release alone. | Reads them with the project (P2 holds every release). |
| E2, P4, E14, E16, E17 | **Search**: runs (E2) across boards and dates by any filter, beside the reports E8 (a core call) lists, and the tests, benches, operators and readings the filters propose (P4, E14, E16, E17; the boards by E4). | No search; a board's results still open (E4, E5), the reports still list (E8). |
| E6 | **Set current**: a person picks which run of a test counts on a board. | The pointer rule alone (5.13): the newest complete run counts. |
| E15 | **Board entries**: a board's lot and its own fields. | None. |
| E2, E11, E12, E13 | **Delete and trash**: a manager deletes, restores and purges runs and artifacts (E11–E13); the trash lists them (E2, and E8 — a core call — with `deleted=only`). <a id="rule-6.1.manager-calls-need-a-manager"></a>A server lists E11, E12 and E13 only when some login can hold the role `manager` (3.5): where nobody may delete, it lists none of them, and the app greys Delete, saying why. | Nothing is ever deleted through the app. |
| L1, L2, L3 | **The library**: bundles, configurations, templates and equipment packs shared through the server. | No library on this server. |
| S2, S3, S4 | **A project's own storage** (7.3): files kept in a storage the server does not use. | A project's files go to the server's own storages only. |
| U1 | **The server's users**: a bench picks the operator from them; the server checks each run's `operator` and each release's `released_by` (6.8). | Any name, as typed. |
| import | **Moving data in** (6.7): a project's evidence and its releases moved onto the server as they were (each run's dates, the report and release numbers, what was deleted). | Nothing is moved onto this server as it was: the app greys Move evidence and Copy evidence onto it, saying why, and refuses, before anything is written, to move or publish a project holding releases there (they would be lost). |

**Rules shared by many calls:**

- <a id="rule-6.1.item-not-held-404"></a>**An item the server does not hold.** A call that names one item the server does not hold
  (E3, E9, P7, P8b, L2) answers `404 not_found`.
- <a id="rule-6.1.unseen-project-empty"></a>**A project the server never saw** is not an error for the evidence reads: it answers as an
  empty project. E2 finds no runs, E4 no boards, E5 the board with no runs and no entries, E8 no
  artifacts. E3 and E9 answer `404 not_found`.
- <a id="rule-6.1.evidence-only-project"></a>**A project known only by its evidence.** A run push (E1) can create a project the server
  holds no files for (its files live elsewhere). Such a project is not a project of the project
  calls: P1 does not list it, and P2–P10 answer `404 not_found` (an empty project would read as
  every file deleted). P9 answers that 404 before reading the body. P11 gives it its files, its
  evidence kept.
- **A replay** (2.1 rule 8) is answered as the first time.

### 6.2 How file bytes travel

This applies to every call that sends or answers files.

**"+ parts"** means: the bytes of the files kept in an `api` storage, as parts of the same body,
less those named in `have`. A file in another storage is a reference the app reads there itself
(section 7).

#### Multipart bodies (`api` storage)

```
POST /api/v1/projects/0b8f3c52-4d1e-4a8b-9f27-6c3e5d1a7b90/runs
Content-Type: multipart/form-data; boundary=b1

--b1
Content-Disposition: form-data; name="json"
Content-Type: application/json

{"format_version": 2, "run_id": "2026-10-26T22-20-42Z_7f3a1c9e04b2d8a6", …}
--b1
Content-Disposition: form-data; name="sha256:0e6aaa07…"; filename="step_4_SCOPE1.png"
Content-Type: image/png

<the PNG's bytes>
--b1--
```

| Rule | |
|---|---|
| <a id="rule-6.2.request-content-type"></a>Request type | A request body is `application/json` or `multipart/form-data`. Any other `Content-Type`: `400 bad_request`. (B1 alone takes `application/octet-stream`.) A multipart body is sent plain, never with a `Content-Encoding` (2.12). |
| <a id="rule-6.2.multipart-form"></a>First part | Named `json`, `Content-Type: application/json`: the object, as plain JSON. It carries no `Content-Encoding` (2.12). |
| <a id="rule-6.2.part-boundary"></a>File parts | Named `sha256:<hex>` of their bytes. Only for files whose `store_id` is an `api` storage, and only for a file of **at most 1 MiB** (1,048,576 bytes; a file of exactly that size rides as a part). `filename` is a hint for logs. `Content-Type` is the file's mimetype: a RunFile's `mimetype`; for an artifact, from its name's extension (`.html` `text/html`, `.json` `application/json`, `.md` `text/markdown`, `.docx` `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, any other `application/octet-stream`). A receiver never relies on a part's `Content-Type`. |
| <a id="rule-6.2.two-parts-one-name-400"></a>One part per content | Two files with the same bytes travel as one part. Two parts with one name: `400 bad_request`. |
| <a id="rule-6.2.digest-mismatch"></a><a id="rule-6.2.part-size-400"></a>Checked | The receiver hashes each part. Bytes not matching the name: `422 digest_mismatch` (kind `corrupt`). A part whose size is not the `size` its file states: `400 bad_request`. |
| <a id="rule-6.2.unreferenced-part-ignored"></a>Unreferenced parts | A part no `api`-stored file of the JSON names is ignored: not stored, not an error. |
| <a id="rule-6.2.part-over-1mib-400"></a>Too large | A request over `limits.max_upload_bytes`: `413 too_large`, `details.max_upload_bytes`, before the body is read. A part over 1 MiB, whatever the request's size: `400 bad_request`, `details.large` its versions, nothing written. |
| <a id="rule-6.2.answer-multipart"></a><a id="rule-6.2.answer-no-part-json"></a>Answers | `multipart/mixed`, the same parts with `Content-Disposition: attachment; name="…"; filename="…"`. An answer with no part to send is plain `application/json`. The client sends `Accept: multipart/mixed, application/json`, but the server chooses the form from what it sends alone: it never reads `Accept` and never answers `406`. A server never gzips a whole multipart answer. |
| <a id="rule-6.2.large-in-answer"></a><a id="rule-6.2.small-without-part-missing"></a>Large files in answers | An answer lists a file over 1 MiB without its part; the client reads it with B2. A file of at most 1 MiB that is not in `have` and has no part is missing (the object's `missing` says why). |

#### `have`: what the reader holds already

<a id="rule-6.2.have"></a>Every read that can answer parts takes `have`: the versions (`sha256:<hex>`) the client holds.
The server leaves those parts out; the JSON still lists the files.

- In a `GET`, `have` is a query parameter, versions separated by commas.
- <a id="rule-6.2.read-twins"></a>When the list is too long for a URL (more than about 6 KB), the client sends the same read as
  `POST <path>/read` with `{"have": [...]}` in the body. These **`/read` twins** are reads: they
  change nothing, and their answer is identical. Parameters that can be long go in the body too
  (P2b's `since`, P6's `names`, E5's `include`); every other parameter stays in the query exactly
  as the GET has it (E5's `part_number` and `board_revision` included). The twins are: P2b (for P2
  and P3), P6 (for P5), P7, P8b, E3, E5 and L2. E9 has none.
- A broken JSON answer is asked for again, with the parts that arrived whole in `have`. There is
  no `Range` on JSON.
- <a id="rule-6.2.text-once"></a>**A text document comes once**, in the answer that reads it. P3 brings each text changed since
  its token; a later P3 does not bring it again. No call reads one text document alone (a client
  that did not keep one reads the project whole with P2).

#### Writing: the bytes first, then the JSON

1. For each file the JSON will reference, the app picks the active storage (the project's
   `store_id`, else the server's active one, from S1) and makes sure the bytes are there:
   - another backend: the app uploads to it itself (7.2);
   - <a id="rule-6.2.held-blob-reference"></a>`api`: a part of the request itself, left out when the app knows the server holds it
     (sent before to this project and accepted, or read from the server); a file over 1 MiB is
     sent alone first with B1, never as a part.
2. One `POST` of the JSON, referencing each file by its hash and `store_id`.
3. <a id="rule-6.2.missing-files-409"></a>When a referenced file is not where the JSON says, the server writes nothing and answers
   `409 missing_files` with `details.missing` (the versions). The app puts them there and sends
   the same request again.

- <a id="rule-6.2.blobs-per-project"></a><a id="rule-6.2.damaged-blob"></a>**Blobs are per project**, on an `api` storage as on every storage. A reference to bytes only
  another project holds is a missing file. A blob the server holds but finds damaged is a
  missing file too; the part sent again replaces it.
- <a id="rule-6.2.unreachable-assumed"></a>A server checks the storages it reaches. A reference into one it does not reach
  (`reachable: false`) it assumes holds.
- <a id="rule-6.2.unknown-store-id-400"></a>An unknown `store_id` in a reference: `400 bad_request`.

### 6.3 Projects, tests and releases

#### P1 · `GET /projects`

<a id="rule-P1.light-with-path"></a>Answer: `{"projects": [Project]}`, each **without** `files`, `bundle` and `releases`, with
`path`. <a id="rule-P1.list"></a><a id="rule-P1.order"></a>Every project whose files this server keeps (not one known only by its evidence),
ordered by name (case folded).

#### P2 · `GET /projects/{project}?have=…`

<a id="rule-P2.whole"></a>Answer: the whole Project (files, bundle, every release with its documents, `missing`, `token`)
+ parts. One request opens a project.

<a id="rule-P2.token-empty-without-P3"></a>**The token and P3.** A server that does not list P3
answers `token: ""` here and wherever a token is answered (P1, P2b, P4, P5, P6, P9, P11). The app
then reads the whole project with P2 on every sync pass and never sends `since`. A server listing
P3 answers a non-empty token everywhere.

#### P3 · `GET /projects/{project}?since=<token>&have=…`

Optional (6.1): P2, or P2b, with `since`. A server not listing it answers `501 unsupported`.
Answer: what changed since the token, + parts:

```json
{"token": "…", "files": {"<path>": File}, "deleted": ["<path>"], "bundle": Bundle,
 "releases": {"<test_id>": [Release]}, "missing": {…}}
```

- <a id="rule-P3.changes"></a><a id="rule-P3.releases-changed"></a><a id="rule-P3.rename-no-release"></a>`files`: each file changed since the token, as it is now. `deleted`: each path deleted.
  `bundle`: present when it changed. `releases`: each `test_id` whose releases changed (one made
  or imported), with its whole list now, each with documents; `{}` when none did. A rename
  changes no release.
- <a id="rule-P3.token-moves"></a>**The token** is opaque. It moves with every request that changes the project's files, bundle
  or releases, **never with a draft alone**.
- <a id="rule-P3.gone"></a>Any token the server cannot answer from (older than what it keeps, another server's, one it
  never gave, one it cannot read): `410 gone`. The app then opens the project in full (P2 with
  `have`).
- <a id="rule-P3.empty-since-400"></a>An empty `since`: `400 bad_request`.
- A client reads on from the token of its **last read** (P2, P3), never from the token a write
  (P9) answers. The write's own changes come back in its next P3, at versions it already holds.

#### P2b · `POST /projects/{project}/read`

Body `{"have": [...], "since": "…"}` (`since` optional). Answer: as P2, or P3 when `since` is
given; `410 gone` as P3. With `since`, it is P3's: `501 unsupported` from a server not listing P3.

#### P4 · `GET /projects/{project}/tests`

<a id="rule-P4.tests"></a>Answer: `{"token": "…", "tests": [Test]}`, each **without** `files`.

#### P5 · `GET /projects/{project}/tests?names=A,B&have=…` and P6 · `POST /projects/{project}/tests/read`

P5's `names` is comma-separated. P6 sends `{"names": [...], "have": [...]}` in the body (a name
holding a comma is asked with P6).

Answer: `{"token": "…", "tests": {"<name as asked>": Test}, "missing": ["<name>"]}` + parts.
<a id="rule-P5.keyed-as-asked"></a>`tests` is keyed by each name **as asked** (the Test's `test` gives the stored spelling).
<a id="rule-P5.missing"></a>`missing` lists the names asked that are no test, as asked.

#### P7 · `GET /projects/{project}/tests/{test}?have=…` · `POST …/tests/{test}/read`

<a id="rule-P7.one-test"></a>Answer: one Test with its files + parts. A test this project does not hold: `404 not_found`.
The twin's body is `{"have": [...]}`.

#### P8 · `GET /projects/{project}/releases/{test_id}`

<a id="rule-P8.list"></a>Answer: `{"releases": [Release]}` **without** documents, oldest first: every release filed under
that id, whether or not a test of the project holds it now. An id with none: `{"releases": []}`
(not 404). A malformed `test_id`: `400 bad_request` (2.5).

#### P8b · `GET /projects/{project}/releases/{test_id}/{version}?have=…` · `POST …/{version}/read`

<a id="rule-P8b.one-release"></a>Answer: one Release with its documents + parts. A version the id does not hold: `404 not_found`.

#### P9 · `POST /projects/{project}/changes`

Writes, deletes, folder moves, drafts and a new bundle, in one request. `multipart/form-data`
when it carries parts, plain JSON otherwise. Role: `bench`.

```
json:
{"changes": [
  {"path": "tests/T1/procedure.json", "replaces": "sha256:9f2c…", "text": "…"},
  {"path": "tests/T5/test.json",      "replaces": null,
   "text": "{\"active\": \"text\", \"test_id\": \"8d02e6b1-…\"}\n"},
  {"path": "tests/T5/procedure_text.md", "replaces": null, "text": "…"},
  {"path": "documents/manual.pdf",    "replaces": "sha256:a913…", "version": "sha256:c2f0…",
   "size": 1902455, "store_id": 1},
  {"path": "tests/T2/test.py",        "replaces": "sha256:77de…", "delete": true},
  {"path": "tests/T3", "replaces_folder": {"test.py": "sha256:…", "test.json": "sha256:…"},
   "move_to": "tests/T4"},
  {"bundle": {"bundle_id": "baseline", "bundle_version": "3.1.0", "bundle_sha256": "…"},
   "replaces": "sha256:3b7a…", "version": "sha256:e19d…", "size": 1790112, "store_id": 1}],
 "drafts": [{"test": "T1", "test_id": "3f9c2a1e-…", "based_on": 4, "fingerprint": "…", "content": "…"},
            {"test": "T5", "test_id": "8d02e6b1-…", "fingerprint": "…", "content": "…"}]}
sha256:c2f0…   the PDF's bytes        (storage 1 is the API server here)
sha256:e19d…   the bundle's zip
```

<a id="rule-P9.shapes"></a>The five shapes of a change:

| Change | Fields | Meaning |
|---|---|---|
| write text | `path`, `replaces`, `text` | Write a text document. `replaces`: the version replaced, `null` for a new file. |
| write a stored file | `path`, `replaces`, `version`, `size`, `store_id` | Write a binary file by reference. |
| delete | `path`, `replaces`, `delete: true` | Delete the file at that version. |
| move a folder | `path`, `replaces_folder`, `move_to` | Move a folder. `replaces_folder` maps each file in it (relative name) to its version. |
| a new bundle | `bundle` (`bundle_id`, `bundle_version`, `bundle_sha256`?), `replaces`, `version`, `size`, `store_id` | Replace the bundle's archive. |

`drafts`: one per test whose documents this request changes, with its `test` and `test_id`, as
the app computed them (5.3). The server never computes them.

**Answer** `200`:

```json
{"token": "…", "results": [{"path": "…", "status": "saved" | "changed" | "refused",
  "version": "sha256:…", "current": File, "kind": "…", "message": "…"}]}
```

<a id="rule-P9.results"></a>One result per change, in order; the bundle's `path` is `bundle/`. `token` is the project's after
this request. A client never reads on from it: its next P3 starts from the token of its last
read (see P3).

| Status | Meaning |
|---|---|
| `saved` | Done; `version` is the new one. |
| <a id="rule-P9.changed"></a>`changed` | The file changed on the server since `replaces`. Nothing written for this path. `current` is what the server holds now (absent when it holds none), so the app can show the conflict without another request. The other changes still apply. |
| `refused` | The server's own rule (quota, a protected path, a permission), a path that never travels (5.2), or a test-id rule (5.3). With `kind` and `message`. |

Rules:

- **Each change is all-or-nothing for its path.** The request as a whole is not a transaction.
- <a id="rule-P9.replay"></a>**A replay** of a change the server already satisfies is `saved` and writes nothing, whatever
  its `replaces` says: a write of the version held, a delete of a file that is gone, a move
  already made. <a id="rule-P9.replay-no-token"></a>Writing nothing, it moves no token: a request sent again whose every change
  is already satisfied answers the token held before it, and P3 from that token brings nothing.
- <a id="rule-P9.malformed-400"></a>**A malformed change refuses the whole request** (`400 bad_request`): a path a PC cannot hold
  (2.10), a file in the wrong form (2.9), a part over 1 MiB.
- <a id="rule-P9.missing-files-409"></a>**A change whose bytes are not where it says** (no part, not in the storage): `409
  missing_files` for the whole request, nothing written.
- **The test-id rules** (5.3) are checked change by change, in order; a change breaking them is
  `refused` (kind `invalid`).
- <a id="rule-P9.move-conflict"></a><a id="rule-P9.move-refused"></a>**A move** names the versions of every file it moves. When the folder holds other files, or
  the destination holds any file: `changed`, nothing moved, with no `current` (a folder is no
  File; the next P3 brings what changed). A move into itself, or one that would change a file's
  form (2.9) or make it a file that never travels: `refused` (kind `invalid`).
- <a id="rule-P9.rename-carries"></a>**A test renamed** is a move of its folder (`tests/<old>/` → `tests/<new>/`). It carries its
  draft and its `test.json`, so the test keeps its `test_id`, and its releases, runs and
  current-run marks stay where they are. A rename moves no evidence. Reports keep the test names
  they were made under. Any name no test holds now may be taken, a deleted test's included.
- <a id="rule-P9.last-file-deletes-test"></a>**A test whose last file a request removes is deleted**: its draft goes; its releases, runs
  and marks stay under its `test_id`.
- Drafts are kept or dropped as 5.3 says. A draft has no result of its own and moves no token.
- A project known only by its evidence: `404 not_found`, before the body is read.

#### P10 · `POST /projects/{project}/releases/{test_id}`

A release of the test whose `test.json` holds that id. Role: `bench` (importer: 6.7).

```json
POST /api/v1/projects/{project}/releases/3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17
{"note": "Ripple limit widened to 45 mV", "format": "text", "fingerprint": "…", "content": "…",
 "released_by": "jdoe", "pc": "LAB-PC-3", "machine_id": "a41f7c02",
 "files": {"tests/VOUT1_ripple_2A/test.py": "sha256:…", "functions/rails.py": "sha256:…"}}
```

| Body field | Req. | Meaning |
|---|---|---|
| `format`, `fingerprint`, `content` | yes | As the Release's. |
| <a id="rule-P10.only-named-files"></a>`files` | yes (not an import) | Project path → version of each file released, under `tests/<test>/` or `functions/`: exactly the documents the release keeps. Only those are checked and kept. |
| `note`, `released_by`, `pc`, `machine_id` | no | As the Release's. |
| <a id="rule-P10.body-test-id-ignored"></a>`test_id` | — | Ignored if sent: the path's id is the one released. |

The server:

1. <a id="rule-P10.not-found"></a>Finds the test whose `test.json` holds the path's `test_id`. None: `404 not_found`.
   A server listing U1 then checks `released_by`: none of its users, `409 unknown_operator`
   (6.8).
2. <a id="rule-P10.changed"></a>Checks every file version named is still what it holds. Else `409 changed`, with
   `details.current` the test's documents now and `details.changed` the paths whose version is
   not the server's.
3. <a id="rule-P10.same-as-release"></a>Refuses a `content` equal to a release's under the same `test_id`: `409 same_as_release`,
   `details.version` that release.
4. <a id="rule-P10.two-files-one-name-400"></a>Keeps each named file under its name in the test: a path under `tests/<test>/` without that
   folder, a `functions/` path whole. Two files taking one name: `400 bad_request`.
5. <a id="rule-P10.number-and-name"></a>Gives the next number under that id (one more than the highest; 1 for a test never released,
   whatever its name), stores the release with the test's **current** name as its `test`.

<a id="rule-P10.answer-light"></a>Answer `200`: the new Release without its documents (`{"version": 5, "test_id", "test",
"released_at", "released_by", …}`). A release is never changed afterwards.

#### P11 · `POST /projects`

Optional (6.1): a server not listing it answers `501 unsupported`, nothing created.

<a id="rule-P11.create"></a>A new project. Role: `bench`. Body: a Project (`format_version`, `project_id`, `name`, `files`,
optional `bundle`, optional `drafts`) + parts. `config/config.json` among the files holds the
`project_id`.

- `name` follows the folder rule of 5.2, else `400 bad_request`.
- A path that never travels (5.2) or that a PC cannot hold (2.10): `400 bad_request`.
- A test folder whose `test.json` holds no lower-case UUID as `test_id`, or two tests holding one
  `test_id`: `400 bad_request`, nothing written.
- <a id="rule-P11.exists"></a><a id="rule-P11.replay"></a>A project with that `project_id` or that name (case folded) held already: `409 exists`, except
  **the same request again** (the same name, files and bundle): a replay, answered with the
  Project.
- A project known only by its evidence takes its files here, its evidence kept.
- <a id="rule-P11.drafts"></a>Drafts as P9's (5.3).

Answer `200`: the whole Project (files, bundle, `releases: {}`), as plain JSON (the client sent
the parts).

### 6.4 Evidence

#### E1 · `POST /projects/{project}/runs` — push a run

Role: `bench` (importer: 6.7). First the run's files go to the active storage (6.2). Then:

```
POST /api/v1/projects/{project}/runs        application/json, or multipart/form-data
  json             the Run, its phases and measurements inline, its files referenced
  sha256:<hex> …   only for an `api` storage: one part per file the server may not hold
```

The server does this **in one step that either all happens or none of it**:

1. Checks every RunFile names a storage it lists (unknown `store_id`: `400 bad_request`) and that
   the bytes are there: a part of this request or a blob this project already holds whole, for an
   `api` storage; for another storage, when the server can see it. Any missing: `409
   missing_files`, `details.missing`, nothing written.
2. <a id="rule-E1.creates-project-board"></a><a id="rule-E1.project-name"></a>Creates the project when it does not know `project_id` (a project whose files live elsewhere
   keeps only its evidence here; `project_name` names it), and the board when it is new (a
   format-1 run's board is the "no part number" one).
3. <a id="rule-E1.push"></a>Stores the run. When the `run_id` exists already in this project, deleted or not:
   - <a id="rule-E1.replay"></a>the same JSON (the same sha256 of its bytes, every encoding undone) is a replay: `already`,
     and a deleted run stays deleted;
   - <a id="rule-E1.run-conflict"></a>a different one: `409 run_conflict`, kind `corrupt`.
   <a id="rule-E1.purged-409"></a>A `run_id` a manager purged: `409 deleted` (a replay never brings a purged run back).
4. Files the run under its `test_id` and applies the pointer rule (5.13) to that test id's mark on
   the board.

Other refusals, `400 bad_request`: a format-2 run with `lot`; a board name `""` or with blanks
around it; a value of `extra` that is not a string; an unknown `format_version`. <a id="rule-E1.project-id-match"></a>A `project_id`
in the body must equal the path's. A server listing U1: an `operator` none of its users, `409
unknown_operator`, nothing written (6.8). A server registering its boards (`boards_registered`,
3.3): a new run on a board that is not registered (E4 does not list it), `409 unknown_board`,
nothing written; never on a board E4 lists.

Answer `200`:

```json
{"status": "saved", "run_id": "2026-10-26T22-20-42Z_7f3a1c9e04b2d8a6", "serial": "SN0042",
 "part_number": "AX-2210", "board_revision": "B",
 "test_id": "3f9c2a1e-7b4d-4e8a-9c61-2d5b8e0f4a17", "current": true,
 "pushed_at": "2026-10-26T22:21:05Z"}
```

<a id="rule-E1.answer"></a>`status` is `saved` or `already`. `serial`, `part_number` and `board_revision` name the board the
run was filed under, in its stored spelling (a format-1 run's has no part number or revision).
`test_id` is the test it was filed under. `current`: it is now the current run of its `test_id` on
the board. The app's outbox keeps the JSON bytes as first built, so a replay sends exactly the
same bytes.

#### E2 · `GET /runs` — search runs

Paged. Across projects when `project_id` is absent. <a id="rule-E2.filters"></a>Every text filter compares by case folding
(NFC, then `casefold()`), except `field` (exact) and `verdict` (exact: one of six fixed words).

| Parameter | Filter |
|---|---|
| <a id="rule-E2.across-projects"></a>`project_id` | One project; absent: every project this login may read. |
| `serial`, `operator`, `machine_id`, `firmware_version` | That value. |
| `test_id` | That test's runs, whatever names it had. Not a lower-case UUID: `400 bad_request` (not `unsupported_filter`). |
| `test` | Runs that **recorded** that name (a Run's `test`), whichever test that was and whatever it is called now. A plain match on the record: the server follows no rename. |
| `part_number`, `board_revision` | The run's **board** has that value; empty: none (the "no part number" board, where every format-1 run is). |
| `lot` | Runs on a board whose lot is that value, and format-1 runs that recorded it. |
| `field`, `field_value` | Runs with that value for one of the project's own fields: the run's (`extra`) or its board's (`details`). The field's name is compared exactly; the value by case folding. May be slower to search by. |
| `verdict` | One or more, comma-separated. |
| `test_version`, `definition_sha256`, `draft` | The version that ran. |
| `from`, `to` | `ended` within `[from, to)`. A run with no `ended` never matches either. |
| <a id="rule-E2.bound-without-measurement-400"></a>`measurement`, `value_min`, `value_max` | Runs with a measurement of that name, in any phase and attempt. With a bound, its `value_num` within the bounds, both inclusive (no `value_num`: no match); with no bound, any value. A bound without `measurement`: `400 bad_request`. |
| <a id="rule-E2.deleted-only"></a>`deleted` | `exclude` (default), `include`, `only`. |
| <a id="rule-E2.limit-cursor"></a>`limit` | At most this many rows; absent: 100. The server may lower a larger one to its own ceiling, never below 500. |
| `cursor` | The previous answer's `cursor`. |

Any other parameter: `400 unsupported_filter`.

Answer: `{"runs": [RunSummary], "cursor": "…" | null}`. <a id="rule-E2.order"></a>Ordered **newest first by `run_id`,
descending** (its start: an incomplete run sits where its start puts it), then by `project_id`
ascending. `cursor: null` means the answer is complete.

#### E3 · `GET /projects/{project}/runs/{run_id}?have=…` · `POST …/runs/{run_id}/read`

<a id="rule-E3.run"></a>Answer: the Run with everything (phases, measurements, files, `annotation`, `current`,
`missing`, `deleted_*` when deleted) + parts. <a id="rule-E3.deleted-answers"></a>A deleted run still answers. A run the project does
not hold: `404 not_found`.

#### E4 · `GET /projects/{project}/boards?part_number=&board_revision=`

<a id="rule-E4.list"></a>Answer: `{"boards": [Board]}` **without** `entries`. <a id="rule-E4.every-board"></a>Every board of the project is
listed, one no run was pushed to yet included (a report's or an entry's board); on a server
registering its boards (3.3), exactly the registered boards, no more, no less. <a id="rule-E4.filters"></a>Each filter is optional: absent means any,
empty means none. <a id="rule-E4.order"></a>Ordered by part number, then revision, then serial, each ascending by case
folding, "none" first. A board whose runs are all deleted is still listed. On a server registering
its boards (3.3), every board registered is listed, before its first run too, and no other.

#### E5 · `GET /projects/{project}/boards/{serial}?part_number=…&board_revision=…&include=current&have=…` · `POST …/boards/{serial}/read`

<a id="rule-E5.board"></a>Answer: the BoardResults (5.12) + parts. `include=current` adds each test id's current Run in
full. Not paged. A board the server does not know (in a project it may never have seen) is not an
error: the board with no runs and no entries. The twin takes the board in the query and `have`
and `include` in the body.

#### Addressing one board (E5, E6, E9, E15)

The call carries the serial (in the path, or in E9's query) and, in the query, `part_number` and
`board_revision`. <a id="rule-E5.addressing-400"></a>**All three are required**, an empty value meaning "none". A call that forgot
one is refused (`400 bad_request`) instead of reaching the "no part number" board. A project-level
export (E9) sends all three empty.

#### E6 · `POST /projects/{project}/boards/{serial}/tests/{test_id}/current?part_number=…&board_revision=…`

<a id="rule-E6.set-clear"></a>Set or clear the current run of that test on that board. Role: `bench` (importer: 6.7).

```json
{"run_id": "2026-10-25T14-02-11Z_1c0d9e7752a4f031",
 "replaces": {"run_id": "2026-10-26T22-20-42Z_7f3a1c9e04b2d8a6", "ended": "2026-10-26T22:20:43Z"},
 "machine_id": "a41f7c02"}
```

- `run_id`: the run to make current; `null` clears it (demote), keeping `ended`.
- `replaces`: the pointer as the client last read it (`{run_id, ended}`), or `null` for none.
- <a id="rule-E6.not-its-run-404"></a>The run must be one of this project's, on that board, of that `test_id`, and not deleted: else
  `404 not_found`. <a id="rule-E6.incomplete-400"></a>It must be complete: else `400 bad_request`.

Answer: `{"status": "saved" | "changed", "pointer": TestPointer}`.

- `saved`: written (when `replaces` matches the pointer); `pointer` is the new one. The pointer
  takes the chosen run's `ended`.
- <a id="rule-E6.changed"></a>`changed`: the pointer moved meanwhile; nothing written; `pointer` is the current one.
- <a id="rule-E6.replay"></a>**A replay**: a request the pointer already satisfies (it names that run; or it names none and
  the request clears) answers `saved` and writes nothing, whatever `replaces` says.
- <a id="rule-E6.no-pointer-yet"></a>**No pointer yet**: `replaces: null` matches it, and the run is set. Any other `replaces`
  on a set answers `changed` with pointer `{"test_id": "<test_id>", "run_id": null}`.
  <a id="rule-E6.clear-no-pointer"></a>A **clear** with no pointer is already satisfied, as a replay is: `saved` with pointer
  `{"test_id": "<test_id>", "run_id": null}`, nothing written, whatever `replaces` says.

#### E7 · `POST /projects/{project}/runs/{run_id}/annotation`

Body `{"entries": [AnnotationEntry]}`: one entry per comment or override changed. Role: `bench`
(importer: 6.7).

<a id="rule-E7.statuses"></a>For each entry, in the order sent, the server adds it to the run's list and answers:

| Status | When |
|---|---|
| `saved` | Its `replaces` is the slot's current entry, or the slot has no entry yet (whatever its `replaces`). It is now current. |
| `changed` | Someone else saved that slot meanwhile. The entry is **still kept** in the history (`current: false`); the answer carries the slot's current entry, so the app can offer to apply it. |
| `already` | The run holds that `entry_id` already (a replay), whatever the rest of the entry says. The entry held stays as it is. |

Answer: `{"results": [{"entry_id", "status", "current": AnnotationEntry}]}`, one per entry, in
order (`current` with `changed`).

<a id="rule-E7.deleted-run-409"></a>A run a manager deleted takes no entry: `409 deleted`. Its entries come back with it on a restore.
<a id="rule-E7.override-value-400"></a>An override value outside the six verdicts: `400 bad_request`. No clock is compared.

#### E8 · `GET /projects/{project}/artifacts`

Paged, as E2. Parameters:

| Parameter | Filter |
|---|---|
| <a id="rule-E8.filters"></a>`kind` | `report`, `export` or `draft`. |
| `serial` | One board's serial; empty selects project-level exports. |
| `part_number`, `board_revision` | That value; empty: none. |
| `work_order` | Reports and drafts with this work order (case folded). |
| `test` | Reports covering this test name (case folded), as the report recorded it. |
| <a id="rule-E8.verdict"></a>`verdict` | One or more, comma-separated: the artifacts whose `verdict` is one of them, compared exactly (one of six fixed words, as E2's). |
| <a id="rule-E8.from-to"></a><a id="rule-E8.from-to-bounds"></a>`from`, `to` | `generated` within `[from, to)`. An artifact with no `generated` never matches either. |
| `deleted` | `exclude` (default), `include`, `only`. |
| <a id="rule-E8.generations"></a>`generations` | `latest` (default: each name's newest generation) or `all` (every generation of each name, each a row of its own, placed by its own `generated`). |
| <a id="rule-E8.include-info"></a>`include` | `info` only: keeps each row's `info`. A draft's `content` is never listed. |
| <a id="rule-E8.limit-cursor"></a>`limit`, `cursor` | As E2's. |

Any other parameter: `400 unsupported_filter`.

<a id="rule-E8.light"></a>Answer: `{"artifacts": [Artifact without info and content], "cursor": "…" | null}`.
<a id="rule-E8.order"></a>Ordered **newest first by `generated`, descending**; an artifact with no `generated` comes
after every one that has it. <a id="rule-E8.order-ties"></a>Equal times (and no time) are ordered by kind, then serial,
part number, revision and name, each ascending by case folding ("none" first), then by
`generation`, newest first. `cursor: null` means the answer is complete.

#### E9 · `GET /projects/{project}/artifacts/{kind}/{name}?serial=…&part_number=…&board_revision=…&generation=&have=…`

<a id="rule-E9.generation"></a>Answer: one Artifact + its part: the newest generation, or generation `n` with `generation=n`. A
<a id="rule-E9.draft-generation-404"></a>generation the name does not hold, or `generation` on a draft (which has none): `404 not_found`.
The board parameters are required (above).

#### E10 · `POST /projects/{project}/artifacts`

Push a report, an export or a draft. Role: `bench` (importer: 6.7). The Artifact as `json`; its
file first put in the active storage, or a part of the same request for an `api` storage.

| Case | Answer |
|---|---|
| <a id="rule-E10.report-already"></a>A report or export equal to its name's newest generation (the same `sha256` **and** the same `info`) | `already`; the generation held is answered. |
| <a id="rule-E10.report-new-generation"></a><a id="rule-E10.info-only-new-generation"></a>A report or export, any other push under that name (other bytes, or a changed `info` with the same file) | `saved` as the next `generation`. **A report or export is never replaced.** |
| <a id="rule-E10.draft"></a>A draft whose `replaces` is the `sha256` of the draft held | `saved`; replaced. |
| A draft equal to the one held (same `sha256`) | `already`. |
| A draft with no draft held | `saved`, whatever its `replaces`. |
| A draft whose `replaces` is not the held draft's `sha256` | `changed`, with the current draft; nothing written. |
| <a id="rule-E10.deleted-name"></a>A name a manager deleted | `409 deleted` (a bench cannot undo a delete by publishing again), except a push equal to that name's newest generation: a replay, `already`, and the artifact stays deleted. |
| A report or a draft (not an import, not a replay) on a board that is not registered, on a server registering its boards (3.3) | `409 unknown_board`, kind `invalid`; nothing written. |

Answer: `{"status": "saved" | "already" | "changed", "artifact": Artifact}`. The first report or
draft of a board creates the board. An export with a serial, or a report or draft without one:
`400 bad_request`.

#### E11 · `POST /projects/{project}/runs/{run_id}/delete` · `…/restore`

Role: `manager` (an importer may send a delete with `import`, 6.7); any other login: `403
not_a_manager`.

Delete body:

```json
{"machine_id": "a41f7c02", "station": "BENCH-3", "deleted_by_name": "alice",
 "reason": "Wrong board tested"}
```

| Field | Req. | Meaning |
|---|---|---|
| `machine_id` | yes | The bench the manager acts from; kept as `deleted_bench`. |
| <a id="rule-E11.reason-400"></a>`reason` | yes | Why. Absent or blank: `400 bad_request`. Kept as `deleted_reason`. |
| <a id="rule-E11.never-empty-strings"></a>`station` | no | The bench's station name; kept as `deleted_station`. Never `""`: a `""` is `400 bad_request` (2.2), nothing deleted. |
| `deleted_by_name` | no | The person's name as typed at the bench; kept as `deleted_by_name`. Never `""`: a `""` is `400 bad_request`, nothing deleted. Nothing proves it. |

Restore body: `{"machine_id": "…"}` only.

- <a id="rule-E11.delete"></a>**Delete** hides the run: lists leave it out (unless `deleted=include|only`); reads of it still
  answer, with `deleted_*` set. `deleted_by` is the login the server authenticated. Deleting the
  current run hands the pointer on (5.13).
- <a id="rule-E11.restore"></a>**Restore** puts it back as it was. A run that was current when deleted
  (`deleted_was_current`) becomes current again by the pointer rule: when the test has no pointer
  on that board, or the pointer's `ended` is earlier than the run's. <a id="rule-E11.restore-stays-history"></a>Otherwise it stays in the
  history, and E6 can still set it.
- <a id="rule-E11.replays"></a>**A replay**: deleting a deleted run, or restoring a live one, answers `200` with the run as it
  is, changes nothing (the first `reason` stays) and carries no `pointer`.

Answer: `{"run": RunSummary, "pointer": TestPointer | null}`. <a id="rule-E11.answer-pointer"></a>`pointer` after a delete of the
current run: the pointer it was handed to, or `null` when no complete run remains. After a
restore that made the run current: the pointer. Absent when nothing moved.

#### E12 · `POST /projects/{project}/artifacts/delete` · `…/restore`

As E11, for an artifact. The body names the artifact: `{machine_id, kind, serial, part_number?,
board_revision?, name}`, plus `reason`, `station`, `deleted_by_name` for a delete. The delete
<a id="rule-E12.every-generation"></a>covers **every generation** of the name; a restore brings them all back. <a id="rule-E12.replays"></a>Replays as E11.
<a id="rule-E12.acts-on-name"></a>Both act on the name, never on one generation: the body names none (a `generation` sent is
ignored), and the answer is the Artifact of the name's **newest** generation, with its
`deleted_*` after a delete.

#### E13 · `POST /projects/{project}/purge`

Role: `manager` only (never an importer). Body: `{"machine_id": "…", "items": [...]}` (`items`
optional): each item `{"run_id"}` or `{"kind", "serial", "part_number"?, "board_revision"?,
"name"}`.

- <a id="rule-E13.not-expired-kept"></a><a id="rule-E13.expired-purged"></a>Removes for good what was deleted **strictly longer ago** than the retention period
  (`limits.retention_days`), by the server's own clock. A purge names no other moment. The
  server may also purge on its own schedule.
- <a id="rule-E13.items"></a>With `items`, it also purges those deleted items now. An item it no longer holds is skipped (a
  retried request). An item that is not deleted refuses the whole request: `400 bad_request`,
  nothing purged.
- <a id="rule-E13.run-id-kept"></a>The server keeps each purged run's `run_id`, so a later push of it is `409 deleted`.
- <a id="rule-E13.pointer-of-purged"></a>A purge of a run a pointer still names (possible only after an imported delete on a server
  offering E6) hands the
  pointer on, as a delete does.

Answer:

```json
{"runs": 1, "artifacts": 1,
 "purged": {"runs": ["2026-10-26T22-20-42Z_7f3a1c9e04b2d8a6"],
            "artifacts": [{"kind": "report", "serial": "SN0042", "part_number": "AX-2210",
                           "board_revision": "B", "name": "2026-09-20_full-qual.html"}]},
 "to_delete": {"2": ["sha256:…"]}}
```

- <a id="rule-E13.counts-to-delete"></a>`runs`, `artifacts`: how many went, one per run and one per artifact name (all its generations
  count once).
- <a id="rule-E13.purged-list"></a>`purged`: what went, what expired included. Each artifact by its kind, its board's names as
  stored (absent parts left out; a format-1 artifact's board has none) and its name; an export
  with `serial: ""`. Nothing went: both lists empty.
- <a id="rule-E13.to-delete-scope"></a>`to_delete`: in the project's own declared storages, this project's blobs nothing references
  now, keyed by `store_id` written as a decimal string (7.3): for each storage exactly what S3
  lists for this project at that moment, so the blobs a replaced or deleted file left too, not
  only those this purge freed, and none S4 settled already.
  <a id="rule-E13.to-delete-absent"></a>Absent when there is none; a storage with none is not a key (never `{}`, never an empty list).

#### E14 · `GET /benches`

<a id="rule-E14.benches"></a>Answer: `{"benches": [Bench]}`.

#### E15 · `POST /projects/{project}/boards/{serial}/entries?part_number=…&board_revision=…`

Save a board's lot or fields. Role: `bench` (importer: 6.7). Body `{"entries": [BoardEntry]}`.
<a id="rule-E15.statuses"></a>Works **exactly as E7**: each entry is added to the board's list and answered `saved`, `changed`
(kept, not current, with the current entry) or `already` (a replay). An entry's slot is its
`kind` and, for a `field`, its `name` compared exactly. `entry_id` is unique within its board. The
<a id="rule-E15.creates-board"></a>board is created when it is new, so a lot can be entered before the board's first run. On a
server registering its boards (3.3), a bench's entry (not an import) on a board that is not
registered is `409 unknown_board`, kind `invalid`, nothing written. Answer:
`{"results": [{"entry_id", "status", "current": BoardEntry}]}`.

#### E16 · `GET /projects/{project}/operators`

The names Search Results proposes for its Operator filter. <a id="rule-E16.operators"></a>Answer: `{"operators": [string]}`:
every operator recorded on the project's runs (a Run's `operator`), each once. Names that differ
only by case folding (2.4) are one, answered in the spelling first received. A run with no
`operator` adds none; a project the server never saw has none (`[]`).
<a id="rule-E16.deleted-included"></a>A deleted run's operator is listed, as E4 lists a board whose runs are all deleted.
<a id="rule-E16.order"></a>Ordered ascending by case folding. No parameter: any is `400 bad_request` (2.11).

#### E17 · `GET /projects/{project}/readings?test=<name>`

The names Search Results proposes for its Measurement filter once a test is picked.
<a id="rule-E17.readings"></a>Answer: `{"readings": [{"name": string, "units": string}]}`: every measurement name (5.7)
recorded on the runs that recorded the test name `test`, in any phase and attempt: the names E2's
`measurement` filter finds that test's runs by. The runs are those E2's `test` filter finds (a
Run's `test`, compared by case folding, following no rename); another test's measurements are not
listed. A measurement's names that differ only by case folding (2.4) are one reading, answered in
the spelling first received. A test with no measurement, and a project the server never saw, have
none (`[]`). <a id="rule-E17.units"></a>`units`: the `units` of the first measurement received under that name that
carries one; absent when none does.
<a id="rule-E17.deleted-included"></a>A deleted run's readings are listed, as E16 lists its operator.
<a id="rule-E17.order"></a>Ordered ascending by the name's case folding.
<a id="rule-E17.test-required"></a>`test` is required, once, and never empty: missing or empty, `400 bad_request`, a fault of the
request's form (2.13, step 5). Any other parameter: `400 bad_request` (2.11).

```json
{"readings": [{"name": "{1}", "units": "V"}, {"name": "{2}"}, {"name": "Ripple", "units": "mV"}]}
```

### 6.5 The library

#### L1 · `GET /library?kind=…`

<a id="rule-L1.list"></a><a id="rule-L1.light"></a>Answer: `{"items": [LibraryItem without manifest]}`: every kind (or the one asked), every id with
every version, in one answer. <a id="rule-L1.order"></a>Ordered by kind, then id (by code point), then publication, oldest
first (the order the server took them). <a id="rule-L1.kind-order"></a>The kinds go by their names' code points too, not by 5.16's table:
`bundle`, `capability`, `config`, `export`, `package`, `report`, `skill`, `tool`, `wizard`. <a id="rule-L1.kind-400"></a>A `kind` that is none of 5.16's: `400 bad_request`.

#### L2 · `GET /library/{kind}/{id}/{version}?have=…` · `POST …/{version}/read`

<a id="rule-L2.item"></a>Answer: one LibraryItem with its manifest + its archive's part. Not held: `404 not_found`.

#### L3 · `POST /library/{kind}`

<a id="rule-L3.publish"></a>Publish one version. The LibraryItem as `json`; its archive put in the active storage first, or a
part of the same `multipart/form-data` request (over 1 MiB: B1 first, owner `library`).

- Role: `bench` for an item with `contains_code: false`; `publisher` for any item. Otherwise
  `403 forbidden`.
- `contains_code: false` for `capability`, `wizard`, `tool` or `package`: `400 bad_request`.
- Same `<id>@<version>` and same `content_sha256`: `already`. Other content: `409
  version_exists`. A case-only twin: `409 exists`.

Answer: `{"status": "saved" | "already", "item": LibraryItem}`.

### 6.6 Storages list and large files

#### S1 · `GET /storages`

<a id="rule-S1.storages"></a>Answer: `{"instance_id": "<this server>", "storages": [Storage]}`. The app needs nothing else to
find any file: a reference names its `store_id`. A push needs S1 (to know the active storage); the
app reads it once per server and keeps it. `instance_id` names this server (7.2).

A server keeping every byte itself lists one storage, backend `api`.

#### Which way a file travels

<a id="rule-E1.large-file"></a>A file of **at most 1,048,576 bytes** (1 MiB) rides as a part of the JSON request or answer
(6.2). A larger one travels **alone**, one file per request: uploaded with B1 before the JSON
that references it, downloaded with B2 after the JSON that lists it. The size stated in the JSON
says which: a client never waits for the part of a file over 1 MiB. A file of at most 1 MiB may
also be sent alone first, when its parts would take a request over `max_upload_bytes`; the JSON
then finds it held. Text documents never use these calls: they always travel inside the JSON.

So "one request per action" means one JSON request, plus one per large file that actually has to
move. The first open of a project with a board archive and a bundle is 1 + 2 requests; every open
after is 1.

#### B1 · `POST /storages/{store_id}/blobs/{owner}/{sha256}` — upload one file

- <a id="rule-B1.upload"></a><a id="rule-B1.content-type-400"></a>The file's bytes are the whole body, `Content-Type: application/octet-stream` (another type:
  `400 bad_request`).
- `{owner}` is the `project_id` the file belongs to, or `library` for a library archive. A blob
  is its owner's: the JSON that follows names a blob its project holds.
- <a id="rule-B1.owner-before-project"></a>The owner need not exist yet: a large file goes **before** the JSON, so B1 for a `project_id`
  the server holds nothing for (a new project's file before its P11, a first run's before its
  E1) is `saved` like any other. The bytes are kept for that `project_id`; the JSON that follows
  finds them held, and B2 reads them under that owner.
- <a id="rule-B1.bare-sha-400"></a>`{sha256}` is the bare 64 hex digits (a `sha256:` prefix: `400 bad_request`).
- <a id="rule-B1.api-storage-only"></a>An `api` storage only; any other storage: `400 bad_request` (the app reaches it directly); an
  unknown `store_id`: `404 not_found`.
- <a id="rule-B1.roles-403"></a>Role: `bench` or `importer` for a project's file (an upload carries no body to say `import`:
  the write that references it does); `bench` or `publisher` for `library`. Any other: `403
  forbidden`.
- <a id="rule-B1.max-file-413"></a>**Held to `limits.max_file_bytes`**, never to `max_upload_bytes`: over it, `413 too_large`,
  `details.max_file_bytes`, before the body is read.
- <a id="rule-B1.digest-422"></a>A body whose hash differs from the path's: `422 digest_mismatch`, kind `corrupt`.
- <a id="rule-B1.kept-24h"></a>**An upload nothing references yet** is kept at least 24 hours, so the JSON that follows can
  reference it. Then the server may reclaim it as any unreferenced blob. A write that finds it
  gone answers `409 missing_files`, and the app uploads it again.

<a id="rule-B1.already"></a>Answer: `{"status": "saved" | "already", "version": "sha256:<hex>", "size": <bytes>}`. `already`
when the owner holds those bytes whole already. A damaged copy is replaced.

#### B2 · `GET /storages/{store_id}/blobs/{owner}/{sha256}` — download one file

- <a id="rule-B2.download"></a><a id="rule-B2.range"></a>The bytes as `application/octet-stream`, with `Accept-Ranges: bytes`.
- One `Range: bytes=<first>-[<last>]` (or `bytes=-<n>`, the last `n` bytes) answers `206` with
  `Content-Range`, so a broken download resumes where it stopped.
- A range past the end: `416 range_not_satisfiable` (kind `corrupt`), with `Content-Range: bytes
  */<size>`.
- Several ranges, or a `Range` the server does not read: the whole file, `200`.
- <a id="rule-B2.owner-404"></a>Bytes the owner does not hold (another project's included): `404 not_found`.
- <a id="rule-B2.any-size"></a>B2 answers **any** file the owner holds, one of at most 1 MiB too (a client that holds no part
  of it reads it alone).
- Storage and `{sha256}` rules as B1. Open to every login.

### 6.7 Moving data in: the import

Moving a project's evidence from another storage into this server must give back exactly what
went in. Some fields are normally set by the server, never by a client:

- a run's `pushed_at`, and an entry's `received_at`;
- a report's or export's `generation`;
- a release's `version` and `released_at`;
- the `deleted_*` fields (a run's `deleted_was_current` included);
- a pointer's `ended`, `machine_id` and `updated_at`.

An import carries them as they were.

**Imports are optional** (6.1): only a server listing `import` in `/info`'s `calls` takes one. A
server that does not list it never grants the `importer` role and answers every `"import": true`
`501 unsupported`, nothing written (6.1); the app moves nothing onto it as it was.

- <a id="rule-import.role-and-flag"></a>**A login with the `importer` role** may send those fields on E1, E6, E7, E10, E15, P10 and the
  deletes of E11 and E12, with `"import": true` in the body. The server stores them as sent,
  instead of stamping its own.
- <a id="rule-import.ignored-without"></a>**Without `import`, or without the role**, those fields are ignored.
- <a id="rule-import.contradiction"></a>**The server still checks everything else** exactly as for a bench: hashes, missing files, the
  board's names, replays; but never whether the board is registered (an import is taken on any
  board, `409 unknown_board` is never an import's, 3.3). It refuses an imported item that contradicts what it holds: the same id with
  other content (`409 run_conflict` for a run), or a number already taken (`409 changed`,
  `details.current` what holds it).

Per call:

| Call | With `"import": true` |
|---|---|
| <a id="rule-import.E1-no-pointer"></a>E1 | The run keeps the `pushed_at` sent. On a server offering E6, **no pointer moves**: the importer sets each one (E6). <a id="rule-import.E1-rule-without-E6"></a>On a server **not** offering E6 (its `/info` `calls` does not list it), the imported run moves the pointer **as a bench's run does** (the pointer rule, 5.13): the newest complete run becomes current, the latest `ended`, a tie going to the greater `run_id`, whatever order the runs come in. |
| <a id="rule-import.E6-as-sent"></a>E6 | The pointer is set exactly as sent: `run_id` (or `null`), `ended`, `machine_id`, `updated_at`; `replaces` not checked. The run named must exist, on that board and test, be complete and not deleted (else `404` / `400` as for a bench). Without `ended`, the pointer takes its run's `ended`; without `updated_at`, the server's time. |
| <a id="rule-import.E7-E15-received-at"></a>E7, E15 | Each entry keeps its `received_at`, or stays without one when none is sent. An entry's `current` is never imported: it is computed. |
| <a id="rule-import.E10-generation"></a>E10 | A report or export keeps the `generation` sent. One the name already holds with another file or `info`: `409 changed`, `details.current` the Artifact of that generation. |
| <a id="rule-import.E11-E12-delete"></a>E11, E12 (delete) | The delete keeps `deleted_at`, `deleted_by`, `deleted_bench` and (a run's) `deleted_was_current` as sent. On a server offering E6 it **hands nothing on**: <a id="rule-import.delete-keeps-pointer"></a>a pointer naming the run deleted keeps naming it, until the importer sets that pointer (E6) or a purge hands it on: E5 answers it as it is, and the run's `current` stays `true`. <a id="rule-import.delete-hands-on-without-E6"></a>On a server **not** offering E6, an imported delete of a run hands the pointer on **as a bench's delete does** (5.13): to the newest remaining complete run not deleted (the latest `ended`, then the greatest `run_id`), or no pointer when none remains; a pointer there never names a deleted run. |
| <a id="rule-import.P10"></a>P10 | The release is filed under the path's `test_id` **whether or not a test holds it now** (a deleted test's releases move too). It keeps `version`, `released_at` and `test` (the name it was released under). Its documents travel whole as `documents` (the Release's own map: text, or references whose bytes are parts of the same `multipart/form-data` request, sent alone with B1, or held already) in place of `files`. Under an id no test holds, `test` and `documents` are required: without either, `400 bad_request`. Its number taken by the same release (same `content` and `fingerprint`): answered again. By another: `409 changed`. |

- **Pointers are the source's where the server offers E6.** An imported run moves no pointer;
  the importer then sets each pointer with E6 as the source had it, a demoted one or none
  included. A server not offering E6 picks the current runs itself, by the pointer rule (an
  imported delete handing the pointer on as a bench's does): a pointer the source set by hand
  (an older run, or none) is not kept there, and the app says so before it moves.
- <a id="rule-import.bench-seen-not-imported"></a>**Not data:** a Bench's `first_seen` and `last_seen` are the server's record of the requests it
  received, an import's included. An import neither carries nor keeps them.
- **The way out** is the reads: every call already answers those fields.
- **What an import needs.** `import` in `/info`'s `calls` first. Then each kind of data moves by
  its own call: a server that does not list an optional call (6.1) takes none of what it carries
  (no pointer set as sent without E6: the server picks it, above; no board entry without E15, no
  deleted run without E11).

### 6.8 The server's users

#### U1 · `GET /users`

Optional (6.1). Any login. <a id="rule-U1.users"></a>Answer: the server's **active** users:

```json
{"users": [{"name": "jdupont", "display_name": "Jean Dupont"},
           {"name": "mlefevre", "display_name": "Marie Lefèvre"}]}
```

- `name`: what a run records as its `operator` and a release as its `released_by`. Never `""`.
- `display_name`: what the app shows for that user. Never `""`: the `name` again when the server
  has no other.
- Ordered by `display_name`, then by `name`, each compared by case folding (2.4). A user the
  server no longer lets work (left, disabled) is not listed. No parameter: any is `400
  bad_request` (2.11).

<a id="rule-U1.operator-known"></a>**What listing U1 makes the server check.** A server listing
U1 holds a run's `operator` (E1) and a release's `released_by` (P10) to its users: a name that is
not the `name` of a user U1 lists now, compared by case folding (2.4), is refused with `409
unknown_operator`, kind `invalid` (the outbox sets that entry aside), and nothing is written.
<a id="rule-U1.unnamed-choice"></a>**An absent `operator` or `released_by`** is the server's
choice (owner, 2026-11-10): it takes the run or the release, or refuses it the same way, `409
unknown_operator`, kind `invalid`, nothing written. The app never sends one to a server listing
U1 (Bench waits for the operator, Studio for who releases). Never checked:

- <a id="rule-U1.import-exempt"></a>an import (`"import": true`, 6.7): a name recorded long
  ago may be no user now;
- <a id="rule-U1.operator-exempt"></a>a replay of a run already stored: `already`, whatever the
  list holds now.

The name is kept as sent (a record, 2.4). A server that does not list U1 checks no name.

---

## 7. File storages the app reaches itself

**Read this first.** Section 7 concerns file storages other than the server itself: a folder on a
share, an FTPS server, an SFTP server. Their bytes never pass through the API: the app reads and
writes them directly, and the API carries only references.

- **A server that keeps every byte itself** (one storage, backend `api`) **can skip 7.1 and 7.2.**
- **7.3 applies to a server listing S2, S3 and S4** (optional, 6.1): a project's own
  configuration can name a storage the app writes to itself, and the app declares it to you.

### 7.1 Your own file storages

A server's own storages are set by its administrator. The server reaches them and keeps them:

- <a id="rule-7.1.references-checked"></a>it **checks every reference** into them, as for `api` (6.2): a reference to absent bytes is
  `409 missing_files`;
- <a id="rule-7.1.reclaim"></a>it **reclaims blobs nothing references** (the app never deletes in them);
- <a id="rule-7.1.params-no-secret"></a>`params` never holds a secret: only the non-secret settings listed in 5.15. Passwords and keys
  stay on each PC, filed per server address (`ftps://host:port`, never a folder on it), so one
  sign-in serves every folder on that server. Every `params` value is a string (a port `"22"`, a
  flag `"true"`).
- When the administrator adds one, they **mark** it for this server (7.2).

### 7.2 Inside a file storage

The layout the app reads and writes:

```
<root>/procedo-attachments.json            {"schema_version": 1, "instance_id": "<the server's>"}
<root>/<project_id>/<sha256[0:2]>/<sha256>  one blob, no extension; its name and type are in the JSON
<root>/library/<sha256[0:2]>/<sha256>       a library archive (it belongs to no project)
```

`<root>` is the storage's `root` (a folder) or its server's `base_path`. A blob is a project's:
two projects' copies of the same bytes are two blobs, and a reference counts only for its own
project's.

What the app does there (so you know what to expect in the storage):

| Step | The app's behaviour |
|---|---|
| Before writing, in one of **your** storages | It reads the marker `procedo-attachments.json`. One naming another `instance_id` than S1's, or no marker, is refused (kind `foreign`) and nothing is written: without that guard one server's clean-up would delete another's blobs. Only the server's administrator marks a storage. |
| Before writing, in a **declared** storage | It keeps the marker its own setup has; the server never cleans it. |
| Upload | Skipped when `<sha256>` is there with the right size. Otherwise it writes `<sha256>.<writer>.part`, renames it to `<sha256>`, and checks the size and hash. |
| Read | Needs no marker: a blob whose hash matches **is** the blob. Streamed to a temporary file and kept only when size and hash match. Otherwise the file is shown as missing and tried again later. |
| Delete | In your storages: never (you reclaim). In a declared storage: only what you list (7.3). |

### 7.3 Storages the app declares (S2–S4)

S2, S3 and S4 are optional (6.1); S3 and S4 serve only the storages S2 declared. A project's configuration may keep its files in a storage the server never uses (a NAS folder, a
lab's SFTP server). The app writes there itself and **declares** that storage, so the server's
information is complete even when no client of the server reads it. <a id="rule-7.3.declared-never-cleaned"></a>The server may not reach it:
it then reports `reachable: false`, assumes each reference into it holds, and **never deletes in
it**. Because the server cannot delete there, the app does, from lists the server gives it.

#### S2 · `POST /storages` — declare a project's own storage

<a id="rule-S2.declare"></a>Role: `bench`. Body: `{"backend": "filesystem" | "ftps" | "sftp", "params": {…}, "identity": "…"}`.

- <a id="rule-S2.params-strings-400"></a>Every `params` value is a string; another type: `400 bad_request`.
- <a id="rule-S2.secret-key-400"></a>A key named for a secret (`password`, `passwd`, `passphrase`, `secret`, `private_key`,
  `token`, compared by case folding): `400 bad_request`. Any other key, declared or not, is kept.
- `identity` is compared **exactly**, as the app composes it. An `identity` already declared
  answers `already` with the Storage as first declared, its `params` unchanged (each PC connects
  with its own project configuration, never with these) and the same `store_id`.

Answer: `{"status": "saved" | "already", "storage": Storage}`, with `declared: true`,
`active: false`, and `reachable` saying whether the server reads it.
<a id="rule-S2.reachable-as-found"></a>`reachable` is what the server found when the storage was declared: `true` only when it could
read it then. A storage it cannot open (a host that does not resolve, such as any name under
`.invalid` (RFC 6761); a folder it does not have; credentials refused) is `false`, and S2 still
answers `saved`: the server never refuses a declaration for it. `already` answers it as first
found.

#### S3 · `GET /storages/{store_id}/to_delete`

<a id="rule-S3.to-delete"></a>Role: `bench`. Answer: `{"to_delete": {"<project_id>": ["sha256:<hex>", …]}}`: the blobs of this
declared storage that **no run, artifact or project references any more**, by project. A blob is
listed only once nothing in its own project references it, so a picture two runs share stays while
one of them does. Blobs come to be unreferenced by a purge, a deleted project or a replaced file.

#### S4 · `POST /storages/{store_id}/deleted`

<a id="rule-S4.deleted"></a>Role: `bench`. Body: `{"set_aside": {"<project_id>": ["sha256:<hex>", …]}}`: the blobs the app set
aside to delete. Answer: `{"delete": {…}, "keep": {…}, "to_delete": {…}}`, each by project:

- `delete`: still referenced by nothing; the server drops them from its list; the app deletes its
  set-aside copies;
- `keep`: referenced by a push since S3 listed them; the app puts each back. A blob S3 never
  listed is in `keep` too (the app deletes only what the server listed);
- `to_delete`: what S3 now lists.

<a id="rule-S3S4.declared-only"></a>S3 and S4 take a declared storage only: an unknown `store_id` is `404 not_found`, one of the
server's own storages `400 bad_request` (the server cleans those itself).

**The delete sequence**, which never loses a blob a push referenced meanwhile:

1. S3 lists the blob, by project.
2. The app renames `<root>/<project_id>/<sha[0:2]>/<sha256>` to `<sha256>.<writer>.deleting`
   beside it: no reader or upload finds it there any more.
3. S4 reports what it set aside. The server answers `delete` for those still referenced by nothing
   and `keep` for those a push referenced since S3 listed them.
4. The app deletes the set-aside copies of `delete`. For each of `keep`, it renames its copy back,
   or deletes it when `<sha256>` is there again (a push that found it gone uploaded it anew).

A push between steps 1 and 2 skipped an upload it found there, and is in `keep`. A push after step
2 uploaded the blob again. The app queues these deletes and retries them until done, waiting while
the storage cannot be reached.

<a id="rule-7.3.to-delete-in-purge"></a>E13's answer also carries this project's unreferenced blobs in declared storages (`to_delete`,
keyed by `store_id` as a decimal string).

---

## 8. Conformance: checking your server

### 8.1 What "conformant" means

A server is conformant when the app's **store-then-read-back** suites pass against it: every
action of the app's storage interfaces (projects, library, evidence), stored through one bench and
read back through another, gives back exactly what went in. On top of the calls, these rules must
hold:

1. `/info` with a valid token answers `server`, `api_versions`, `server_time`, `limits`, `calls`,
   `boards_registered` and `user`. With no `Authorization`: `401`, `WWW-Authenticate: Bearer`, `X-Bench-Store: v1`, and
   an envelope whose `details` holds `api_versions` and `server` and nothing else. A valid token
   that may not use this server: `403`, never `401`.
2. Every answer, 4xx and 5xx included, carries `X-Bench-Store: v1`, `X-Request-Id`, and a `Date`
   within 2 s of `server_time`.
3. Every 4xx and 5xx body parses as the envelope of 4.1 with `code`, `kind`, `message` and
   `request_id`, and `message` is a sentence fit to show a user.
4. Whatever the server refuses on its own rules (quota, permission) answers with a status from 4.3
   and a `kind`. 429, 503 and 507 carry `Retry-After` as integer seconds.
5. A `401` is never returned for a permission failure, and a `403` never for a bad token (test
   both, with a read-only token and a garbage one). Plain HTTP refuses or redirects and never
   accepts a token. One connection serves at least five sequential requests.
6. Mounted at `https://serve.com/testprocedure/`, the whole suite passes against
   `https://serve.com/testprocedure/api/v1/…`, with and without a trailing slash on the base. No
   body or header holds an absolute URL of this API, and every `cursor` and `token` is accepted
   back verbatim on the same base.
7. A name holding `/`, `#`, `+`, a blank or a character outside ASCII, sent as a path segment or a
   query value, reaches the server as that one name.
8. Every core call is taken (6.1). Each optional call `/info` lists follows its section; each one
   it does not list answers `501 unsupported`, and so does an `"import": true` when `import` is not
   listed. A server offering no optional call is conformant.

### 8.2 Run the Procedo server conformance kit against your server

The **Procedo server conformance kit** (`procedo_server_kit-<version>.zip`) checks your server
against this contract. It holds:

- the store-then-read-back suites of 8.1, for projects, the library and the evidence, run by
  the app's own API client (shipped in the kit, unchanged), so they check exactly what the app
  will do;
- checks with plain HTTP of the rules every answer follows (rules 1, 2, 3 and 5 of 8.1, the
  `404` with `X-Bench-Store`, the base with and without its trailing slash);
- a **reference server**: a small, standard-library-only Python implementation of this whole
  contract. It is a working example of every rule here; when in doubt about a behaviour, run it
  and compare.
- this contract (`procedo_server_contract.md`) and `coverage.md`, every rule with the case that
  proves it, at the kit's root.

You need Python 3.10 to 3.13 and pytest (`pip install -r requirements.txt`). With no variable
set, `python -m pytest` in the kit's folder runs every case against a reference server it starts
itself: run that first, it should all pass.

**To run it against your server:**

1. Make two logins on a **test instance** of your server, each with a token: a bench login with
   the roles `bench` and `publisher`, and a manager login with the role `manager` (3.5).
2. Leave room in your limits: `max_upload_bytes` and `max_file_bytes` of at least 4 MiB.
   If your server lists U1, make the users the kit's runs and releases name active on it (the
   kit's `README.md` lists them): it checks every `operator` (6.8).
3. Set the variables and run the kit from its folder:

   | Variable | Value |
   |---|---|
   | `PROCEDO_KIT_SERVER` | Your base address, as a user types it: `https://serve.com/testprocedure/` |
   | `PROCEDO_KIT_BENCH_TOKEN` | The bench login's token |
   | `PROCEDO_KIT_MANAGER_TOKEN` | The manager login's token |
   | `PROCEDO_KIT_CA_BUNDLE` | Optional: the CA bundle (PEM) that signs your certificate |

   ```
   python -m pytest
   ```

The kit needs no call that empties a server: each case writes under project ids, names and
library ids of its own, made new for each run, and reads back what it wrote among whatever else
your server holds. What it wrote stays on the server. A case that needs an empty server is
skipped against yours, saying why.

**A server offering some optional calls only** (6.1) runs the same kit. Before any case runs, the
kit reads `/info`'s `calls` and skips each case making an optional call your server does not list,
naming the call; the kit ships the list of the optional calls each case makes. A case that runs
and fails fails: nothing is skipped afterwards. One case checks that each call not listed answers
`501 unsupported`, and that an `"import": true` does too when `import` is not listed, nothing
written. Every rule of the core calls keeps a case making core calls only, so it is checked on
every server, except those needing a fresh project or an import: their cases are skipped where
P11 or `import` is not listed ([0](#0-before-you-start-what-you-must-may-and-choose)).

**A server registering its boards** (`boards_registered: true`, 3.3) refuses the writes the kit
makes on boards of its own (a run, a report, a board entry). Given the importer's token, the kit
registers each such board itself when the server refuses a write on it `409 unknown_board`: it
reads E4 first, and fails when E4 lists that board (a board E4 lists is never refused so); else
it imports a run on it (an import is never refused for its board), checks E4 lists it, deletes
and purges that run at once when the server offers E11 and E13, then sends the refused write
again, and every case runs; without that token, those cases are skipped before they run, saying
so. Such a run is always the kit's own: test id `ffffffff-ffff-4fff-bfff-ffffffffffff`, machine
`0000feed`; the boards it registers stay. Run the kit against a test instance. Register at least
one board in a project you name in `PROCEDO_KIT_BOARDS_PROJECT`: its cases check E4 lists its
boards, a write on a board it does not list is refused, and one on a board it lists is taken.

A server registering its boards and **not listing `import`** gives the kit no way to make a
board: the kit says so once as it starts ("this server registers its boards and takes no import:
the cases that need a fresh board are skipped") and skips those cases with that reason, whatever
token is set. The kit needs imports to check such a server fully.

The kit's `README.md` has the details: how to read a failure, the cases skipped against your
server, and the checks to make by hand (HTTPS, `Retry-After`, names holding `/`, `#` or `+` in
a path or a query, your own refusals, the path prefix behind your real proxy, the proxy
streaming uploads).

---

## 9. The machine-readable twin

<a id="rule-9.twin-schemas"></a>`procedo_api.openapi.yaml` (OpenAPI 3.1) is the machine-readable twin of this document: every
call, every object's JSON schema, the multipart bodies and the error envelope. Use it to generate
a server skeleton or to validate bodies.
