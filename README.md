# Procedo server conformance kit

This kit checks that a server follows the Procedo server contract
(`procedo_server_contract.md`, with its machine-readable twin `procedo_api.openapi.yaml`). Run
it against your own implementation: when every case passes, the Procedo app can use your
server.

> **What the kit does not ask.** No database (SQL or not), language, framework or hosting: the cases
> only look at what your server answers over HTTP. The reference server in `suites/reference_server/`
> keeps everything in memory; it is an example to compare answers against, not a design to copy.

The contract is in this folder, `procedo_server_contract.md`, beside `coverage.md`: every rule
of the contract with the case that proves it. The twin is `suites/procedo_api.openapi.yaml`.

The cases store data through the Procedo app's own API client, then read it back through
another client, as a second bench would, and compare everything: bytes, order, versions,
conflicts, refusals. The client in `vendor/` is the app's code, unchanged, so the kit checks
exactly what the app will do.

## What it checks

| File (in `suites/`) | What it checks |
|---|---|
| `test_server_http_conformance.py` | The rules every answer follows, with plain HTTP: `/info` and its fields; `401` with `WWW-Authenticate` and only the public `details` when the token is missing or wrong; `403` (never `401`) for a valid login without the right; `404` with the server's signature; the headers `X-Bench-Store: v1`, `X-Request-Id` and `Date` on every answer, `Date` within 2 s of `server_time`; the error envelope (`code`, `kind`, `message`, `request_id` equal to `X-Request-Id`); one connection serving five requests in a row; the base address with and without its trailing slash; no answer holding the API's own address. |
| `test_server_contract_conventions.py` | The contract's conventions, transport and errors (§2, §3, §4, §9), with raw HTTP so nothing a client tolerates hides a fault: every answer checked against the twin `procedo_api.openapi.yaml` (shipped beside the cases) and walked for `null` and the headers; `PUT`, `DELETE`, `PATCH`, `HEAD` and a call the server does not take (`405`/`501 unsupported`); unknown fields kept and *answer* fields ignored; names stored in NFC and answered in their first spelling; a malformed test id `400` before the project is looked at; board names `""` or with blanks around them; `format_version` and own fields; text sent in the wrong form or with a wrong version; path names no PC can hold; a query parameter a call does not define; gzip bodies, the replay digest of the JSON with every encoding undone, multipart answers never gzipped; a `cursor` and a `token` taken back; names holding `/`, `#`, `+` and blanks in a path and a query; `401` on every kind of call; a token anywhere but the header; every login reads, a write needs its role, `not_a_manager`; `411`, `413` before the body is read, `416`, `422`; each `409` with its kind and `details`; one server clock. |
| `test_evidence_conformance_api.py` | Every evidence action: runs (complete, stopped, staged), the current-run mark, test ids and renames, reports, exports and drafts with their generations, annotation and board entries, delete, restore, purge, the trash. |
| `test_project_conformance.py` | Every project action: create, read, changes since a token, pushes with conflicts, text and binary files (inside the request, as parts, and alone over 1 MiB, both ways), bundles, tests and their ids, drafts, releases, a release made from a working copy. Every path that never travels as a file (contract §5.2, rule P11), generated from the app's own list (`never_travels.py`): each refused by a new project (`400 bad_request`, nothing created) and by a push (`refused`, kind `invalid`, nothing written, the next change of the request saved). |
| `test_library_conformance.py` | Every library action: publish, list, download, a version published once, conflicts, an archive over 1 MiB both ways. |
| `test_server_contract_evidence.py` | The evidence rules the app's client would hide, with plain HTTP (the client checks a request before sending it, sorts lists, recomputes `current` and a board's lot, reads an error's kind and not its status): a run read back field by field (phases, measurements, files, unknown fields), RunSummary, `deleted_by` the authenticated login, format-1 runs and their board, `lot` on a format-2 run, `run_conflict`, a file over 1 MiB; every E2 filter, its order and its paging past 500 rows; E4's filters and order; E5's test order and `include=current`; the three board parameters required; the pointer rule (ties, older runs, a demoted pointer, a run set by hand, the hand-off on delete), E6's `changed`, replays and "no pointer yet"; annotation and board entries' `current`, `data`, lot and details; artifacts' fields, key order, generations, E8's order (newest first by `generated`, then its ties), filters (`from`/`to` and their bounds, `verdict`) and paging; delete, restore, replays, purge counts and `to_delete`, E14's benches, E16's operators (each once, deleted runs' included, by case folding), E17's readings of one test (each once in its first spelling with its first unit, deleted runs' included, by case folding, another test's never, `test` required). Each refusal is checked by its status, `kind`, `code` and envelope. Each case names the contract rules it checks (`@pytest.mark.rule`, the ids of the contract's coverage table). |
| `test_server_contract_projects.py` | With plain HTTP, the rules the app's client would hide (it checks a request before sending it, sorts what it lists, ignores `missing`, takes parts it did not ask for, reads the kind and not the status): a project known only by its evidence; parts (exactly 1 MiB, over 1 MiB, two with one name, bytes not their name, a size not the file's, one no file names), a multipart answer's form (`multipart/mixed`, the json part first, each file an `attachment` named by its bytes), a blob the project holds referenced without its part, `have`, every `/read` twin against its `GET`, `409 missing_files`, blobs per project, an unknown `store_id`; P1's order, P3's releases, token and `410 gone`, P4's list without files, a `test.json` naming neither format, a project's `store_id`, a bundle's archive kept as sent, P5's `missing`, P9's moves, refusals and malformed changes, P10, a release's `missing` and `numbered_before_releases`, P11's names, test ids and drafts; the library's order, kinds and roles; S1–S4, a reference into the server's own file storage, nothing deleted in a declared one; B1's refusals and limits, B2's ranges; the import (§6.7). Each case checks the status, the `kind`, the `code` and the body. |
| `test_server_load.py` | Off by default: many benches at once (see "Load and soak"). |

