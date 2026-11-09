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


def active() -> frozenset[str]:
    """The rule ids the environment names."""
    return frozenset(n.strip() for n in os.environ.get(ENV, "").split(",") if n.strip())


def on(rule: str) -> bool:
    """Whether this server breaks *rule* on purpose."""
    return rule in active()
