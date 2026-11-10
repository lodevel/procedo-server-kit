"""The server the conformance suites over the JSON API talk to: the reference server, started
for each case on 127.0.0.1 (the default), or a server the environment names, which the server
conformance kit (``scripts/build_server_kit.py``) runs the same suites against.

* ``PROCEDO_KIT_SERVER``: the server's base address (``https://host/prefix/``, with or without
  the trailing slash). Set, no reference server starts and every case talks to that server.
* ``PROCEDO_KIT_BENCH_TOKEN``: a token whose login holds the roles ``bench`` and ``publisher``.
* ``PROCEDO_KIT_MANAGER_TOKEN``: a token whose login holds the role ``manager``.
* ``PROCEDO_KIT_IMPORTER_TOKEN`` (optional): a token whose login holds only the role
  ``importer`` (contract §6.7). Unset, the cases moving data in are skipped, saying so.
* ``PROCEDO_KIT_MOVER_TOKEN`` (optional): a token whose login holds the roles ``bench`` and
  ``importer``: the login the app moves a project's data in with (contract §3.5). Unset, the
  case telling it apart is skipped, saying so.
* ``PROCEDO_KIT_BENCH_ONLY_TOKEN`` (optional): a token whose login holds only the role
  ``bench``, without ``publisher``. Unset, the cases telling the two apart are skipped.
* ``PROCEDO_KIT_BOARDS_PROJECT`` (only for a server registering its boards, contract §3.3): a
  project holding a board registered on it (``test_server_contract_calls``).
* ``PROCEDO_KIT_CA_BUNDLE`` (optional): a CA bundle file for an ``https`` server whose
  certificate a private CA signed.
* ``PROCEDO_KIT_SLOW`` (optional): ``1`` runs the cases that wait on the server (a body that
  stalls until the server gives up on it, ``408``); ``PROCEDO_KIT_SLOW_WAIT``, the seconds such a
  case waits for the server's answer (300 by default). Unset, they are skipped, saying so.
* ``PROCEDO_KIT_LOAD`` (optional): ``1`` runs the load test (``test_server_load.py``) with
  ``PROCEDO_KIT_LOAD_BENCHES`` benches at once (20 by default), each a few rounds, or for
  ``PROCEDO_KIT_LOAD_MINUTES`` minutes (a soak run); either of the two switches it on too.

Nothing here empties a server (the contract has no such call): a case that talks to a server
the environment names writes under ids and names of its own (:func:`fresh_id`), and reads back
what it wrote among whatever else the server holds. A case that needs what only a fresh
reference server gives (its limits set small, an empty server) is skipped there, saying why
(:func:`local_only`).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from reference_server import start, stop

from procedo.core.evidence_http import ApiEndpoint

SERVER_ENV = "PROCEDO_KIT_SERVER"
CA_BUNDLE_ENV = "PROCEDO_KIT_CA_BUNDLE"
SLOW_ENV = "PROCEDO_KIT_SLOW"
SLOW_WAIT_ENV = "PROCEDO_KIT_SLOW_WAIT"
LOAD_ENV = "PROCEDO_KIT_LOAD"
LOAD_BENCHES_ENV = "PROCEDO_KIT_LOAD_BENCHES"
LOAD_MINUTES_ENV = "PROCEDO_KIT_LOAD_MINUTES"
#: The repository's own: the optional calls the reference servers the cases start offer
#: (``none``, or ids joined by commas), the kit's up-front skips applied as against a server
#: offering only those (``tests/test_contract_faults.py``). Unset: every call.
REFERENCE_CALLS_ENV = "PROCEDO_KIT_REFERENCE_CALLS"
#: role -> (the variable naming its token on a server the environment names, the token the
#: suites give the reference server for it)
TOKENS = {
    "bench": ("PROCEDO_KIT_BENCH_TOKEN", "t-bench"),
    "manager": ("PROCEDO_KIT_MANAGER_TOKEN", "t-manager"),
    "importer": ("PROCEDO_KIT_IMPORTER_TOKEN", "t-importer"),
    "mover": ("PROCEDO_KIT_MOVER_TOKEN", "t-mover"),
    "bench_only": ("PROCEDO_KIT_BENCH_ONLY_TOKEN", "t-bench-only"),
}
#: role -> what its login must hold, for the roles a server under test may leave unset: the
#: cases needing one are skipped there, saying which variable to set.
OPTIONAL = {
    "importer": "only the role importer",
    "mover": "the roles bench and importer",
    "bench_only": "only the role bench (no publisher)",
}
#: The server's users (U1, contract §6.8) the kit's runs and releases name as their ``operator``
#: and ``released_by``: a server listing U1 refuses any other name, so its test instance holds
#: these active (the kit's README lists them). The reference servers the suites start list them.
KIT_USERS = (
    {"name": "al", "display_name": "Albert Roy"},
    {"name": "alice", "display_name": "Alice Martin"},
    {"name": "ann", "display_name": "Ann Lee"},
    {"name": "bob", "display_name": "Robert Durand"},
    {"name": "carol", "display_name": "Carol Petit"},
    {"name": "jdoe", "display_name": "Jane Doe"},
    {"name": "kit", "display_name": "Conformance Kit"},
    {"name": "zoé", "display_name": "Zoé Blanc"},
)
#: Every login the suites give the reference server, by its token.
LOCAL_LOGINS = {
    "t-bench": {"name": "bench", "roles": ["bench", "publisher"]},
    "t-manager": {"name": "manager-login", "roles": ["manager"]},
    "t-importer": {"name": "importer-login", "roles": ["importer"]},
    "t-mover": {"name": "mover-login", "roles": ["bench", "importer"]},
    "t-bench-only": {"name": "bench-only-login", "roles": ["bench"]},
}


def external() -> str:
    """The base address of the server the environment names; "" for the reference server."""
    return os.environ.get(SERVER_ENV, "").strip()


def token(role: str) -> str:
    """The token of a login holding *role* (``bench``, ``manager``, ``importer``,
    ``bench_only``) on the server under test. An optional role left unset skips the case."""
    variable, local = TOKENS[role]
    if not external():
        return local
    value = os.environ.get(variable, "").strip()
    if not value and role in OPTIONAL:
        skip(f"{variable} is not set: this case needs a login holding {OPTIONAL[role]}")
    if not value:
        pytest.fail(f"{SERVER_ENV} is set but {variable} is not", pytrace=False)
    return value


def endpoint(base_url: str) -> ApiEndpoint:
    """How the app's client reaches *base_url*: ``https`` checked against the system's CAs (or
    the bundle the environment names), plain ``http`` only with the client's "insecure"
    setting, as on a loopback test server."""
    if base_url.lower().startswith("https://"):
        return ApiEndpoint(base_url=base_url, ca_bundle=os.environ.get(CA_BUNDLE_ENV, ""))
    return ApiEndpoint(base_url=base_url, tls=False, insecure=True)


@contextmanager
def serve(root: Path, tokens: dict, **limits: Any) -> Iterator[tuple[str, Any]]:
    """The server a case talks to, as ``(base_url, the reference server or None)``: a new
    reference server under *root* holding *tokens* (and *limits*: its limits, the optional
    calls it offers), stopped afterwards; or the server the environment names, which a case
    needing *limits* cannot use. The reference server offers every optional call, U1 listing
    :data:`KIT_USERS`."""
    base_url = external()
    if base_url:
        if limits:
            local_only(f"it needs the server's own limits set to {limits}")
        yield base_url, None
        return
    chosen = reference_calls()
    defaults = {"users": list(KIT_USERS), **({} if chosen is None else {"calls": sorted(chosen)})}
    base_url, running = start(root, tokens, **{**defaults, **limits})
    try:
        yield base_url, running
    finally:
        stop(running)


def reference_calls() -> frozenset[str] | None:
    """The optional calls :data:`REFERENCE_CALLS_ENV` has the reference servers offer; None
    when unset (every call)."""
    value = os.environ.get(REFERENCE_CALLS_ENV, "").strip()
    if not value:
        return None
    return frozenset(c.strip() for c in value.split(",") if c.strip() and c.strip() != "none")


def local_only(why: str) -> None:
    """Skip this case on a server the environment names, saying *why*."""
    if external():
        skip(f"runs only against the reference server: {why}")


def fresh_id() -> str:
    """A new project id, a lower-case UUID: no other case, no earlier run, holds it."""
    return str(uuid.uuid4())


def fresh_suffix() -> str:
    """Eight hex digits that make a name this case's own on a shared server."""
    return uuid.uuid4().hex[:8]


