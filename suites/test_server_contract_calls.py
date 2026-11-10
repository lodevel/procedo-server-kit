"""What a server offers beyond the core calls (``procedo_server_contract.md`` §3.3, §6.1, §6.8):
``/info``'s ``calls`` and ``boards_registered``, the ``501 unsupported`` of an optional call it
does not list, the empty change token of a server without P3, its users (U1) and the names a run
and a release must then give, and the boards it registers itself. Raw HTTP (the ``Api`` of
``test_server_contract_conventions``, every answer checked against the twin), against the
reference server or the server the environment names (``server_target``).

Against the reference server, a case also starts one offering a part of the optional calls only
(or registering its boards), so both sides of each rule are checked; against yours, the side its
``/info`` says.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
import uuid
import warnings
from contextlib import ExitStack
from typing import Any

import pytest
import server_target
from reference_server.offer import CORE, OPTIONAL
from test_server_contract_conventions import (
    LOCAL_TOKENS,
    MACHINE,
    Api,
    board_entry,
    board_query,
    ok,
    project_body,
    refused,
    report_body,
    run_body,
    seg,
    sha,
    text_file,
)

#: Every case reads /info itself and checks what the server lists and what it does not.
pytestmark = [pytest.mark.servers, pytest.mark.offer_aware]

rule = pytest.mark.rule

TOKENS = {**LOCAL_TOKENS, "t-importer": {"name": "importer-login", "roles": ["importer"]}}
#: A project on your server holding boards registered on it (``boards_registered: true``).
BOARDS_PROJECT_ENV = "PROCEDO_KIT_BOARDS_PROJECT"
TEST_ID = "5e0f2a6b-1c3d-4e8f-9a0b-1c2d3e4f5a6b"
RUN_ID = "2026-10-26T08-00-00Z_0123456789abcdef"


def fold(name: str) -> str:
    """How names compare (§2.4): NFC, then case folding."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def probes(pid: str) -> dict[str, list[tuple[str, str, str, dict | None]]]:
    """For each optional call, requests making it: ``(method, path, query, body)``. Each names a
    project no server holds, so a server offering the call refuses it (or answers it empty), and
    one not offering it answers ``501`` first (§2.13: the call before the form, the roles and
    what it names)."""
    board = board_query()
    return {
        "P3": [
            ("GET", f"projects/{pid}", "since=kit", None),
            ("POST", f"projects/{pid}/read", "", {"since": "kit", "have": []}),
        ],
        "P4": [("GET", f"projects/{pid}/tests", "", None)],
        "P5": [("GET", f"projects/{pid}/tests", "names=T1", None)],
        "P6": [("POST", f"projects/{pid}/tests/read", "", {"names": ["T1"]})],
        "P8": [("GET", f"projects/{pid}/releases/{TEST_ID}", "", None)],
        "P8b": [
            ("GET", f"projects/{pid}/releases/{TEST_ID}/1", "", None),
            ("POST", f"projects/{pid}/releases/{TEST_ID}/1/read", "", {"have": []}),
        ],
        "E2": [("GET", "runs", f"project_id={pid}", None)],
        "E6": [
            (
                "POST",
                f"projects/{pid}/boards/SN-1/tests/{TEST_ID}/current",
                board,
                {"run_id": RUN_ID, "replaces": None, "machine_id": MACHINE},
            )
        ],
        "E11": [
            ("POST", f"projects/{pid}/runs/{RUN_ID}/delete", "", {"machine_id": MACHINE}),
            ("POST", f"projects/{pid}/runs/{RUN_ID}/restore", "", {"machine_id": MACHINE}),
        ],
        "E12": [
            ("POST", f"projects/{pid}/artifacts/delete", "", {"machine_id": MACHINE}),
            ("POST", f"projects/{pid}/artifacts/restore", "", {"machine_id": MACHINE}),
        ],
        "E13": [("POST", f"projects/{pid}/purge", "", {"machine_id": MACHINE})],
        "E14": [("GET", "benches", "", None)],
        "E15": [("POST", f"projects/{pid}/boards/SN-1/entries", board, {"entries": "none"})],
        "E16": [("GET", f"projects/{pid}/operators", "", None)],
        "E17": [("GET", f"projects/{pid}/readings", "test=T1", None)],
        "L1": [("GET", "library", "", None)],
        "L2": [
            ("GET", "library/config/kit-never-published/1.0", "", None),
            ("POST", "library/config/kit-never-published/1.0/read", "", {"have": []}),
        ],
        "L3": [("POST", "library/config", "", {"kind": "nothing"})],
        "S2": [("POST", "storages", "", {"backend": "nothing"})],
        "S3": [("GET", "storages/999999/to_delete", "", None)],
        "S4": [("POST", "storages/999999/deleted", "", {"set_aside": {}})],
        "U1": [("GET", "users", "", None)],
    }


