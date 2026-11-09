"""The kit's fixtures: the suites and the app's client modules come from this folder alone
(``suites/``, ``vendor/``), and every case runs in a sandbox of its own (the per-user folders
and settings of the app's client point into pytest's temporary folder)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE / "vendor"), str(HERE / "suites")]


def pytest_configure(config):
    from procedo.core import testing

    testing.register_plugin(config)
    config.addinivalue_line("markers", "servers: talks to the server under test")
    config.addinivalue_line("markers", "postgres: the app's own storage cases, never run by the kit")
    config.addinivalue_line(
        "markers",
        "rule(*ids): the contract rules a case catches (the contract's rule-<id> anchors)",
    )
    config.addinivalue_line(
        "markers", "switch(variable): runs only when the environment sets this variable to 1"
    )


def pytest_report_header(config):
    import server_target

    base = server_target.external()
    return f"server under test: {base or 'the reference server, started for each case'}"


@pytest.fixture(autouse=True)
def _fresh_push_backoff():
    """The outbox retry schedule is per process: no case inherits another's failures."""
    from procedo.core import evidence_backoff

    evidence_backoff.retry_now()


@pytest.fixture(autouse=True)
def _isolate_app_settings(monkeypatch, tmp_path):
    """The client's settings file is this case's."""
    from procedo.core import app_settings

    appdata = tmp_path / "_settings"
    appdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_settings, "get_app_data_dir", lambda: appdata)


@pytest.fixture(autouse=True)
def _isolate_package_roots(monkeypatch, tmp_path):
    """The client's local packages are this case's."""
    from procedo.core import package_store

    monkeypatch.setattr(package_store, "local_root", lambda: tmp_path / "_packages" / "local")
    monkeypatch.setattr(
        package_store, "installed_root", lambda: tmp_path / "_packages" / "installed"
    )
