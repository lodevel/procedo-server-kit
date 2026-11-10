"""The load and soak test of the server conformance kit: many benches at once (``server_load``),
off by default.

Run it with ``PROCEDO_KIT_LOAD=1`` (20 benches, a few rounds each), ``PROCEDO_KIT_LOAD_BENCHES``
benches, and ``PROCEDO_KIT_LOAD_MINUTES`` for a soak run of that long. It fails when anything is
lost, refused or read back different; it prints each call's response times (median, p95, max)
and never fails on them. It writes under a project id of its own.
"""

from __future__ import annotations

import pytest
import server_load
import server_target

pytestmark = pytest.mark.servers


@pytest.mark.offer_aware  # the lot only when the server offers E15
@pytest.mark.boards_aware
def test_many_benches_at_once_lose_nothing_and_read_back_the_same(tmp_path, capsys):
    benches, seconds = server_target.load_settings()
    with server_target.serve(tmp_path / "server", server_target.LOCAL_LOGINS) as (base, running):
        server_target.pushes_runs(base)
        load = server_load.run(base, benches=benches, seconds=seconds)
    with capsys.disabled():
        print("\n" + load.report())
    assert load.rounds >= benches, load.report()
    assert not load.failures, "\n".join(load.failures[:30])
    if running is not None:
        assert not running.internal_errors, running.internal_errors[:3]