def core_probes(pid: str, store: int) -> dict[str, list[tuple[str, str, str, dict | None]]]:
    """For each core call, requests making it, naming nothing the server holds: refused or
    answered, never ``501`` (§6.1). P2b without ``since`` is core whatever the server offers."""
    blob = f"storages/{store}/blobs/{pid}/{'5' * 64}"
    board = board_query()
    return {
        "info": [("GET", "info", "", None)],
        "P1": [("GET", "projects", "", None)],
        "P2": [("GET", f"projects/{pid}", "", None)],
        "P2b": [("POST", f"projects/{pid}/read", "", {"have": []})],
        "P7": [
            ("GET", f"projects/{pid}/tests/T1", "", None),
            ("POST", f"projects/{pid}/tests/T1/read", "", {"have": []}),
        ],
        "P9": [("POST", f"projects/{pid}/changes", "", {"changes": []})],
        "P10": [("POST", f"projects/{pid}/releases/{TEST_ID}", "", {"format": "nothing"})],
        "P11": [("POST", "projects", "", {"format_version": 1})],
        "E1": [("POST", f"projects/{pid}/runs", "", {"format_version": 2})],
        "E3": [
            ("GET", f"projects/{pid}/runs/{RUN_ID}", "", None),
            ("POST", f"projects/{pid}/runs/{RUN_ID}/read", "", {"have": []}),
        ],
        "E4": [("GET", f"projects/{pid}/boards", "", None)],
        "E5": [
            ("GET", f"projects/{pid}/boards/SN-1", board, None),
            ("POST", f"projects/{pid}/boards/SN-1/read", board, {"have": []}),
        ],
        "E7": [("POST", f"projects/{pid}/runs/{RUN_ID}/annotation", "", {"entries": []})],
        "E8": [("GET", f"projects/{pid}/artifacts", "", None)],
        "E9": [("GET", f"projects/{pid}/artifacts/report/kit.html", f"serial=SN-1&{board}", None)],
        "E10": [("POST", f"projects/{pid}/artifacts", "", {"kind": "nothing"})],
        "S1": [("GET", "storages", "", None)],
        "B2": [("GET", blob, "", None)],
    }


def servers(stack: ExitStack, tmp_path, **variants: dict) -> list[str]:
    """The servers a case checks: yours alone; or the reference server as the suites start it
    (every optional call), then one per *variant* (its own ``calls``, ``boards``, …)."""
    found = []
    for name, limits in {"default": {}, **variants}.items():
        if server_target.external() and limits:
            continue
        base, _running = stack.enter_context(server_target.serve(tmp_path / name, TOKENS, **limits))
        found.append(base)
    return found


def api_of(stack: ExitStack, base: str, twin) -> Api:
    api = Api(base, twin)
    stack.callback(api.close)
    return api


@pytest.fixture(scope="module")
def twin():
    from openapi_twin import Twin

    return Twin()


# ---- §3.3, §6.1 The optional calls -------------------------------------------------------------