def info(base_url: str) -> dict:
    """``GET /api/v1/info`` with the bench's token: the server's name, limits and the login."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/info",
        headers={"Authorization": f"Bearer {token('bench')}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30, context=tls_context()) as answer:
        return json.loads(answer.read())


#: ``/info`` of each server asked, by its base address.
_INFO: dict[str, dict] = {}


def _info_of(base_url: str) -> dict:
    """``/info`` of the server at *base_url*, asked once of a server the environment names (a
    reference server started for a case is asked each time: another may take its port later)."""
    key = base_url.rstrip("/")
    if key not in _INFO or not external():
        _INFO[key] = info(base_url)
    return _INFO[key]


def offered(base_url: str) -> frozenset[str]:
    """The optional calls the server at *base_url* offers: ``/info``'s ``calls`` (contract
    §3.3, §6.1)."""
    calls = _info_of(base_url).get("calls")
    listed = calls if isinstance(calls, list) else []
    return frozenset(c for c in listed if isinstance(c, str))


def registers_boards(base_url: str) -> bool:
    """Whether the server at *base_url* registers its boards itself (``/info``'s
    ``boards_registered``, contract §3.3): a run on a board it does not list is refused."""
    return _info_of(base_url).get("boards_registered") is True


def seeds_boards() -> bool:
    """Whether the kit registers its own boards on the server the environment names, which
    registers its boards (:class:`BoardSeeds`): the importer's token is set."""
    base = external()
    importer = os.environ.get(TOKENS["importer"][0], "").strip()
    return bool(base and importer and registers_boards(base))