Each store-then-read-back case also checks that the client made only the calls the contract
names for that action.

Each case names the contract rules it catches: `rules=(...)` where a store suite registers it
(`conformance(...)`), `@pytest.mark.rule(...)` on a plain test. The names are the anchors of the
contract (`procedo_server_contract.md#rule-<id>`), so a failing case leads to the rules it
checks.

The paths that never travel are not typed into the kit: `suites/never_travels.py` builds them from
the app's own list (in `vendor/`), one path for each name it holds: the root folders
(`evidence/`, `results/`, `.trash/`, `bundle/`, `releases/`…), the names refused at any depth
(`__pycache__`, `.venv`, `.<name>.deleting<n>`, `.<name>.copying<n>`…), the bench's working
files and the `*.part` and `*.bak` files. 26 paths today, each checked twice (a new project
holding it, a push writing it): 52 cases. A name the app adds is checked by the next kit with no
change to the cases. Each case uses a project id and name of its own.

## Requirements

- Python 3.10 to 3.13.
- The packages in `requirements.txt` (pytest only).
- A test instance of your server (see "What the kit leaves on your server").

## Install

```
cd procedo_server_kit-<version>
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt        (Windows: .venv\Scripts\python)
```

## First, run it against the reference server

The kit holds a **reference server** (`suites/reference_server/`): a small Python implementation
of the whole contract, standard library only (plus the app's list of the paths that never travel,
from `vendor/`). It is a working example of every rule; when in
doubt about a behaviour, run it and compare. With no variable set, the kit starts a new one for
each case and runs against it:

```
.venv/bin/python -m pytest
```

Everything should pass (around one minute), but for two cases that run only on demand (the
slow `408` case and the load test, below): they are skipped, saying which variable runs them.
This tells you the kit works on your machine.

To see the kit run against an external server, start the reference server on its own, behind a
path prefix, in a second terminal:

```
.venv/bin/python serve_reference.py --port 8765 --prefix /procedo/
```