@rule("3.3.info-calls", "6.1.unlisted-501", "6.1.core-calls", "4.3.405-501")
def test_info_lists_the_optional_calls_offered_and_each_one_not_listed_is_501(tmp_path, twin):
    """``calls`` lists optional calls only, each once; every core call is taken and every call
    listed too (never ``501``); every optional call not listed answers ``501 unsupported``,
    kind ``invalid``. Against the reference server, once offering every call, once none."""
    with ExitStack() as stack:
        for base in servers(stack, tmp_path, none={"calls": []}):
            api = api_of(stack, base, twin)
            calls = ok(api.call("GET", "info"))["calls"]
            assert isinstance(calls, list) and all(isinstance(c, str) for c in calls), calls
            assert len(set(calls)) == len(calls), f"{calls}: a call listed twice"
            assert not set(calls) & set(CORE), f"{calls}: a core call listed"
            assert set(calls) <= set(OPTIONAL), f"{calls}: not optional calls of the contract"
            for call, requests in probes(server_target.fresh_id()).items():
                for method, path, query, body in requests:
                    reply = api.call(method, path, json_body=body, query=query)
                    if call in calls:
                        assert reply.status != 501, f"{call} is listed: {reply}"
                    else:
                        refused(reply, 501, "invalid", "unsupported")
            # A core call is never 501, whatever the server offers.
            storages = ok(api.call("GET", "storages"))["storages"]
            assert storages, "S1 lists no storage"
            store = storages[0]["store_id"]
            for call, requests in core_probes(server_target.fresh_id(), store).items():
                for method, path, query, body in requests:
                    reply = api.call(method, path, json_body=body, query=query)
                    assert reply.status != 501, f"{call}, a core call, answers 501: {reply}"
            data = b"kit core upload " + uuid.uuid4().bytes
            upload = api.call("POST", f"storages/{store}/blobs/{server_target.fresh_id()}/"
                              f"{hashlib.sha256(data).hexdigest()}", data=data,
                              headers={"Content-Type": "application/octet-stream"})  # fmt: skip
            assert upload.status != 501, f"B1, a core call, answers 501: {upload}"


@rule("P2.token-empty-without-P3")
def test_without_p3_every_change_token_is_empty_and_with_it_none_is(tmp_path, twin):
    """§6.3, P2: on a server that does not list P3, every answer's ``token`` is ``""`` (P1,
    P2, P2b, P4, P5, P6, P9, P11); on one listing it, none is. Against the reference server,
    once with every call, once without P3."""
    without_p3 = {"calls": [c for c in OPTIONAL if c != "P3"]}
    with ExitStack() as stack:
        for base in servers(stack, tmp_path, without_p3=without_p3):
            api = api_of(stack, base, twin)
            offered = set(ok(api.call("GET", "info"))["calls"])
            pid, tid = server_target.fresh_id(), str(uuid.uuid4())
            body = project_body(pid, tid)
            tokens = {"P11": ok(api.call("POST", "projects", json_body=body))["token"]}
            listed = ok(api.call("GET", "projects"))["projects"]
            tokens["P1"] = next(p["token"] for p in listed if p["project_id"] == pid)
            tokens["P2"] = ok(api.call("GET", f"projects/{pid}"))["token"]
            tokens["P2b"] = ok(api.call("POST", f"projects/{pid}/read", json_body={"have": []}))[
                "token"
            ]
            text = "# T1, again\n"
            change = {
                "path": "tests/T1/procedure_text.md",
                "replaces": body["files"]["tests/T1/procedure_text.md"]["version"],
                "text": text,
            }
            pushed = api.call("POST", f"projects/{pid}/changes", json_body={"changes": [change]})
            tokens["P9"] = ok(pushed)["token"]
            if "P4" in offered:
                tokens["P4"] = ok(api.call("GET", f"projects/{pid}/tests"))["token"]
            if "P5" in offered:
                tokens["P5"] = ok(api.call("GET", f"projects/{pid}/tests", query="names=T1"))[
                    "token"
                ]
            if "P6" in offered:
                read = api.call("POST", f"projects/{pid}/tests/read", json_body={"names": ["T1"]})
                tokens["P6"] = ok(read)["token"]
            if "P3" in offered:
                assert all(isinstance(t, str) and t for t in tokens.values()), tokens
            else:
                assert all(t == "" for t in tokens.values()), tokens


# ---- §6.7 An import on a server without E6 ---------------------------------------------------


def incomplete(test_id: str) -> dict:
    """A run of *test_id* stopped before its end, started after every other run of these cases."""
    return run_body(test_id, day=23, complete=False, ended=None, verdict=None)