def pushes_runs(base_url: str) -> None:
    """Skip this case, a bench pushing runs on boards of its own, on a server registering its
    boards the kit cannot register its boards on (no importer's token)."""
    if external() and registers_boards(base_url) and not seeds_boards():
        skip(missing_for(["E1"], offered(base_url), True))


def needs(base_url: str, *calls: str) -> None:
    """Skip this case when the server does not offer every one of *calls* (optional calls,
    contract §6.1), naming those it does not list."""
    _optional(calls)
    missing = [call for call in calls if call not in offered(base_url)]
    if missing:
        skip(f"the server does not offer {', '.join(missing)} (not in /info's calls)")


def offers(base_url: str, *calls: str) -> bool:
    """Whether the server offers every one of *calls* (optional calls, contract §6.1): a case
    marked ``offer_aware`` makes them only then."""
    _optional(calls)
    return all(call in offered(base_url) for call in calls)


def _optional(calls: tuple[str, ...]) -> None:
    """*calls* are optional calls: a core call is never asked about (every server takes it)."""
    from reference_server.offer import OPTIONAL

    core = [call for call in calls if call not in OPTIONAL]
    assert not core, f"{core}: not optional calls of the contract (§6.1)"


@contextmanager
def unrecorded() -> Iterator[None]:
    """The optional calls made inside, which the case makes only when the server offers them
    (:func:`offers`), are not recorded for it (:class:`CallRecorder`): it runs on any server.
    A bench's run made inside still is (a server registering its boards may refuse it)."""
    from reference_server import offer

    quiet, offer.QUIET = offer.QUIET, True
    try:
        yield
    finally:
        offer.QUIET = quiet


def offer_aware(fixture_function):
    """Mark a fixture making an optional call only when the server lists it (:func:`offers`):
    its calls are not counted for the cases using it (:class:`CallRecorder`)."""
    fixture_function.offer_aware = True
    return fixture_function


def skip(reason: str) -> None:
    """Skip the case now running, the skip pointing at the case (not at this module)."""
    raise pytest.skip.Exception(reason, _use_item_location=True)


# ---- The optional calls each case makes --------------------------------------------------------

#: Each case of the kit and the optional calls it makes (``reference_server.offer.OPTIONAL``),
#: ``E1`` beside them when it writes on a board as a bench (a run, a report, an entry: what a
#: server registering its boards refuses on a board of the case's own): recorded running the kit
#: against the reference server offering every call (``scripts/kit_calls.py --write``); ``tests/test_server_kit.py`` fails when it is
#: stale. Beside this module, in the kit as in the repository.
CALLS_FILE = Path(__file__).resolve().parent / "kit_calls.json"
#: Set to a folder, the reference servers of this process note the calls each case makes and
#: write them there when pytest ends (:class:`CallRecorder`).
RECORD_ENV = "PROCEDO_KIT_RECORD_CALLS"


def case_key(nodeid: str) -> str:
    """A case's name in :data:`CALLS_FILE`: its node id from its file's name on (``suites/`` in
    the kit, ``tests/`` in the repository)."""
    path, sep, rest = nodeid.partition("::")
    return path.rsplit("/", 1)[-1] + sep + rest


def case_calls() -> dict[str, list[str]]:
    """:data:`CALLS_FILE`: each case and the calls it makes beyond the core ones."""
    return json.loads(CALLS_FILE.read_text(encoding="utf-8"))["cases"]


def missing_for(needed: list[str], listed: frozenset[str], boards: bool) -> str:
    """Why a case making *needed* cannot run against a server listing *listed* (registering its
    boards when *boards*); ``""`` when it can."""
    from reference_server.offer import OPTIONAL

    reasons = []
    absent = [call for call in OPTIONAL if call in needed and call not in listed]  # §6.1's order
    if absent:
        reasons.append(
            f"the server does not offer {', '.join(absent)} (not in /info's calls), which this "
            "case makes"
        )
    if boards and "E1" in needed:
        reasons.append(
            "the server registers its boards itself (/info's boards_registered): this case "
            f"pushes runs on boards of its own, which the kit registers when {TOKENS['importer'][0]} "
            "is set (it imports a run on each)"
        )
    return "; ".join(reasons)


