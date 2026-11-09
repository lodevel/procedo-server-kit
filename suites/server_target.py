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
import ssl
import urllib.request
import uuid
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
        pytest.skip(f"{variable} is not set: this case needs a login holding {OPTIONAL[role]}")
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
    reference server under *root* holding *tokens* (and *limits*), stopped afterwards; or the
    server the environment names, which a case needing *limits* cannot use."""
    base_url = external()
    if base_url:
        if limits:
            local_only(f"it needs the server's own limits set to {limits}")
        yield base_url, None
        return
    base_url, running = start(root, tokens, **limits)
    try:
        yield base_url, running
    finally:
        stop(running)


def local_only(why: str) -> None:
    """Skip this case on a server the environment names, saying *why*."""
    if external():
        pytest.skip(f"runs only against the reference server: {why}")


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


def tls_context() -> ssl.SSLContext:
    bundle = os.environ.get(CA_BUNDLE_ENV, "")
    return ssl.create_default_context(cafile=bundle or None)


def switched_on(variable: str) -> bool:
    """Whether the environment sets *variable* to ``1``: an optional kind of case is wanted."""
    return os.environ.get(variable, "").strip() == "1"


def slow(why: str) -> None:
    """Skip this case unless ``PROCEDO_KIT_SLOW=1``, saying *why* it is slow."""
    if not switched_on(SLOW_ENV):
        pytest.skip(f"set {SLOW_ENV}=1 to run it: {why}")


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
        pytest.skip(f"set {LOAD_ENV}=1 (or {LOAD_BENCHES_ENV}, {LOAD_MINUTES_ENV}) to run it")
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