It prints the variables to set (below); set them in the first terminal and run the kit
again. Only the cases needing the reference server itself are then skipped (see "Cases skipped
against your server").

## Prepare your server

Give the kit two logins on your server, each with a token:

| Login | Roles (contract §3.5) | Used for |
|---|---|---|
| a bench login | `bench` and `publisher` | every write but the managers' ones, publishing library items |
| a manager login | `manager` | deleting, restoring and purging runs and artifacts |
| an importer login (optional) | `importer` only | moving data in (§6.7), and uploading a project's file (B1); `importer` alone, so the cases can tell the import's writes from a bench's |
| a mover login (optional) | `bench` and `importer` | the login the app moves a project's data in with: the right to move held beside `bench` (§3.5) |
| a bench-only login (optional) | `bench` only, no `publisher` | telling a bench from a publisher (L3 with code, B1 for the library) |

Use a manager login whose name is not `alice`: the cases type that name as the person deleting,
and check it reads back apart from the login.

Your server's limits (`/info`, `limits`) must leave room for what the suites send:
`max_upload_bytes` and `max_file_bytes` of at least 4 MiB (4194304). The case
`test_the_limits_leave_room_for_the_suites` fails otherwise.

The server's clock must be within a few minutes of the clock of the PC running the kit.

## Run it against your server

Set these environment variables, then run `python -m pytest` from the kit's folder:

| Variable | Value |
|---|---|
| `PROCEDO_KIT_SERVER` | Your server's base address, as a user would type it in the app: `https://host/prefix/` (the trailing slash is optional). The kit adds `api/v1/`. |
| `PROCEDO_KIT_BENCH_TOKEN` | The bench login's token. |
| `PROCEDO_KIT_MANAGER_TOKEN` | The manager login's token. |
| `PROCEDO_KIT_IMPORTER_TOKEN` | Optional: the importer login's token. Unset, the import's cases are skipped, saying so. |
| `PROCEDO_KIT_MOVER_TOKEN` | Optional: the mover login's token. Unset, the case needing it is skipped, saying so. |
| `PROCEDO_KIT_BENCH_ONLY_TOKEN` | Optional: the bench-only login's token. Unset, the two cases needing it are skipped, saying so. |
| `PROCEDO_KIT_CA_BUNDLE` | Optional: a PEM file of the CA that signed your server's certificate, when it is not one your system trusts. |

```
export PROCEDO_KIT_SERVER=https://serve.example.com/testprocedure/
export PROCEDO_KIT_BENCH_TOKEN=...
export PROCEDO_KIT_MANAGER_TOKEN=...
.venv/bin/python -m pytest
```

(Windows PowerShell: `$env:PROCEDO_KIT_SERVER = "https://..."`, and so on.)

The first line of the run says which server is under test. Plain `http` is only for a test
server on the loopback address (`http://127.0.0.1:...`), as the contract says (§3.1).

Useful options: `-x` (stop at the first failure), `-k <words>` (only the cases whose name holds
the words), `--lf` (only the cases that failed last time), `-v` (one line per case).

## Slow checks, load and soak (off by default)

| Variable | What it switches on |
|---|---|
| `PROCEDO_KIT_SLOW=1` | The case for `408 request_timeout` (contract §4.3): it sends half a request's body, then stalls, and expects your server to give up with `408`, code `request_timeout`, kind `timeout`, its headers and the envelope, nothing written. It waits up to `PROCEDO_KIT_SLOW_WAIT` seconds (300 by default) for that answer; set it above your server's (and your proxy's) body timeout. A proxy answering `408` itself fails the case: the answer must carry `X-Bench-Store`. |
| `PROCEDO_KIT_LOAD=1` | The load test, `test_server_load.py`: `PROCEDO_KIT_LOAD_BENCHES` benches at once (20 by default), each doing what a real bench does, a few rounds each: push a run with its record and a picture, upload a report, annotate the run, set the board's lot, read the run and the report back. Setting `PROCEDO_KIT_LOAD_BENCHES` or `PROCEDO_KIT_LOAD_MINUTES` switches it on too. |
| `PROCEDO_KIT_LOAD_MINUTES` | Makes the load a **soak** run: the benches go on, round after round, for that many minutes. |

The load test **fails** when anything is lost, refused or read back different (a field, a byte,
a run a board no longer lists). A `429` or `503` with `Retry-After` is not a failure: the bench
waits as the app does and sends the request again, and the report counts it. It **reports** each
call's response times (median, p95, max) at the end of the run, without failing on them:

```
PROCEDO_KIT_LOAD=1 PROCEDO_KIT_LOAD_BENCHES=40 PROCEDO_KIT_LOAD_MINUTES=30 \
    .venv/bin/python -m pytest suites/test_server_load.py
```

It writes under a project id of its own, one serial per bench (`LOAD-<n>-<6 hex digits>`).

## What the kit leaves on your server

The contract has no call to empty a server, so the kit never needs one. Each case writes under
names of its own, made new for each run, and reads back only what it wrote among whatever else
the server holds; running the kit again is fine. What stays on the
server afterwards:

- projects named `Ripple bench <8 hex digits>`, each under a new project id;
- evidence (runs, reports, exports, drafts, entries, the trash) under new project ids;
- library items whose ids end in `-<8 hex digits>` (`baseline-…`, `acme-…`, `sheet-…`), or
  begin with `kit-<8 hex digits>`;
- projects named `kit <8 hex digits> …` and `KIT <8 hex digits> …`;
- declared storages (S2) whose identity is `sftp://kit-<8 hex digits>.invalid:22/kit`, a host no
  server reaches, and `filesystem` ones whose root is a folder of the kit's temporary folder on
  the PC running it (`…/declared-<8 hex digits>`, §7.3: the server must not delete there);
- with `kit_data.py fill`, a project named `Dataset <8 hex digits>` and the library item
  `kit-<8 hex digits>-dataset`;
- benches whose `machine_id` is new for each run (E14).

Run it against a test instance, not a server people use.

## Reading a failure

Each case is named after the rule it checks, for example
`test_every_action_reads_back_as_it_went_in_over_the_json_api[a_release_reads_back_with_its_documents-api]`.
Read the case in `suites/` (search for `def a_release_reads_back_with_its_documents`): its
docstring says what the rule is.

- **An `assert` with two values** (`assert [...] == [...]`): what was read back differs from
  what was written. pytest shows both; the difference is what your server changed, lost or
  added.
- **An `EvidenceError`**: your server refused a call the case expected to succeed. The error
  shows the `kind` and `code` the client read and your server's `message`.
- **`... calls the action map does not name for these actions`** or **`outside the map`**: the
  client had to make a call the contract does not name for that action (often a retry after an
  answer it did not expect). Look at the answer before it.
- **A case of `test_server_http_conformance.py`**: the message names the header, status or
  field that is wrong.

If a case fails against your server but passes against the reference server, run the same
case against `serve_reference.py`, and compare the two servers' answers to the same request.

## Cases skipped against your server

| Case | Why |
|---|---|
| `test_project_conformance.py` `an_empty_storage_lists_no_project` | It needs a server holding no project; it runs only against the reference server the kit starts. |
| `test_server_contract_evidence.py` `test_e13_purges_what_was_deleted_strictly_longer_ago_than_the_retention` | It moves the server's clock past the retention period; no call of the contract does that. Check it by hand: a run deleted more than `retention_days` ago goes at the next purge (E13), one deleted exactly that long ago stays. |
| `test_server_contract_evidence.py` `test_e13_purging_a_run_a_pointer_still_names_hands_the_pointer_on` | It needs an importer's login (an imported delete leaves the pointer on the deleted run); the kit has none. |
| `test_server_contract_evidence.py` `test_a_file_gone_from_the_api_storage_is_reported_missing` | It takes a blob away from the server's storage; no call does that. Check it by hand: remove a run's or a report's bytes from your storage, then read it (E3, E9): it answers, the file in `missing`. |
| `test_server_contract_projects.py` `test_a_damaged_blob_is_missing_and_the_bytes_sent_again_replace_it` | It damages a blob on the reference server's own disk. |
| `test_server_contract_projects.py` `test_b1_is_held_to_max_file_bytes_never_to_max_upload_bytes` | It needs the server's limits set small. |
| `test_server_contract_projects.py`, the cases of the import (§6.7) and `test_b1_takes_a_projects_bytes_…` | Only when `PROCEDO_KIT_IMPORTER_TOKEN` is not set. |
| `test_server_contract_projects.py` `test_only_a_publisher_publishes_an_item_holding_code`, `test_b1_takes_a_projects_bytes_…` | Only when `PROCEDO_KIT_BENCH_ONLY_TOKEN` is not set. |
| `test_server_contract_conventions.py`: the cases sending a file as a part (gzip whole multipart, `416`, `422`, a part over 1 MiB, `missing_files`, a library version) | They need a storage of backend `api` (`GET /storages`); a server keeping no byte itself skips them, saying so. |
| `test_server_contract_conventions.py` `test_a_body_that_stops_arriving_is_408_request_timeout` | Only when `PROCEDO_KIT_SLOW=1` is not set (slow: it waits for your server to give up). |
| `test_server_load.py` | Only when `PROCEDO_KIT_LOAD=1` is not set. |
| `test_server_contract_projects.py` `test_a_reference_into_the_servers_own_file_storage_is_checked` | When your server lists no storage of its own but `api` (§7.1 is then not yours). With one, the case checks a reference to bytes not there (`409 missing_files`), and, when the PC running the kit can write that storage's folder (a `filesystem` storage marked for your server), puts them there and checks the write is then taken. |
| `test_server_contract_projects.py` `test_a_releases_document_the_server_cannot_give_is_missing_by_its_name` | It takes a blob away from the reference server's own disk. Check it by hand: remove a released document's bytes from your storage, then read the release (P8b): it answers, the document's name in `missing`. |

Every other case runs against your server. One rule no case can check: a run push's
`project_name` (E1) names a project the push creates, but no call answers that name.

The case `test_the_server_never_deletes_in_a_storage_the_app_declares` declares a `filesystem`
storage whose folder is on the PC running the kit, references blobs there, then makes them
unreferenced (a file deleted, a run deleted and purged): every blob must still be there,
unchanged, and listed by S3. When your server reaches that folder (`reachable: true`, a server
on the same PC) it also checks a reference to bytes not there is `409 missing_files`.

## Check these by hand

The kit cannot provoke or observe these; check them with `curl` against your deployed address.

1. **HTTPS.** Your server answers `https` only; plain `http` is refused or redirected, and never
   accepts a token (contract §3.6, §8.1 rule 5).
2. **`Retry-After`.** A `429`, `503` or `507` carries `Retry-After` as integer seconds (§4.3).
3. **Names in paths and queries.** A name holding `/`, `#`, `+`, a blank or a character outside
   ASCII, sent percent-encoded as a path segment or as a query value, reaches your server as that
   one name; a `+` in a query value is a plus, not a space (§3.1, §8.1 rule 7).
   `test_server_contract_conventions.py` sends such a name as a board's serial (a path segment)
   and part number (a query value) only; a test's, an artifact's or a library item's name in a
   path, and every name through your real proxy (8.), check by hand.
4. **`403` for a login not allowed on this server at all**: a valid token whose login may not use
   the server gets `403` from every call, `/info` included, never `401` (§3.3).
5. **`X-Bench-Store` from the API process**, not added by a proxy: stop the API process behind
   your proxy; the proxy's own error must not carry `X-Bench-Store` (§3.2).
6. **Errors from your own rules** (quota, size, rate): each answers a status of §4.3 with the
   envelope and a `kind` (§4, §8.1 rule 4). The suites see only the contract's own refusals.
7. **`5xx` answers**: carry `X-Bench-Store`, `X-Request-Id`, `Date` and the envelope (§3.2,
   §4.1), like every other answer.
8. **The path prefix in your real deployment.** Run the kit with `PROCEDO_KIT_SERVER` set to the
   address users will type, through your real reverse proxy, with and without the trailing
   slash.
9. **`500 internal`** (§4.3). Make your server fail on something it does not handle: make the
   store behind it fail, whatever your server keeps its data in (a database, a folder, a
   bucket): stop it, or make it read-only. Then:

   ```
   curl -i -H "Authorization: Bearer $PROCEDO_KIT_BENCH_TOKEN" "${PROCEDO_KIT_SERVER%/}/api/v1/projects"
   ```

   Expect status `500`, the headers `X-Bench-Store: v1`, `X-Request-Id` and `Date`,
   `Content-Type: application/json`, and the body
   `{"error": {"code": "internal", "kind": "internal", "message": "<a sentence>", "request_id": "<the X-Request-Id>"}}`
   (`details` optional), with no stack trace or internal detail in it. If your server reports
   the stopped store as `503 unavailable` instead, that is right too, with kind `unreachable`
   and `Retry-After` (check 2); then try the other way (the store made read-only, and a run
   pushed with a file as a part) to reach a `500`. Bring the store back afterwards.
10. **Reclaiming blobs nothing references** (§7.1), in each storage your server keeps itself
    (`api`, and any file storage of its own). For a new project id:
    1. upload a file of 5 MiB alone (B1), push a run referencing it (E1);
    2. delete the run (E11) and purge it (E13, with `items` naming it), as the manager;
    3. note the size of the storage's folder (or of the project's folder in it);
    4. after your server's own reclaim period, the blob is gone: the folder is 5 MiB smaller,
       and `GET /api/v1/storages/<store_id>/blobs/<project_id>/<sha256>` answers `404`.

    Repeat with the file referenced by two runs, only one of them purged: the blob stays.
11. **An upload nothing references is kept at least 24 hours** (B1). Upload a file alone (B1)
    for a new project id and reference it from nothing. 24 hours later (run your reclaim job
    in between, if it has one):
    `GET /api/v1/storages/<store_id>/blobs/<project_id>/<sha256>` still answers `200` with the
    same bytes, and a run referencing the file without sending it (no part) is `saved`, not
    `409 missing_files`.
12. **Your reverse proxy streams uploads** (§3.6 Proxy), if one fronts the API. The kit cannot
    tell a proxy holding a body from a slow server, so check it through your real address, with
    the `api` storage's `store_id` (S1) and a new project id:
    1. make a file of 100 MiB, below `max_file_bytes`:

       ```
       head -c 104857600 /dev/urandom > big.bin
       sha=$(sha256sum big.bin | cut -c1-64)
       ```

    2. upload it slowly (about 50 s) and time it:

       ```
       curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' --limit-rate 2M \
         -H "Authorization: Bearer $PROCEDO_KIT_BENCH_TOKEN" \
         -H "Content-Type: application/octet-stream" --data-binary @big.bin \
         "${PROCEDO_KIT_SERVER%/}/api/v1/storages/<store_id>/blobs/<project_id>/$sha"
       ```

       Expect `200`. In the API process's own log, the request arrives within a few seconds
       of curl's start, not about 50 s later (a proxy holding the whole upload hands it over
       only at its end), and its answer within 30 s of the upload's last byte;
    3. download it back and time the first byte:

       ```
       curl -sS -o /dev/null -w '%{http_code} %{time_starttransfer}\n' \
         -H "Authorization: Bearer $PROCEDO_KIT_BENCH_TOKEN" \
         "${PROCEDO_KIT_SERVER%/}/api/v1/storages/<store_id>/blobs/<project_id>/$sha"
       ```

       Expect `200` and a first byte within 30 s.

    A proxy that answers `Expect: 100-continue` itself (nginx does) is allowed; run through it
    (check 8), `test_a_body_over_the_limit_is_413_before_it_is_read[...-expect-100]`,
    `test_a_body_over_its_limit_is_413_before_it_is_sent` and
    `test_a_body_over_its_limit_is_413_before_anything_else_is_looked_at[expect-100]` then
    fail: run them against the API process directly.