class OfferedCalls:
    """A pytest plugin for the kit run against a server the environment names (contract §6.1):
    before any case runs, each case making an optional call the server does not list in
    ``/info`` (:data:`CALLS_FILE`) is skipped, naming the call, and so is each case pushing a
    bench's run on a board of its own when the server registers its boards (§3.3). Decided up
    front, never from what a case met: a case that runs and fails, fails.

    A case handling the server's offer itself (marker ``offer_aware``: it reads ``/info`` and
    makes an optional call only when the server lists it) is never skipped for its optional
    calls; one handling ``boards_registered`` itself (marker ``boards_aware``) never for its
    runs."""

    def pytest_configure(self, config) -> None:
        config.addinivalue_line(
            "markers",
            "offer_aware: a case reading /info's calls itself, never skipped for them up front",
        )
        config.addinivalue_line(
            "markers",
            "boards_aware: a case reading /info's boards_registered itself, never skipped for it",
        )
        config.addinivalue_line(
            "markers",
            "boards_unregistered: a case pushing on boards it leaves unregistered (§3.3)",
        )

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items) -> None:
        if not items:
            return
        base = external()
        if base:
            listed, boards = offered(base), registers_boards(base)
        elif reference_calls() is not None:  # the reference servers the cases start offer these
            listed, boards = reference_calls(), False
        else:
            return
        seeding = boards and seeds_boards()
        if seeding:
            BoardSeeds.install(base)
        calls = case_calls()
        for item in items:
            needed = calls.get(case_key(item.nodeid), [])
            if item.get_closest_marker("offer_aware"):  # its optional calls: only when listed
                needed = [call for call in needed if call == "E1"]
            if item.get_closest_marker("boards_aware"):  # it reads boards_registered itself
                needed = [call for call in needed if call != "E1"]
            reason = missing_for(needed, listed, boards and not seeding)
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item) -> None:
        # the two cases of §3.3 push on the boards they mean to leave unregistered
        BoardSeeds.paused = item.get_closest_marker("boards_unregistered") is not None


#: The test id of the runs the kit imports to register its boards on a server registering its
#: boards (:class:`BoardSeeds`). Load-bearing: the cases leave runs of this id out of what they
#: read (:func:`kit_tests`, :func:`kit_runs`); all ``f``, it sorts after every other test id (E5
#: lists tests by id), and its run ids start ``2000-01-01`` (E2 lists newest first), so leaving
#: it out never moves another row.
SEED_TEST_ID = "ffffffff-ffff-4fff-bfff-ffffffffffff"
SEED_TEST = "kit board registration"
SEED_MACHINE = "0000feed"
SEED_STARTED = "2000-01-01T00:00:00Z"
_WRITES_PATH = re.compile(
    r"/api/v1/projects/([0-9a-f-]{36})/(runs|artifacts|boards/[^/]+/entries)$"
)
#: The most a pushed body is decompressed to, to read the run it carries (bytes).
MAX_INFLATED = 64 * 1024 * 1024
#: How long a case waits for another thread registering the same board (seconds).
SEED_WAIT = 120.0


