"""A reference server for the Procedo evidence API, built from the contract only.

The contract is ``docs/plans/data_format.md``, ``docs/plans/procedo_api.openapi.yaml``,
``docs/plans/web_api_spec.md`` (the transport: address, headers, discovery, refusals) and
``docs/plans/web_api_projects.md`` (P1–P11). The questions this server raised are decided in
``docs/plans/contract_questions.md`` (``CONTRACT_QUESTIONS.md`` beside this file points there). The storage conformance suite runs the app's API client against this server, so
the contract itself gets tested.

It imports one module of the app, and only because the contract defines a rule by it: the
paths that never travel are exactly the app's sync exclusion list (P11; ``project_files``).

    base_url, server = start(tmp_path, {"t-bench": ["bench"], "t-manager": ["manager"]})
    ...
    stop(server)

Scope: every call of the contract: ``/info``, the storages (S1–S4) and their large-file calls
(B1, B2, §3.4b), the evidence calls (E1–E15), the projects and tests (P1–P11) and the library
(L1–L3). A project P11 created keeps its files here; one the server knows only by its evidence
keeps them elsewhere, and P2–P10 answer ``404`` for it (data_format.md §5.1). A test's
releases, runs and current-run marks are filed under its hidden ``test_id`` (``ids``).
"""

from .server import ReferenceServer, start, stop

__all__ = ["ReferenceServer", "start", "stop"]