@rule("import.E1-rule-without-E6", "5.13.incomplete-never-current")
def test_without_e6_an_imported_run_moves_the_pointer_by_the_rule_in_any_order(tmp_path, twin):
    """§6.7: on a server not listing E6, an imported run moves the pointer as a bench's run does
    (5.13): the newest complete run is current whichever came in first, a tie going to the
    greater ``run_id``, an incomplete run never (on a server listing E6, an imported run moves
    no pointer: ``import.E1-no-pointer``). Against the reference server without E6; against
    yours when its /info does not list E6."""
    without_e6 = {"calls": [c for c in OPTIONAL if c != "E6"]}
    with ExitStack() as stack:
        bases = [
            base
            for base in servers(stack, tmp_path, without_e6=without_e6)
            if "E6" not in server_target.offered(base)
        ]
        if not bases:
            server_target.skip("the server offers E6: an imported run moves no pointer there")
        for base in bases:
            api = api_of(stack, base, twin)
            test_id = str(uuid.uuid4())
            older, newer = run_body(test_id, day=20), run_body(test_id, day=21)
            tie = sorted(
                (run_body(test_id, day=22), run_body(test_id, day=22)), key=lambda r: r["run_id"]
            )
            stopped = incomplete(test_id)  # the newest run, never current
            for first, then, current in (
                (newer, older, newer),  # out of order: the older one, imported last, stays out
                (older, newer, newer),
                (tie[1], tie[0], tie[1]),  # a tie: the greater run_id, whichever came first
                (tie[0], tie[1], tie[1]),
            ):
                pid = server_target.fresh_id()
                for run in (first, stopped, then):
                    body = {**run, "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
                    ok(api.call("POST", f"projects/{pid}/runs", role="importer", json_body=body))
                board = ok(api.call("GET", f"projects/{pid}/boards/SN-1", query=board_query()))
                pointers = {t["test_id"]: t.get("pointer") for t in board["tests"]}
                pointer = pointers.get(test_id) or {}
                assert pointer.get("run_id") == current["run_id"], (
                    "without E6 the newest complete run imported is current",
                    first["run_id"],
                    then["run_id"],
                    board,
                )


@rule("import.delete-hands-on-without-E6", "5.13.handoff-complete-only")
def test_without_e6_an_imported_delete_hands_the_pointer_on_as_a_benchs_does(tmp_path, twin):
    """§6.7: on a server not listing E6, an imported delete of the current run hands the pointer
    on as a bench's delete does (5.13): to the newest remaining complete run, never an
    incomplete one, else no pointer (on a server listing E6 it hands nothing on:
    ``import.delete-keeps-pointer``). Against the reference server without E6; against yours
    when its /info lists E11 and not E6."""
    without_e6 = {"calls": [c for c in OPTIONAL if c != "E6"]}
    with ExitStack() as stack:
        bases = [
            base
            for base in servers(stack, tmp_path, without_e6=without_e6)
            if "E6" not in server_target.offered(base) and "E11" in server_target.offered(base)
        ]
        if not bases:
            server_target.skip("the server offers E6, or no E11: this case cannot be made there")
        for base in bases:
            api = api_of(stack, base, twin)
            test_id, pid = str(uuid.uuid4()), server_target.fresh_id()
            older, newer = run_body(test_id, day=20), run_body(test_id, day=21)
            for run in (older, newer, incomplete(test_id)):  # the incomplete newest: never
                body = {**run, "import": True, "pushed_at": "2020-01-02T03:04:05Z"}
                ok(api.call("POST", f"projects/{pid}/runs", role="importer", json_body=body))

            def pointer(api=api, pid=pid, test_id=test_id) -> dict | None:
                board = ok(api.call("GET", f"projects/{pid}/boards/SN-1", query=board_query()))
                return {t["test_id"]: t.get("pointer") for t in board["tests"]}.get(test_id)

            assert (pointer() or {}).get("run_id") == newer["run_id"], pointer()
            for gone, then in ((newer, older["run_id"]), (older, None)):
                delete = {"import": True, "machine_id": MACHINE, "reason": "moved in",
                          "deleted_at": "2020-02-02T02:02:02Z", "deleted_was_current": True}  # fmt: skip
                path = f"projects/{pid}/runs/{gone['run_id']}/delete"
                ok(api.call("POST", path, role="importer", json_body=delete))
                now = pointer()
                assert (now or {}).get("run_id") == then, (
                    "without E6 an imported delete hands the pointer on as a bench's does",
                    gone["run_id"],
                    now,
                )


# ---- §6.8 U1, the server's users ---------------------------------------------------------------


@pytest.fixture
def api(tmp_path, twin):
    with server_target.serve(tmp_path / "server", TOKENS) as (base, running):
        api = Api(base, twin)
        api.running = running
        try:
            yield api
        finally:
            api.close()
        if running is not None:
            assert not running.internal_errors, running.internal_errors


@rule("U1.users")
def test_u1_lists_the_active_users_by_display_name_then_name(api):
    server_target.needs(api.base_url, "U1")
    users = ok(api.call("GET", "users"))["users"]
    for user in users:
        assert isinstance(user.get("name"), str) and user["name"], user
        assert isinstance(user.get("display_name"), str) and user["display_name"], user
    order = [(fold(u["display_name"]), fold(u["name"])) for u in users]
    assert order == sorted(order), [u["display_name"] for u in users]
    names = {fold(u["name"]) for u in users}
    missing = [u["name"] for u in server_target.KIT_USERS if fold(u["name"]) not in names]
    assert not missing, (
        f"the kit's runs and releases name these users, which U1 does not list: {missing} "
        "(make them active on the test instance: the kit's README)"
    )
    refused(api.call("GET", "users", query="kind=x"), 400, "invalid", "bad_request")


def _project(api: Api):
    pid, tid = server_target.fresh_id(), str(uuid.uuid4())
    body = project_body(pid, tid)
    ok(api.call("POST", "projects", json_body=body))
    files = {path: f["version"] for path, f in body["files"].items() if path.startswith("tests/")}
    return pid, tid, files


@rule("U1.operator-known", "4.3.409-invalid")
def test_with_u1_a_run_or_a_release_naming_none_of_its_users_is_409_unknown_operator(api):
    """A server listing U1: a run's ``operator`` and a release's ``released_by`` that are no
    user's ``name`` (compared by case folding) are ``409 unknown_operator``, kind ``invalid``,
    nothing written; a user's name in another case is taken and kept as sent; none at all is
    taken."""
    server_target.needs(api.base_url, "U1")
    pid, tid, files = _project(api)
    nobody = f"nobody-{server_target.fresh_suffix()}"
    run = run_body(tid, operator=nobody)
    reply = api.call("POST", f"projects/{pid}/runs", json_body=run)
    refused(reply, 409, "invalid", "unknown_operator")
    refused(api.call("GET", f"projects/{pid}/runs/{run['run_id']}"), 404, "not_found")
    shouted = run_body(tid, day=27, operator="ALICE")  # the kit's user alice
    assert ok(api.call("POST", f"projects/{pid}/runs", json_body=shouted))["status"] == "saved"
    read = ok(api.call("GET", f"projects/{pid}/runs/{shouted['run_id']}"))
    assert read["operator"] == "ALICE", read
    unnamed = run_body(tid, day=28)
    assert ok(api.call("POST", f"projects/{pid}/runs", json_body=unnamed))["status"] == "saved"

    release = {"format": "text", "fingerprint": "a" * 64, "content": "b" * 64, "files": files}
    path = f"projects/{pid}/releases/{tid}"
    reply = api.call("POST", path, json_body={**release, "released_by": nobody})
    refused(reply, 409, "invalid", "unknown_operator")
    made = ok(api.call("POST", path, json_body={**release, "released_by": "Kit"}))
    assert (made["version"], made["released_by"]) == (1, "Kit"), made  # nothing made before
    again = ok(api.call("POST", path, json_body={**release, "content": "c" * 64}))
    assert again["version"] == 2, again


@rule("U1.operator-exempt")
def test_with_u1_an_import_keeps_a_name_the_server_does_not_list(api):
    """Not checked: an import (a name recorded long ago, §6.7): a run whose ``operator`` and a
    release whose ``released_by`` are no user's are taken from the importer's login, as sent."""
    server_target.needs(api.base_url, "U1")
    importer = server_target.token("importer")
    pid, tid, _files = _project(api)
    nobody = f"nobody-{server_target.fresh_suffix()}"
    imported = {**run_body(tid, operator=nobody), "import": True}
    reply = api.call("POST", f"projects/{pid}/runs", role=None, token=importer,
                     json_body=imported)  # fmt: skip
    assert ok(reply)["status"] == "saved"
    assert ok(api.call("GET", f"projects/{pid}/runs/{imported['run_id']}"))["operator"] == nobody
    gone_id = str(uuid.uuid4())  # a test no longer held: the release carries its documents
    release = {"import": True, "version": 1, "test": "Gone", "released_by": nobody,
               "released_at": "2025-01-02T03:04:05Z", "format": "text", "fingerprint": "a" * 64,
               "content": "b" * 64, "documents": {"procedure_text.md": text_file("# Gone\n")}}  # fmt: skip
    reply = api.call("POST", f"projects/{pid}/releases/{gone_id}", role=None, token=importer,
                     json_body=release)  # fmt: skip
    assert ok(reply)["released_by"] == nobody, reply


@rule("U1.operator-exempt", "U1.operator-known")
def test_with_u1_a_replay_is_answered_after_its_operator_left(api):
    """Not checked: a replay of a stored run, once its operator is no user any more; a new run
    under that name is refused."""
    server_target.local_only("it changes the server's users")
    pid, tid, _files = _project(api)
    run = run_body(tid, day=27, operator="carol")
    ok(api.call("POST", f"projects/{pid}/runs", json_body=run))
    state = api.running.state
    state.people = [u for u in state.people if u["name"] != "carol"]  # carol left
    assert ok(api.call("POST", f"projects/{pid}/runs", json_body=run))["status"] == "already"
    later = run_body(tid, day=28, operator="carol")
    refused(api.call("POST", f"projects/{pid}/runs", json_body=later), 409, "invalid",
            "unknown_operator")  # fmt: skip


# ---- §3.3 Boards the server registers itself ---------------------------------------------------


@pytest.mark.boards_aware
@pytest.mark.boards_unregistered  # the kit registers none of its boards
@rule("3.3.info-boards-registered", "E4.registered-listed", "E1.unknown-board",
      "E10.unknown-board", "E15.unknown-board", "4.3.409-invalid")  # fmt: skip
def test_a_server_registering_its_boards_lists_them_before_any_run_and_refuses_others(
    tmp_path, twin
):
    """``boards_registered: true``: E4 lists exactly the boards registered, before their first
    run; a run, a report and a board entry on a board it does not list are each ``409
    unknown_board``, kind ``invalid``, nothing written (E4 lists the same boards after); on a
    board it lists each is taken. ``false``: any serial run becomes a board.
    Against the reference server, once as the suites start it (``false``), once registering a
    board; against yours, the project ``PROCEDO_KIT_BOARDS_PROJECT`` names when it registers
    its boards."""
    local_pid = server_target.fresh_id()
    board = {"serial": "SN-REG", "part_number": "PN-1", "board_revision": "A"}
    with ExitStack() as stack:
        for base in servers(stack, tmp_path, registered={"boards": {local_pid: [board]}}):
            api = api_of(stack, base, twin)
            flag = ok(api.call("GET", "info"))["boards_registered"]
            assert isinstance(flag, bool), flag
            tid = str(uuid.uuid4())
            if not flag:
                pid = server_target.fresh_id()
                run = run_body(tid, serial=f"SN-{server_target.fresh_suffix()}")
                ok(api.call("POST", f"projects/{pid}/runs", json_body=run))
                serials = [b["serial"] for b in ok(api.call("GET", f"projects/{pid}/boards"))[
                    "boards"
                ]]  # fmt: skip
                assert serials == [run["serial"]], serials
                continue
            pid = local_pid
            if server_target.external():
                pid = os.environ.get(BOARDS_PROJECT_ENV, "").strip()
                if not pid:
                    pytest.skip(f"set {BOARDS_PROJECT_ENV} to a project holding a board "
                                "registered on your server")  # fmt: skip
            boards = ok(api.call("GET", f"projects/{pid}/boards"))["boards"]
            assert boards, f"E4 lists no board of {pid}, whose boards are registered"
            if not server_target.external():
                assert [b["serial"] for b in boards] == ["SN-REG"], boards  # no run yet
            held = boards[0]
            unknown = run_body(tid, serial=f"SN-NONE-{server_target.fresh_suffix()}")
            unknown.update(part_number=held.get("part_number", "PN-1"),
                           board_revision=held.get("board_revision", "A"))  # fmt: skip
            reply = api.call("POST", f"projects/{pid}/runs", json_body=unknown)
            refused(reply, 409, "invalid", "unknown_board")
            gone = api.call("GET", f"projects/{pid}/runs/{unknown['run_id']}")
            refused(gone, 404, "not_found")
            listed = [fold(b["serial"]) for b in ok(api.call("GET", f"projects/{pid}/boards"))[
                "boards"
            ]]  # fmt: skip
            assert fold(unknown["serial"]) not in listed, listed
            # A report and a board entry on that board: refused alike, nothing listed after.
            names = {k: unknown[k] for k in ("part_number", "board_revision")}
            _board_writes(api, base, pid, unknown["serial"], names, refused_as="unknown_board")
            again = [fold(b["serial"]) for b in ok(api.call("GET", f"projects/{pid}/boards"))[
                "boards"
            ]]  # fmt: skip
            assert again == listed, ("E4 lists exactly the registered boards", listed, again)
            on_it = run_body(tid, serial=held["serial"])
            for name in ("part_number", "board_revision"):
                if name in held:
                    on_it[name] = held[name]
                else:
                    on_it.pop(name, None)
            saved = ok(api.call("POST", f"projects/{pid}/runs", json_body=on_it))
            assert saved["status"] == "saved", saved
            # A board E4 lists is registered: a report and an entry on it are taken too.
            names = {k: held.get(k) for k in ("part_number", "board_revision")}
            _board_writes(api, base, pid, held["serial"], names, refused_as=None)


def _draft(serial: str, names: dict) -> dict:
    """A draft (E10) on the board *serial* with *names*: no file, so no storage is needed."""
    content = {"blocks": [f"kit {server_target.fresh_suffix()}"]}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    draft = {"format_version": 2, "kind": "draft", "serial": serial,
             "name": f"kit-{server_target.fresh_suffix()}.html", "machine_id": MACHINE,
             "updated_at": "2026-10-26T08:00:00Z", "content": content,
             "sha256": sha(text.encode("utf-8")), "replaces": None}  # fmt: skip
    draft.update({k: v for k, v in names.items() if v is not None})
    return draft


def _api_store(api: Api) -> int | None:
    """The ``store_id`` of the server's ``api`` storage (S1); None when it lists none."""
    storages = ok(api.call("GET", "storages"))["storages"]
    return next((s["store_id"] for s in storages if s["backend"] == "api"), None)


def _report_push(
    api: Api,
    pid: str,
    store: int,
    serial: str,
    names: dict,
    *,
    data: bytes | None = None,
    name: str | None = None,
    **login: Any,
):
    """E10: a report on the board *serial* with *names*, its file a part of the request; an
    import when *login* gives a token (the importer's)."""
    data = data or f"<html>kit {server_target.fresh_suffix()}</html>".encode()
    report = report_body(store, data, serial=serial, name=name or f"kit-{sha(data)[:8]}.html")
    for key in ("part_number", "board_revision"):
        report.pop(key, None)
    report.update({k: v for k, v in names.items() if v is not None})
    if login.get("token"):
        report["import"] = True
    return api.call("POST", f"projects/{pid}/artifacts", json_body=report,
                    parts={f"sha256:{sha(data)}": data}, **login)  # fmt: skip


def _entries_path(pid: str, serial: str, names: dict) -> tuple[str, str]:
    """E15's path and query for the board *serial* with *names*."""
    query = board_query(names.get("part_number") or "", names.get("board_revision") or "")
    return f"projects/{pid}/boards/{seg(serial)}/entries", query


def _board_writes(
    api: Api, base: str, pid: str, serial: str, names: dict, *, refused_as: str | None
) -> None:
    """A bench's draft (E10) and board entry (E15, when offered) on the board *serial* with
    *names*: refused ``409`` *refused_as*, kind ``invalid``; or taken."""
    reply = api.call("POST", f"projects/{pid}/artifacts", json_body=_draft(serial, names))
    if refused_as:
        refused(reply, 409, "invalid", refused_as)
    else:
        assert ok(reply)["status"] == "saved", reply
    if server_target.offers(base, "E15"):
        path, query = _entries_path(pid, serial, names)
        reply = api.call("POST", path, query=query, json_body={"entries": [board_entry()]})
        if refused_as:
            refused(reply, 409, "invalid", refused_as)
        else:
            assert [r["status"] for r in ok(reply)["results"]] == ["saved"], reply


@pytest.mark.boards_aware
@pytest.mark.boards_unregistered  # the kit registers none of its boards
@rule("E1.unknown-board-exempt", "3.3.listed-taken", "3.3.import-any-call", "3.3.import-registers")
def test_a_server_registering_its_boards_takes_an_import_and_a_replay_on_any_board(tmp_path, twin):
    """``boards_registered: true``, not refused for its board: an import (§6.7) on a board E4
    does not list (a run, E1; a draft and a report, E10; an entry, E15), whose board E4 lists
    from then on and takes a bench's run (registered); and, against the reference server (no
    call of the contract unregisters a board), a run and a report already stored sent again once
    their board is no longer registered (``already``)."""
    local_pid = server_target.fresh_id()
    board = {"serial": "SN-REG", "part_number": "PN-1", "board_revision": "A"}
    limits = {} if server_target.external() else {"boards": {local_pid: [board]}}
    with server_target.serve(tmp_path / "server", TOKENS, **limits) as (base, running):
        api = Api(base, twin)
        try:
            if not ok(api.call("GET", "info"))["boards_registered"]:
                server_target.skip("the server does not register its boards (boards_registered)")
            pid = local_pid
            if server_target.external():
                pid = os.environ.get(BOARDS_PROJECT_ENV, "").strip()
                if not pid:
                    server_target.skip(f"set {BOARDS_PROJECT_ENV} to a project holding a board "
                                       "registered on your server")  # fmt: skip
            tid = str(uuid.uuid4())
            serial = f"SN-MOVED-{server_target.fresh_suffix()}"
            imported = {**run_body(tid, serial=serial), "import": True}
            reply = api.call("POST", f"projects/{pid}/runs", role=None,
                             token=server_target.token("importer"), json_body=imported)  # fmt: skip
            assert ok(reply)["status"] == "saved", reply
            listed = [fold(b["serial"]) for b in ok(api.call("GET", f"projects/{pid}/boards"))[
                "boards"
            ]]  # fmt: skip
            assert fold(serial) in listed, listed
            # Registered so: a bench's run on that board is taken, never unknown_board.
            bench = run_body(tid, serial=serial, day=25)
            assert (
                ok(api.call("POST", f"projects/{pid}/runs", json_body=bench))["status"] == "saved"
            )
            # An imported draft (E10) and an imported entry (E15), each on a board of its own:
            # taken, each board listed from then on and taking a bench's run.
            importer = {"role": None, "token": server_target.token("importer")}
            names = {"part_number": "PN-1", "board_revision": "A"}
            serials = [f"SN-DRAFT-{server_target.fresh_suffix()}"]
            draft = {**_draft(serials[0], names), "import": True}
            reply = api.call("POST", f"projects/{pid}/artifacts", json_body=draft, **importer)
            assert ok(reply)["status"] == "saved", reply
            if server_target.offers(base, "E15"):
                serials.append(f"SN-ENTRY-{server_target.fresh_suffix()}")
                path, query = _entries_path(pid, serials[-1], names)
                body = {"import": True, "entries": [board_entry()]}
                reply = api.call("POST", path, query=query, json_body=body, **importer)
                assert [r["status"] for r in ok(reply)["results"]] == ["saved"], reply
            store = _api_store(api)  # an imported report (E10) too, its file a part
            if store is None:
                warnings.warn(
                    "the server lists no api storage: the imported report (E10) is not checked, "
                    "only the imported draft",
                    stacklevel=1,
                )
            else:
                serials.append(f"SN-REPORT-{server_target.fresh_suffix()}")
                reply = _report_push(api, pid, store, serials[-1], names, **importer)
                assert ok(reply)["status"] == "saved", reply
            listed = [fold(b["serial"]) for b in ok(api.call("GET", f"projects/{pid}/boards"))[
                "boards"
            ]]  # fmt: skip
            for day, each in enumerate(serials, start=20):
                assert fold(each) in listed, ("an import's board not listed", each, listed)
                on_it = run_body(tid, serial=each, day=day)
                reply = api.call("POST", f"projects/{pid}/runs", json_body=on_it)
                assert ok(reply)["status"] == "saved", reply
            if running is None:
                return
            # Against the reference server only: no call of the contract unregisters a board.
            run = run_body(tid, serial="SN-REG", day=27)
            assert ok(api.call("POST", f"projects/{pid}/runs", json_body=run))["status"] == "saved"
            store = _api_store(api)
            assert store is not None, "the reference server lists an api storage"
            report_on = {"store": store, "serial": "SN-REG", "names": names}
            data = f"<html>kit replay {server_target.fresh_suffix()}</html>".encode()
            first = ok(_report_push(api, pid, data=data, **report_on))
            assert first["status"] == "saved", first
            # Its administrator takes the board off the server's registered boards: E4 no more
            # lists it, and a replay of the run and of the report is still not refused for it.
            running.state.registered_boards[pid].clear()
            assert ok(api.call("GET", f"projects/{pid}/boards"))["boards"] == []
            again = ok(api.call("POST", f"projects/{pid}/runs", json_body=run))
            assert again["status"] == "already", again
            again = ok(_report_push(api, pid, data=data, name=first["artifact"]["name"],
                                    **report_on))  # fmt: skip
            assert again["status"] == "already", again
        finally:
            api.close()