## Backups and upgrades: `kit_data.py fill` and `verify`

`kit_data.py` writes a known, varied data set to your server and checks later that every item
and every byte of it is still there and the same. It talks to the server the variables name
(`PROCEDO_KIT_SERVER`, `PROCEDO_KIT_BENCH_TOKEN`, `PROCEDO_KIT_MANAGER_TOKEN`).

- `fill` writes a project (text and binary files, one over 1 MiB, a bundle), a change to it, two
  releases of a test, a library item, runs with their files (a picture each, one large file)
  on a board, annotation and board entries, a report in two generations, an export, and a run
  and an export deleted (in the trash). It reads everything back and writes the **manifest**,
  `kit_dataset.json` (or `--manifest <file>`): each read with the answer your server gave, and
  every blob. Keep that file.
- `verify` makes the same reads and downloads every blob alone (B2): it prints each difference
  (`DIFFERS  <read>: <where>: <was> → <now>`, a blob gone or changed) and exits with status 1,
  or says nothing differs and exits with 0. The change token is left out of the comparison (it
  is opaque, §3.1); everything else must be the same, server-set times included.

**A backup and its restore:**

1. `python kit_data.py fill`
2. Back up your server as your procedure says: whatever it keeps its data in (a database, a
   folder, a bucket), and its storages.