class _Seeding:
    """One board being registered: done when :attr:`event` is set, failed with :attr:`error`,
    else registered at :attr:`done_at` (``time.monotonic``)."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.error = ""
        self.done_at = 0.0


class BoardSeeds:
    """The kit's boards registered on a server registering its boards (contract §3.3), the
    importer's token given (``PROCEDO_KIT_IMPORTER_TOKEN``).

    Hooked into ``http.client``, which every request of the kit goes through. A bench's run
    (E1 without ``"import": true``, whatever the login) that the server refuses ``409
    unknown_board`` (the error envelope's code) is the cue: the kit imports a run on that
    board, exactly as the refused run names it (an import is never refused for its board, and
    its board becomes one of the server's), then sends the refused request again and hands the
    case that answer. So only a run the server refused for its board gets its board
    registered, under the case's own spelling, before anything else is stored on it. When the
    server offers E11 and E13, the imported run is deleted and purged at once with the
    manager's login.

    The case fails, saying why, when: the import, its delete or its purge is refused; the run
    sent again is refused ``unknown_board`` again, or a later run on a board the kit registered
    is (the import's board did not become the server's); the run sent again answers
    ``already`` (the refused push stored it: a refusal writes nothing); sending it again gets no
    answer. Two threads pushing a first run on one board: one registers it, the other waits,
    then sends its run again too."""

    base = ""
    paused = False
    seen: dict[tuple, _Seeding] = {}
    lock = threading.Lock()
    originals: tuple = ()

    @classmethod
    def install(cls, base: str) -> None:
        import http.client

        cls.base = base
        connection = http.client.HTTPConnection
        if getattr(connection, "_procedo_kit_seeds", False):
            return
        send, answer = connection.request, connection.getresponse
        cls.originals = (send, answer)

        def request(conn, method, url, body=None, headers=None, **kwargs):
            headers = headers or {}
            conn._procedo_kit_sent = None
            if method == "POST" and isinstance(body, bytes | bytearray | str):
                board = cls.board_of(url, body, headers)
                if board is not None:
                    sent = (method, url, body, headers)
                    conn._procedo_kit_sent = (board, sent, kwargs, time.monotonic())
            return send(conn, method, url, body, headers, **kwargs)

        def getresponse(conn):
            reply = answer(conn)
            sent, conn._procedo_kit_sent = getattr(conn, "_procedo_kit_sent", None), None
            if sent is None or reply.status != 409:
                return reply
            data = reply.read()
            if _error_code(_answer_of(reply, data)) != "unknown_board":
                return _replayed(reply, data)
            (project, board), request_args, kwargs, sent_at = sent
            cls.register(project, board, sent_at)
            try:
                send(conn, *request_args, **kwargs)
                again = answer(conn)
                data = again.read()
            except (OSError, http.client.HTTPException) as exc:
                pytest.fail(
                    f"the kit registered the board {_named(board)} in project {project} and sent "
                    f"the run your server had refused 409 unknown_board again: it got no answer "
                    f"({type(exc).__name__}: {exc})",
                    pytrace=False,
                )
            found = _answer_of(again, data)
            if again.status == 409 and _error_code(found) == "unknown_board":
                pytest.fail(_still_refused(project, board), pytrace=False)
            if again.status == 200 and _replayed_answer(found):
                pytest.fail(
                    f"your server refused a bench's write on the board {_named(board)} in project "
                    f"{project} 409 unknown_board, then answered it `already` once the kit had "
                    "registered the board and sent it again: the refused push stored it (§3.3: "
                    "a refusal writes nothing)",
                    pytrace=False,
                )
            return _replayed(again, data)

        connection.request = request
        connection.getresponse = getresponse
        connection._procedo_kit_seeds = True

    @classmethod
    def uninstall(cls) -> None:
        """Undo :meth:`install` and forget every board registered (the repository's own tests
        of this class: a process running other cases)."""
        import http.client

        connection = http.client.HTTPConnection
        if getattr(connection, "_procedo_kit_seeds", False):
            connection.request, connection.getresponse = cls.originals
            connection._procedo_kit_seeds = False
        cls.seen, cls.base, cls.paused = {}, "", False

    @classmethod
    def board_of(cls, url: str, body: bytes | bytearray | str, headers: dict):
        """``(project_id, (part_number, board_revision, serial))`` of a bench's write naming a
        board (§3.3): a run (E1), a report or a draft (E10), a board entry (E15); each name as
        sent (None when absent or empty). None for any other request, an import whoever sends
        it, or while paused."""
        if cls.paused:
            return None
        path, _, query = url.partition("?")
        found = _WRITES_PATH.search(path)
        if found is None:
            return None
        sent = _run_of(body, headers)
        if sent is None or sent.get("import") is True:
            return None
        project, what = found.group(1), found.group(2)
        if what.startswith("boards/"):  # E15: the board in the path and the query
            asked: dict[str, str] = {}
            for pair in query.split("&"):  # a "+" sent is a plus, never a blank (§3.1)
                name, _, value = pair.partition("=")
                asked.setdefault(urllib.parse.unquote(name), urllib.parse.unquote(value))
            names = [asked.get(k) or None for k in ("part_number", "board_revision")]
            return project, (*names, urllib.parse.unquote(what.split("/")[1]))
        serial = sent.get("serial")
        if not isinstance(serial, str) or serial == "":  # an export names no board
            return None
        if what == "artifacts" and sent.get("kind") == "export":
            return None
        two = sent.get("format_version") != 1
        names = [sent.get(k) if two else None for k in ("part_number", "board_revision")]
        return project, (*names, serial)

    @classmethod
    def register(cls, project: str, board: tuple, sent_at: float | None = None) -> None:
        """Register *board* in *project*, once per board and process; a second caller waits
        for the first. Fails the case when: E4 lists the board already (a bench's write on a
        board E4 lists is never refused ``unknown_board``: the kit never seeds around it); the
        registration fails; E4 does not list the board once registered; or the board was
        registered before a write refused for it was sent (*sent_at*, ``time.monotonic``): the
        server still refuses a board the kit registered."""
        key = (project, *(fold_name(n) if isinstance(n, str) else n for n in board))
        with cls.lock:
            seeding = cls.seen.get(key)
            first = seeding is None
            if first:
                seeding = cls.seen[key] = _Seeding()
        if first:
            try:
                if cls.lists(project, board):
                    seeding.error = (
                        f"your server lists the board {_named(board)} in project {project} (E4), "
                        "yet refuses a bench's write on it 409 unknown_board (§3.3: a board E4 "
                        "lists is registered, and a bench's write on it is never refused so)"
                    )
                else:
                    cls.seed(project, board)
                    if not cls.lists(project, board):
                        seeding.error = (
                            f"the kit imported a run on the board {_named(board)} in project "
                            f"{project} (200), yet E4 does not list that board (§3.3: an "
                            "import's board becomes registered, and E4 lists it from then on)"
                        )
                    seeding.done_at = time.monotonic()
            except _Refused as exc:
                seeding.error = (
                    f"the kit could not register the board {_named(board)} in project "
                    f"{project} on your server (§3.3, a server registering its boards): {exc}"
                )
            finally:
                seeding.event.set()
            if seeding.error:
                pytest.fail(seeding.error, pytrace=False)
            return
        if not seeding.event.wait(SEED_WAIT):
            pytest.fail(
                f"the kit waited {SEED_WAIT:.0f} s for another case registering the board "
                f"{_named(board)} in project {project} (§3.3): it never ended",
                pytrace=False,
            )
        if seeding.error:
            pytest.fail(seeding.error, pytrace=False)
        if sent_at is not None and seeding.done_at <= sent_at:
            pytest.fail(_still_refused(project, board), pytrace=False)

    @classmethod
    def lists(cls, project: str, board: tuple) -> bool:
        """Whether E4 lists *board* in *project*, names compared as §2.4 compares them."""
        found = _get(cls.base, f"projects/{project}/boards", token("bench"), "E4, its boards")
        wanted = tuple(fold_name(n) if n else None for n in board)
        for held in found.get("boards") or []:
            names = (held.get(k) for k in ("part_number", "board_revision", "serial"))
            if tuple(fold_name(n) if isinstance(n, str) and n else None for n in names) == wanted:
                return True
        return False

    @classmethod
    def seed(cls, project: str, board: tuple) -> None:
        """Import a run on *board*, its names exactly as the case's run sends them; then, when
        the server offers E11 and E13, delete and purge it with the manager's login."""
        part, revision, serial = board
        run_id = f"2000-01-01T00-00-00Z_{uuid.uuid4().hex[:16]}"
        seed = {"format_version": 2, "run_id": run_id, "serial": serial, "test": SEED_TEST,
                "test_id": SEED_TEST_ID, "started": SEED_STARTED, "complete": False,
                "machine_id": SEED_MACHINE, "draft": False, "phases": [], "files": [],
                "import": True}  # fmt: skip
        for name, value in (("part_number", part), ("board_revision", revision)):
            if value is not None:  # sent exactly, "" included
                seed[name] = value
        _post(cls.base, f"projects/{project}/runs", seed, _importer(), "the import (E1)")
        if {"E11", "E13"} <= offered(cls.base):
            gone = {"machine_id": SEED_MACHINE, "reason": "the kit's board registration"}
            path = f"projects/{project}/runs/{run_id}/delete"
            _post(cls.base, path, gone, token("manager"), "its delete (E11, the manager)")
            purge = {"machine_id": SEED_MACHINE, "items": [{"run_id": run_id}]}
            path = f"projects/{project}/purge"
            _post(cls.base, path, purge, token("manager"), "its purge (E13, the manager)")


def register_board(base_url: str, project: str, board: tuple) -> None:
    """Register *board* (part number, revision, serial, as a run sends them) in *project* on a
    server registering its boards the kit registers its boards on; nothing elsewhere. For a
    case whose run the server takes as a bench's though it is not one (an ``"import": true``
    from a login without the importer role)."""
    if external() and registers_boards(base_url) and seeds_boards():
        BoardSeeds.register(project, board)


def _named(board: tuple) -> str:
    part, revision, serial = board
    return f"{part!r}/{revision!r}/{serial!r}"


def _still_refused(project: str, board: tuple) -> str:
    return (
        f"the kit registered the board {_named(board)} in project {project} by an import (200), "
        "and your server still refuses a bench's write on it 409 unknown_board (§3.3: an imported "
        "run's board becomes one of the server's boards)"
    )


def _replayed_answer(found: dict) -> bool:
    """Whether a write's answer says it was held already: a run's or an artifact's
    ``status`` ``already`` (E1, E10), or every entry of E15's ``results`` ``already``."""
    if found.get("status") == "already":
        return True
    results = found.get("results")
    if isinstance(results, list) and results:
        return all(isinstance(r, dict) and r.get("status") == "already" for r in results)
    return False


def _error_code(found: dict) -> object:
    """The error envelope's ``code`` (§4.1), None without one."""
    error = found.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _answer_of(reply, data: bytes) -> dict:
    """The JSON object an answer's body carries (gzipped or not); {} when it holds none."""
    try:
        if (reply.getheader("Content-Encoding") or "").strip().lower() == "gzip":
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = inflate.decompress(data, MAX_INFLATED)
        found = json.loads(data)
    except (ValueError, zlib.error):
        return {}
    return found if isinstance(found, dict) else {}


def _replayed(reply, data: bytes):
    """*reply*, whose body was read, readable again as it came."""
    import io

    reply.fp = io.BufferedReader(io.BytesIO(data))  # type: ignore[arg-type]
    reply.length = len(data)
    reply.chunked = False
    return reply


def kit_tests(results: dict) -> dict:
    """A BoardResults (E5) less the test of the run the kit imported to register the board
    (:data:`SEED_TEST_ID`), which a case reads around."""
    if isinstance(results.get("tests"), list):
        results["tests"] = [t for t in results["tests"] if t.get("test_id") != SEED_TEST_ID]
    return results


def kit_runs(found: dict) -> dict:
    """An E2 answer less the runs the kit imported to register its boards
    (:data:`SEED_TEST_ID`), which a case reads around; its ``cursor`` is kept (a page may then
    hold fewer rows than the limit: a case paging reads every page to the ``null`` cursor)."""
    if isinstance(found.get("runs"), list):
        found["runs"] = [r for r in found["runs"] if r.get("test_id") != SEED_TEST_ID]
    return found


def _importer() -> str:
    return os.environ.get(TOKENS["importer"][0], "").strip()


def _run_of(body: bytes | bytearray | str, headers: dict) -> dict | None:
    """The run a request's body carries: plain JSON, gzipped, or the ``json`` part of a
    multipart body; None when it is none of these (or inflates past :data:`MAX_INFLATED`)."""
    data = body.encode() if isinstance(body, str) else bytes(body)
    lower = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    try:
        if lower.get("content-encoding", "") == "gzip":
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = inflate.decompress(data, MAX_INFLATED)
            if inflate.unconsumed_tail:
                return None
        if lower.get("content-type", "").startswith("multipart/"):
            part = re.search(
                rb'name="json"[^\r\n]*\r\n(?:[^\r\n]+\r\n)*\r\n(.*?)\r\n--', data, re.S
            )
            if part is None:
                return None
            data = part.group(1)
        found = json.loads(data)
    except (OSError, ValueError, EOFError, zlib.error):
        return None
    return found if isinstance(found, dict) else None


class _Refused(Exception):
    """A request of the kit's board registration the server did not answer ``200``."""


def _post(base: str, path: str, body: dict, login: str, what: str) -> None:
    """A POST with the token *login*; any answer but ``200`` is :class:`_Refused`."""
    _send("POST", base, path, body, login, what)


def _get(base: str, path: str, login: str, what: str) -> dict:
    """A GET with the token *login*: its JSON answer; any answer but ``200`` is
    :class:`_Refused`."""
    return _send("GET", base, path, None, login, what)


def _send(method: str, base: str, path: str, body: dict | None, login: str, what: str) -> dict:
    """One request of the kit's board registration; any answer but ``200`` is
    :class:`_Refused`, saying *what* it was, its status and its body."""
    headers = {"Authorization": f"Bearer {login}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base.rstrip("/") + "/api/v1/" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=tls_context()) as answer:
            data = answer.read()
    except urllib.error.HTTPError as exc:
        text = exc.read()[:240].decode("utf-8", "replace")
        hint = " (is PROCEDO_KIT_IMPORTER_TOKEN an importer's token?)" if "import" in what else ""
        raise _Refused(f"{what}, {method} {path}, answered {exc.code}: {text}{hint}") from exc
    except OSError as exc:
        raise _Refused(f"{what}, {method} {path}, got no answer: {exc}") from exc
    try:
        found = json.loads(data) if data else {}
    except ValueError as exc:
        raise _Refused(f"{what}, {method} {path}, answered no JSON: {data[:240]!r}") from exc
    return found if isinstance(found, dict) else {}


def fold_name(name: str) -> str:
    """How names compare (contract §2.4): NFC, then case folding, then NFC."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


class CallRecorder:
    """A pytest plugin noting, case by case, the optional calls (and the bench's runs) the
    reference servers of this process answer (``reference_server.offer.note``), into
    ``<folder>/calls-<worker>.json`` when pytest ends. A fixture's calls count for every case
    using it, the cases after its first included."""

    def __init__(self, folder: str) -> None:
        self.folder = Path(folder)
        self.cases: dict[str, list[str]] = {}
        self.fixtures: dict[tuple[str, str], set[str]] = {}
        self.skipped: set[str] = set()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item) -> None:
        from reference_server import offer

        offer.NOTED = []

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        from reference_server import offer

        noted = offer.NOTED
        start = len(noted) if noted is not None else 0
        yield
        if noted is None:
            return
        if getattr(fixturedef.func, "offer_aware", False):
            # its optional calls made only when the server lists them; its bench's runs counted
            made = [call for call in noted[start:] if call == offer.BENCH_RUN]
            noted[start:] = made
        key = (fixturedef.argname, fixturedef.baseid)
        self.fixtures.setdefault(key, set()).update(noted[start:])

    def pytest_runtest_logreport(self, report) -> None:
        if report.skipped:  # a case that did not run makes no call worth keeping
            self.skipped.add(case_key(report.nodeid))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        yield
        from reference_server import offer

        made = set(offer.NOTED or [])
        offer.NOTED = None
        if case_key(item.nodeid) in self.skipped:
            return
        definitions = getattr(getattr(item, "_fixtureinfo", None), "name2fixturedefs", {})
        for name in item.fixturenames:
            for fixturedef in definitions.get(name, ()):
                made |= self.fixtures.get((fixturedef.argname, fixturedef.baseid), set())
        self.cases[case_key(item.nodeid)] = sorted(made)

    def pytest_sessionfinish(self, session) -> None:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"calls-{worker}.json"
        target.write_text(json.dumps(self.cases, sort_keys=True, indent=1), encoding="utf-8")


def register(config) -> None:
    """The kit's plugins on *config*: the calls skipped up front against a server the
    environment names, and the record of the calls each case makes when asked for."""
    config.pluginmanager.register(OfferedCalls(), "procedo-offered-calls")
    folder = os.environ.get(RECORD_ENV, "").strip()
    if folder:
        config.pluginmanager.register(CallRecorder(folder), "procedo-call-recorder")


def tls_context() -> ssl.SSLContext:
    bundle = os.environ.get(CA_BUNDLE_ENV, "")
    return ssl.create_default_context(cafile=bundle or None)


def switched_on(variable: str) -> bool:
    """Whether the environment sets *variable* to ``1``: an optional kind of case is wanted."""
    return os.environ.get(variable, "").strip() == "1"


def slow(why: str) -> None:
    """Skip this case unless ``PROCEDO_KIT_SLOW=1``, saying *why* it is slow."""
    if not switched_on(SLOW_ENV):
        skip(f"set {SLOW_ENV}=1 to run it: {why}")


def slow_wait() -> float:
    """The seconds a slow case waits for the server's answer (``PROCEDO_KIT_SLOW_WAIT``)."""
    value = os.environ.get(SLOW_WAIT_ENV, "").strip()
    try:
        return float(value) if value else 300.0
    except ValueError:
        pytest.fail(f"{SLOW_WAIT_ENV} is a number of seconds, not {value!r}", pytrace=False)


def load_settings() -> tuple[int, float]:
    """The load run the environment asks for: ``(benches, seconds)``; seconds 0 for a few rounds
    per bench, more for a soak run. Skipped unless ``PROCEDO_KIT_LOAD=1``,
    ``PROCEDO_KIT_LOAD_BENCHES`` or ``PROCEDO_KIT_LOAD_MINUTES`` is set."""
    benches = os.environ.get(LOAD_BENCHES_ENV, "").strip()
    minutes = os.environ.get(LOAD_MINUTES_ENV, "").strip()
    if not (switched_on(LOAD_ENV) or benches or minutes):
        skip(f"set {LOAD_ENV}=1 (or {LOAD_BENCHES_ENV}, {LOAD_MINUTES_ENV}) to run it")
    try:
        count = int(benches) if benches else 20
        seconds = float(minutes) * 60 if minutes else 0.0
    except ValueError:
        pytest.fail(f"{LOAD_BENCHES_ENV} is a whole number, {LOAD_MINUTES_ENV} a number of "
                    "minutes", pytrace=False)  # fmt: skip
    if count < 1 or seconds < 0:
        pytest.fail(
            f"{LOAD_BENCHES_ENV} is at least 1, {LOAD_MINUTES_ENV} at least 0", pytrace=False
        )
    return count, seconds
