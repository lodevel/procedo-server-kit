"""The contract rule this reference server breaks on purpose: none, unless the environment names
one in ``PROCEDO_CONTRACT_FAULT`` (a rule id, read on every request).

Where the server keeps a rule that a kit check claims, it may ask :func:`on` (or
``State.broken``) and break that rule when told to; the repository's fault table
(``tests/contract_faults.py``, not part of the kit) says how each rule is broken, and its proof
(``tests/test_contract_faults.py``) runs the checks claiming a rule against a server breaking
it. Unset, the server keeps every rule.
"""

from __future__ import annotations

import os

#: The variable naming the rule to break (one rule id, or several comma-separated).
ENV = "PROCEDO_CONTRACT_FAULT"
#: Beside it, for a rule broken call by call (6.1.import-unlisted-501): the one call broken.
CALL_ENV = "PROCEDO_CONTRACT_FAULT_CALL"


def active() -> frozenset[str]:
    """The rule ids the environment names."""
    return frozenset(n.strip() for n in os.environ.get(ENV, "").split(",") if n.strip())


def only_call() -> str:
    """The one call :data:`CALL_ENV` names a rule broken on; ``""``: the fault's own choice."""
    return os.environ.get(CALL_ENV, "").strip()


def on(rule: str) -> bool:
    """Whether this server breaks *rule* on purpose."""
    return rule in active()