3. Wipe it: a new, empty instance, or everything it keeps emptied.
4. `python kit_data.py verify` now fails, listing everything missing: the check sees a loss.
5. Restore the backup.
6. `python kit_data.py verify`: nothing differs.

**An upgrade:**

1. Before the upgrade, `python kit_data.py fill`.
2. Upgrade your server (and migrate its data).
3. `python kit_data.py verify`: nothing differs. Then run the whole kit (`python -m pytest`)
   against the upgraded server.

## Try it with the real app

The kit checks the API; this checks the whole path, in the order a lab meets it, with the Procedo
app on two PCs. The app calls your server a **Web server**. Each step says what Procedo shows
when it works.

1. **Sign in.** Bench: **File ▸ Settings…**, page **Server**, **Add server…**; **Server type**
   *Web server*, **Server address** your base address; **Token:** a bench login's token,
   **Save sign-in**. **Check access** shows *Server reachable*. **Test sign-in** shows
   *Reachable: yes*, *List: ok* and *Signed in as `<login>` (`<roles>`).*
2. **Create a project on the server.** **File ▸ New Project…**; under **Name and place**,
   **Location** your server; **Create project**. While it runs: *Publishing the project to …*.
   The project opens; the bottom of the window shows **Server: synced**.
3. **A second PC checks it out.** On the second PC, sign in as in step 1, then **File ▸ Open
   Project from Server…**: the project is listed under **Project**, with your server under
   **Server**. **Open** shows *Checking out … from …*, then the project opens, **Server:
   synced**, the same tests in it.
