"""One request as a call handler sees it, and the answer it gives back."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import faults
from .errors import bad_request
from .multipart import Part
from .state import State, User
from .validate import check_board_name, import_flag, version_hex


class Query:
    """The query string, each parameter at most once."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._values: dict[str, list[str]] = {}
        for name, value in pairs:
            self._values.setdefault(name, []).append(value)

    def names(self) -> set[str]:
        return set(self._values)

    def has(self, name: str) -> bool:
        return name in self._values

    def one(self, name: str) -> str | None:
        values = self._values.get(name)
        if not values:
            return None
        if len(values) > 1:
            raise bad_request(f"The parameter {name} is given more than once.")
        return values[0]

    def only(self, allowed: set[str]) -> None:
        extra = sorted(self.names() - allowed)
        if extra and not faults.on("2.11.unknown-query-param"):
            raise bad_request(f"This call takes no parameter {extra[0]!r}.", parameter=extra[0])


@dataclass
class Call:
    state: State
    user: User
    method: str
    params: dict[str, str]
    query: Query
    body: dict | None = None
    raw_json: bytes | None = None
    parts: dict[str, bytes] = field(default_factory=dict)
    raw_body: bytes = b""  # a call that takes the bytes themselves (§3.4b)
    wire: bytes = b""  # the body as it came, before any Content-Encoding was undone
    headers: dict[str, str] = field(default_factory=dict)  # the request's, names lower case

    def json(self) -> dict:
        if self.body is None:
            raise bad_request("This call needs a JSON body.")
        return self.body

    def import_asked(self) -> bool:
        """The body says ``"import": true`` (whoever sent it)."""
        return self.body is not None and import_flag(self.body)

    def importing(self) -> bool:
        """``"import": true`` from a login with the importer role (§6.10), whatever other roles it
        holds (§3.5); else the fields are ignored."""
        roles = self.user.roles
        if "bench" in roles and self.state.broken("3.5.importer-beside-bench"):
            return False
        return self.import_asked() and "importer" in roles

    def is_read_twin(self) -> bool:
        return self.method == "POST"

    def have(self) -> set[str]:
        """The sha256 hex the client holds: ``?have=`` on a GET, ``{"have": [...]}`` on a read twin."""
        if self.is_read_twin():
            values = self.json().get("have", [])
            if values is None:
                values = []
            if not isinstance(values, list):
                raise bad_request("have must be a list of versions.")
        else:
            text = self.query.one("have")
            values = [] if not text else text.split(",")
        held = {version_hex(value, "Each entry of have") for value in values}
        return set() if self.state.broken("6.2.have") else held

    def board_address(self) -> tuple[str | None, str | None]:
        """``part_number`` and ``board_revision`` of a call naming one board (§5.2, Addressing).

        Both are required, in the query (a read twin's included, §3.4), an empty value meaning
        "none", so a call that forgot them never reaches the "no part number" board by mistake.
        """
        found = []
        for name in ("part_number", "board_revision"):
            value = self.query.one(name)
            if value is None and self.state.broken("E5.addressing-400"):
                value = ""
            if value is None:
                raise bad_request(
                    f"A call naming a board needs {name} (empty for none), so it never reaches "
                    'the "no part number" board by mistake.',
                    parameter=name,
                )
            found.append(check_board_name(value, name) if value else None)
        return found[0], found[1]


@dataclass
class Answer:
    obj: dict
    parts: list[Part] = field(default_factory=list)
    raw: bytes | None = None  # a file's bytes themselves, in place of JSON (§3.4b)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
