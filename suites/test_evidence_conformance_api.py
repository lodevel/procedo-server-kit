"""The conformance suite of ``test_evidence_conformance.py`` over the JSON API: every case, with
the same table of known gaps, run by Procedo's API client (:class:`evidence_store_api.ApiStore`)
against the reference server (``tests/reference_server``, used as a black box: ``start`` and
``stop``), or the server the environment names (``server_target``): each case writes under a
project id of its own. The two were built from the contract alone, each without the other's code, so where
they disagree the contract is unclear, or one of them is wrong.

Each case also holds the client to the action map (:data:`evidence_actions.ACTIONS`): every call
an action makes is one of the calls the map names for it. A LOCAL action calls what the actions
it stands for name (``flush_outbox`` the writes it sends, ``local_path`` the reads whose bytes it
places), and discovery belongs to no action (:data:`evidence_actions.DISCOVERY`): any action may
make it, once per store, before its first call.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import server_target
import test_evidence_conformance
from test_evidence_board_layout import PID, WHY
from test_evidence_conformance import PROTOCOL, Storage, params, run_case

from procedo.core.evidence_actions import ACTIONS, DISCOVERY
from procedo.core.evidence_store_api import ApiStore


def _calls_of(*actions: str) -> frozenset[str]:
    return frozenset(op for a in actions for op in ACTIONS[a].calls)


#: What a LOCAL action may call: what the actions it stands for name.
LOCAL_CALLS = {
    "flush_outbox": _calls_of(
        "write_run", "commit_run", "write_artifact", "add_run_annotation_entries",
        "add_board_entries",
    ),
    "local_path": _calls_of("read_run", "read_artifact"),
}  # fmt: skip


def _allowed(action: str) -> frozenset[str]:
    mapped = ACTIONS[action]
    return frozenset(DISCOVERY) | (
        frozenset(mapped.calls) if mapped.calls else LOCAL_CALLS.get(action, frozenset())
    )


def _recording(store: ApiStore, calls: list[tuple[str, str]]) -> ApiStore:
    """``store`` noting, for each call it makes, the action of the interface it makes it for
    (the outermost one a case called)."""
    acting = threading.local()

    def noting(action: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            outer = getattr(acting, "name", None)
            acting.name = outer or action
            try:
                return method(*args, **kwargs)
            finally:
                acting.name = outer

        return call

    for action in PROTOCOL & set(dir(store)):
        setattr(store, action, noting(action, getattr(store, action)))
    sent = store.io.call

    def call(op: str, *args: Any, **kwargs: Any) -> Any:
        calls.append((getattr(acting, "name", None) or "", op))
        return sent(op, *args, **kwargs)

    store.io.call = call  # type: ignore[method-assign]
    return store


@pytest.fixture
def server(tmp_path):
    """The reference server: a bench's login, a manager's and one holding the importer right
    beside bench (the evidence moves onto it). The manager's login is not the name the person typed (``WHY.deleted_by``): the
    server keeps the login as ``deleted_by``
    and the name typed as ``deleted_by_name`` (§6.7), and the trash reads back the name typed,
    as on every storage."""
    tokens = {
        "t-bench": {"name": "bench", "roles": ["bench"]},
        "t-manager": {"name": "manager-login", "roles": ["manager"]},
        "t-mover": {"name": "mover-login", "roles": ["bench", "importer"]},  # moves data in
    }
    assert WHY.deleted_by != "manager-login"
    with server_target.serve(tmp_path / "server", tokens) as served:
        yield served


def api_store(base_url: str, tmp_path: Path, machine: str, **kwargs: Any) -> ApiStore:
    """A bench's store on the server, with its own project copy, outbox and cache."""
    project = tmp_path / f"project-{machine}"
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "config" / "config.json").write_text(json.dumps({"project_id": PID}), "utf-8")
    bench, manager = server_target.token("bench"), server_target.token("manager")
    kwargs.setdefault("credentials", lambda _identity: ("bench", bench))
    kwargs.setdefault("manager", lambda: manager)
    return ApiStore(
        server_target.endpoint(base_url),
        machine_id=machine,
        project_root=project,
        project_id=PID,
        outbox=tmp_path / f"outbox-{machine}",
        cache_dir=tmp_path / f"cache-{machine}",
        **kwargs,
    )


@pytest.fixture
def calls() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def storage(server, calls, tmp_path, monkeypatch) -> Storage:
    """The storage, under a project id no other case writes under; on a server the environment
    names, a deletion is kept for the days its ``/info`` says."""
    base_url, _running = server
    pid = server_target.fresh_id()
    monkeypatch.setitem(globals(), "PID", pid)
    monkeypatch.setattr(test_evidence_conformance, "PID", pid)
    if server_target.external():
        days = server_target.info(base_url)["limits"]["retention_days"]
        monkeypatch.setattr(test_evidence_conformance, "RETENTION_DAYS", days)
    return Storage(
        "api", lambda *, machine: _recording(api_store(base_url, tmp_path, machine), calls)
    )


@pytest.mark.parametrize(("case", "backend"), params("api", pytest.mark.servers))
def test_every_action_reads_back_as_it_went_in_over_the_api(
    case, backend, storage, calls, monkeypatch
):
    run_case(case, storage, monkeypatch)
    outside = sorted({(action, op) for action, op in calls if op not in _allowed(action)})
    assert outside == [], "calls the action map does not name for these actions"