4. **Run a test on a bench.** Run a test on a board to its end. **Test ▸ Details…**, tab
   **Results**: the run's card (verdict, date, serial), marked **current**. **Reports ▸ Generate
   Report…**: *Saved as evidence of board … — listed in the Report Browser.* **Reports ▸ View
   Reports…** opens the **Report Browser**, the report listed. On the second PC, the run and
   the report are there too.
5. **Release a test.** In Procedo Studio, open the project, select the test, **Release…**,
   then **Release v1**: the footer says *`<test>` released as v1*, and the chip beside the test
   reads **v1**. **Versions…** lists it. Released again from the other PC, the next number
   follows (the server gives it).
6. **Search.** **Reports ▸ Search Results…** lists the project whose results your server keeps,
   with your server's address, and it can be ticked (greyed *no sign-in on this PC for …* only
   when this PC has no sign-in for your server). **Search** asks your server: runs with E2,
   reports with E8 (`kind=report&include=info`, with the dates and the verdict asked),
   `limit=500` a page, newest first; the window shows at most the newest 500. The run you made in step 4 and its report are listed;
   **Operator**, **Bench** and **Measurement** (the second line) narrow the runs through E2.
   Double-click the run: it is fetched to this PC (*Opening…*) and its folder opens.
7. **Move a project onto the server and back.** Open a project kept in a folder, then
   **Project ▸ Move or Copy Project…**: **Move the project**, **To** *A server*, your server,
   **Name on the server**; the **What will happen** card describes it; **Move project**. The
   project reopens from the server (**Server: synced**). Move it back the same way, **To** *A
   folder*: it becomes an ordinary project again; its copy on the server stays there. (A move
   waits while uploads still wait on this PC.)
