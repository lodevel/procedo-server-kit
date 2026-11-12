"""What the log must never hold, hidden from any text before it is written: :func:`redact`.

The app logs no secret on purpose; this is for text from elsewhere — an exception's message, a
library's line, a server's refusal — that may still carry one:

* a credential header or field (``Authorization``, ``Cookie``, ``Set-Cookie``, ``X-Api-Key``…):
  its whole value, every cookie pair of it;
* a ``Bearer``, ``token`` or ``passwd`` followed by a value that looks like one;
* a key naming a secret, compound names included (``password``, ``db_password``,
  ``PGPASSWORD``, ``client_secret``, ``id_token``, ``private_key``, ``credentials``, ``pass``,
  ``auth``, ``key``…), with ``=`` or ``:``, its value quoted (spaces kept inside), a tuple, or
  bare;
* the password of an address ``scheme://user:password@host`` (one with ``@`` or ``/`` in it, an
  address turned into a path with one slash or a backslash, ``mailto:``), and of a
  ``user:password@host`` with no scheme.

A header named at the end of a line hides nothing of the next line. Prose ("the password is
…") is not recognised.
"""

from __future__ import annotations

import re

HIDDEN = "[hidden]"
#: The longest text from outside (a server's sentence in an error) the app keeps.
FOREIGN_MAX = 300

_KEPT = (
    r"(?![\s\"']*\[hidden\])"  # a value hidden already stays as it is: hiding twice changes nothing
)
_VALUE = _KEPT + r"(?:'[^']*'|\"[^\"]*\"|\([^)]*\)|[^\s,;&}\)\]]+)"
_RULES = (
    # an address's password: up to the last @ of the address (mailto: has no slashes)
    (
        re.compile(
            r"(\b[a-zA-Z][a-zA-Z0-9+.-]*:(?:[/\\]{1,2}|(?<=mailto:))[^/\\\s:@]+:)" + _KEPT + r"\S*@"
        ),
        r"\1" + HIDDEN + "@",
    ),
    # user:password@host with no scheme (an address's own rule came first)
    (
        re.compile(r"(?<![:/\\\w.+-])([\w.+-]+:)" + _KEPT + r"[^\s:@/\\]+@(?=[\w-])"),
        r"\1" + HIDDEN + "@",
    ),
    # a credential header or field: its whole value, to the end of ITS line or the quote
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|"
            r"x-auth-token)\b([\"']?[ \t]*[:=][ \t]*[\"']?)" + _KEPT + r"[^\r\n\"'}]+"
        ),
        r"\1\2" + HIDDEN,
    ),
    # Bearer <token>; token or passwd <value that looks like one: 8 characters, a capital or a
    # digit> (prose, "the password is…", is not this module's)
    (re.compile(r"(?i)\b(bearer)[ \t]+" + _KEPT + r"[^\s,;\"')\]]+"), r"\1 " + HIDDEN),
    (
        re.compile(r"\b((?i:token|passwd))[ \t]+" + _KEPT + r"(?=[^\s]*[A-Z0-9])[^\s,;\"')\]]{8,}"),
        r"\1 " + HIDDEN,
    ),
    # a key naming a secret, and its value
    (
        re.compile(
            r"(?i)\b([\w-]*(?:password|passwd|pwd|passphrase|secret|token|api_?key|"
            r"private_?key|credentials)[\w-]*|pass|auth|key)\b([\"']?[ \t]*[:=][ \t]*)" + _VALUE
        ),
        r"\1\2" + HIDDEN,
    ),
)


def redact(text: str) -> str:
    """*text* with every secret this module knows hidden as ``[hidden]``."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def foreign(text: str) -> str:
    """A text from outside the app (a server's sentence) as it is kept, shown and logged: its
    first :data:`FOREIGN_MAX` characters ("…" after), any secret in it hidden."""
    cut = text if len(text) <= FOREIGN_MAX else text[:FOREIGN_MAX] + "\u2026"
    return redact(cut)