8. **Delete and restore a run.** The app deletes and restores with the PC's own sign-in, so give
   that login the role `manager` too for this step. A run's **⋯** ▸ **Delete**, a reason,
   **Move to trash**. The run leaves the **Results** tab; if it was
   current, the most recent earlier run becomes current. **Reports ▸ Evidence Trash…** lists
   it (*Deleted runs and reports of this project, kept … days*); **Restore** puts it back in
   the history.
9. **Work offline.** Stop your server (or unplug the bench's network), then run a test: the
   footer shows *1 run pending upload*, and **Reports ▸ Waiting Uploads…** (window **Waiting
   uploads**) lists it. Start the server again and click **Retry now** (or wait: the app
   retries after 1, 2, 4, 8, 15 and 30 minutes, then every hour): the row leaves the window, the
   footer count goes, and the run is on the server (the second PC sees it).

**Security** beyond logins and roles (a penetration test: the TLS setup, injection, access to
other projects' data through crafted requests, rate limits, the hosting) is the server team's
own job. The kit checks the contract's logins and roles (`401`, `403`, who may write, delete,
publish, import), nothing more.

## What is in the kit

| Path | What it is |
|---|---|
| `suites/` | The cases, the module that picks the server (`server_target.py`), the contract's twin (`procedo_api.openapi.yaml`) with the module reading it (`openapi_twin.py`, standard library only), and the reference server. |
| `vendor/procedo/` | The Procedo app's API client and the modules it imports, as the app ships them. The kit imports nothing else but pytest and the Python standard library. |
| `conftest.py`, `pytest.ini` | How pytest finds and runs the cases; each case runs in a sandbox of its own (the client's per-user folders point into pytest's temporary folder). |
| `serve_reference.py` | The reference server on its own, behind a path prefix (`--body-timeout`: the seconds a stalled body waits before `408`). |
| `kit_data.py` | The data set's `fill` and `verify`, for backups and upgrades. |
| `VERSION` | The Procedo version and the commit the kit was built from. |
