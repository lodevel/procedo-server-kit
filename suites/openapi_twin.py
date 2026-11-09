"""The contract's machine-readable twin, ``procedo_api.openapi.yaml`` (``procedo_server_contract.md``
§9), read and applied with the standard library alone, so the server conformance kit imports
nothing but pytest:

* :func:`load_yaml` reads the YAML the twin is written in (block mappings and sequences, flow
  ``{…}`` and ``[…]`` on one line, plain and quoted scalars, ``|`` blocks) and fails on any
  other form, so a layout it does not know is an error, never a silently wrong schema;
* :class:`Twin` finds the schema of an answer (its call by path and method, its status, its
  content type) and checks a JSON value against it: the JSON Schema keywords the twin uses
  (``type``, ``enum``, ``const``, ``pattern``, ``minimum``, ``minLength``, ``required``,
  ``properties``, ``additionalProperties``, ``patternProperties``, ``propertyNames``,
  ``items``, ``allOf``, ``oneOf``, ``anyOf``, ``if``/``then``/``else``, ``$ref``); any other
  validation keyword in the twin fails :meth:`Twin.check_keywords`.

In the kit the twin sits beside this module; in the repository it is ``docs/plans/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
NAME = "procedo_api.openapi.yaml"


def twin_path() -> Path:
    """The twin: beside this module (the kit), else the repository's ``docs/plans/``."""
    for candidate in (HERE / NAME, HERE.parent / "docs" / "plans" / NAME):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{NAME} is neither beside {HERE} nor in docs/plans/")


# ---- YAML, the subset the twin is written in -------------------------------------------------


class YamlError(ValueError):
    pass


_INT = re.compile(r"[-+]?[0-9]+")
_FLOAT = re.compile(r"[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?")
_BOOL = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}
_NULL = {"", "~", "null", "Null", "NULL"}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0", " ": " "}


def _plain(text: str) -> Any:
    text = text.strip()
    if text in _NULL:
        return None
    if text in _BOOL:
        return _BOOL[text]
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


def _double_quoted(text: str, start: int) -> tuple[str, int]:
    """The string opened by the ``"`` at *start*, and the index after its closing quote."""
    out, i = [], start + 1
    while i < len(text):
        char = text[i]
        if char == '"':
            return "".join(out), i + 1
        if char == "\\":
            code = text[i + 1]
            if code == "u":
                out.append(chr(int(text[i + 2 : i + 6], 16)))
                i += 6
                continue
            if code not in _ESCAPES:
                raise YamlError(f"unknown escape \\{code} in {text!r}")
            out.append(_ESCAPES[code])
            i += 2
            continue
        out.append(char)
        i += 1
    raise YamlError(f"unclosed quote in {text!r}")


def _single_quoted(text: str, start: int) -> tuple[str, int]:
    out, i = [], start + 1
    while i < len(text):
        if text[i] == "'":
            if text[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(text[i])
        i += 1
    raise YamlError(f"unclosed quote in {text!r}")


class _Flow:
    """One line's flow collection (``{a: b, c: [d, e]}``)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def parse(self) -> Any:
        value = self.value()
        self.blanks()
        if self.i != len(self.text):
            raise YamlError(f"trailing text after a flow value: {self.text!r}")
        return value

    def blanks(self) -> None:
        while self.i < len(self.text) and self.text[self.i] == " ":
            self.i += 1

    def value(self) -> Any:
        self.blanks()
        char = self.text[self.i : self.i + 1]
        if char == "{":
            return self.mapping()
        if char == "[":
            return self.sequence()
        if char == '"':
            value, self.i = _double_quoted(self.text, self.i)
            return value
        if char == "'":
            value, self.i = _single_quoted(self.text, self.i)
            return value
        start = self.i
        while self.i < len(self.text):
            char = self.text[self.i]
            if char in ",]}":
                break
            if char == ":" and self.text[self.i + 1 : self.i + 2] in (" ", ",", "}", "]", ""):
                break
            self.i += 1
        return _plain(self.text[start : self.i])

    def expect(self, char: str) -> None:
        self.blanks()
        if self.text[self.i : self.i + 1] != char:
            raise YamlError(f"expected {char!r} at {self.i} in {self.text!r}")
        self.i += 1

    def mapping(self) -> dict:
        self.expect("{")
        out: dict = {}
        self.blanks()
        if self.text[self.i : self.i + 1] == "}":
            self.i += 1
            return out
        while True:
            key = self.value()
            self.blanks()
            if self.text[self.i : self.i + 1] == ":":
                self.i += 1
                self.blanks()
                if self.text[self.i : self.i + 1] in (",", "}"):
                    value = None
                else:
                    value = self.value()
            else:
                value = None
            out[key] = value
            self.blanks()
            if self.text[self.i : self.i + 1] == ",":
                self.i += 1
                continue
            self.expect("}")
            return out

    def sequence(self) -> list:
        self.expect("[")
        out: list = []
        self.blanks()
        if self.text[self.i : self.i + 1] == "]":
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self.blanks()
            if self.text[self.i : self.i + 1] == ",":
                self.i += 1
                continue
            self.expect("]")
            return out


def _inline(text: str) -> Any:
    """A value written on the rest of a line: flow, quoted or plain, its comment dropped."""
    text = text.strip()
    if text[:1] in "{[":
        return _Flow(text).parse()
    if text[:1] in "\"'":
        value, end = (_double_quoted if text[0] == '"' else _single_quoted)(text, 0)
        rest = text[end:].strip()
        if rest and not rest.startswith("#"):
            raise YamlError(f"text after a quoted scalar: {text!r}")
        return value
    comment = re.search(r"\s#", text)
    return _plain(text[: comment.start()] if comment else text)


def _split_key(text: str) -> tuple[Any, str] | None:
    """``key: rest`` as ``(key, rest)``; None when the line is no mapping entry."""
    if text[:1] in "\"'":
        key, end = (_double_quoted if text[0] == '"' else _single_quoted)(text, 0)
        if text[end : end + 1] == ":" and text[end + 1 : end + 2] in (" ", ""):
            return key, text[end + 1 :]
        return None
    if text[:1] in "{[#":
        return None
    match = re.match(r"([^:#]*?):(?: |$)", text)
    if match is None:
        return None
    return _plain(match.group(1)), text[match.end() :]


class _Block:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.i = 0

    @staticmethod
    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def next_line(self) -> int | None:
        """The index of the next line holding something, comments and blanks skipped."""
        j = self.i
        while j < len(self.lines):
            stripped = self.lines[j].strip()
            if stripped and not stripped.startswith("#"):
                return j
            j += 1
        return None

    def parse(self) -> Any:
        j = self.next_line()
        if j is None:
            return None
        value = self.node(self.indent(self.lines[j]))
        if self.next_line() is not None:
            raise YamlError(f"line {self.next_line() + 1}: not read")
        return value

    def node(self, indent: int) -> Any:
        j = self.next_line()
        self.i = j
        line = self.lines[j]
        if line[indent:].startswith("- ") or line[indent:] == "-":
            return self.sequence(indent)
        return self.mapping(indent)

    def value_after(self, rest: str, indent: int, *, seq_same: bool) -> Any:
        """A key's value: on its line, a ``|`` block, or the lines below."""
        rest = rest.strip()
        if rest in ("|", "|-", "|+"):
            return self.literal(indent, rest)
        if rest and not rest.startswith("#"):
            return _inline(rest)
        j = self.next_line()
        if j is None:
            return None
        below = self.indent(self.lines[j])
        if below > indent:
            return self.node(below)
        if seq_same and below == indent and self.lines[j][indent:].startswith("- "):
            return self.sequence(indent)
        return None

    def literal(self, indent: int, style: str) -> str:
        out: list[str] = []
        block_indent = None
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() == "":
                out.append("")
                self.i += 1
                continue
            here = self.indent(line)
            if here <= indent:
                break
            if block_indent is None:
                block_indent = here
            if here < block_indent:
                raise YamlError(f"line {self.i + 1}: a block scalar line less indented")
            out.append(line[block_indent:])
            self.i += 1
        while out and out[-1] == "":
            out.pop()
        if style == "|-":
            return "\n".join(out)
        return "\n".join(out) + "\n" if out else ""

    def mapping(self, indent: int) -> dict:
        out: dict = {}
        while True:
            j = self.next_line()
            if j is None or self.indent(self.lines[j]) != indent:
                break
            line = self.lines[j]
            if line[indent:].startswith("- "):
                break
            split = _split_key(line[indent:])
            if split is None:
                raise YamlError(f"line {j + 1}: not a mapping entry: {line!r}")
            key, rest = split
            self.i = j + 1
            if key in out:
                raise YamlError(f"line {j + 1}: the key {key!r} twice")
            out[key] = self.value_after(rest, indent, seq_same=True)
        j = self.next_line()
        if j is not None and self.indent(self.lines[j]) > indent:
            raise YamlError(f"line {j + 1}: indented under no key: {self.lines[j]!r}")
        return out

    def sequence(self, indent: int) -> list:
        out: list = []
        while True:
            j = self.next_line()
            if j is None or self.indent(self.lines[j]) != indent:
                break
            line = self.lines[j]
            if not (line[indent:].startswith("- ") or line[indent:] == "-"):
                break
            rest = line[indent + 2 :]
            if rest.strip() == "":
                self.i = j + 1
                out.append(self.value_after("", indent, seq_same=False))
            elif _split_key(rest) is not None:
                # "- key: value": a mapping whose keys sit two columns in
                self.lines[j] = " " * (indent + 2) + rest
                self.i = j
                out.append(self.mapping(indent + 2))
            else:
                self.i = j + 1
                out.append(_inline(rest))
        return out


def load_yaml(text: str) -> Any:
    """The YAML document *text*, in the subset the twin is written in."""
    return _Block(text).parse()


# ---- The twin --------------------------------------------------------------------------------

#: The validation keywords :meth:`Twin.errors` applies.
APPLIED = {
    "$ref", "type", "enum", "const", "pattern", "minimum", "maximum", "minLength", "maxLength",
    "minItems", "required", "properties", "additionalProperties", "patternProperties",
    "propertyNames", "items", "allOf", "oneOf", "anyOf", "if", "then", "else",
}  # fmt: skip
#: Keywords that only annotate (JSON Schema 2020-12 and OpenAPI): nothing to check.
ANNOTATIONS = {
    "description", "examples", "example", "readOnly", "writeOnly", "format", "title",
    "contentMediaType", "default", "deprecated", "summary", "$comment", "discriminator",
}  # fmt: skip


class Twin:
    """The twin, loaded once; the schema of each answer and the check of a value against it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or twin_path()
        self.doc = load_yaml(self.path.read_text(encoding="utf-8"))
        self.paths: dict[str, dict] = self.doc["paths"]
        self._templates = [(tuple(p.strip("/").split("/")), p) for p in self.paths]
        #: Every property name a schema of the twin defines.
        self.properties: set[str] = set()
        self._collect(self.doc)

    def _collect(self, node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "properties" and isinstance(item, dict):
                    self.properties |= set(item)
                self._collect(item)
        elif isinstance(node, list):
            for item in node:
                self._collect(item)

    # -- finding the schema --

    def template(self, path: str) -> str | None:
        """The twin's path matching *path* (``/projects/<id>/runs``, no ``/api/v1``); the one
        with most literal segments when several do."""
        segments = tuple(path.strip("/").split("/"))
        found = []
        for pattern, template in self._templates:
            if len(pattern) != len(segments):
                continue
            literal = 0
            for want, got in zip(pattern, segments, strict=True):
                if want.startswith("{"):
                    if got == "":
                        break
                elif want != got:
                    break
                else:
                    literal += 1
            else:
                found.append((literal, template))
        return max(found)[1] if found else None

    def resolve(self, node: Any) -> Any:
        while isinstance(node, dict) and set(node) <= {"$ref", "description"} and "$ref" in node:
            node = self.ref(node["$ref"])
        return node

    def ref(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise KeyError(f"only local references are read: {ref}")
        node: Any = self.doc
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def response(self, method: str, path: str, status: int) -> dict | None:
        """The twin's response for this answer (its status's, else ``default``); None when the
        call is not in the twin."""
        template = self.template(path)
        if template is None:
            return None
        operation = self.paths[template].get(method.lower())
        if operation is None:
            return None
        responses = operation["responses"]
        chosen = responses.get(str(status), responses.get(status))
        if chosen is None:
            chosen = responses.get(f"{str(status)[0]}XX", responses.get("default"))
        return self.resolve(chosen)

    def answer_schema(self, method: str, path: str, status: int, content_type: str) -> Any:
        """The schema the JSON of this answer must match. A ``multipart/mixed`` answer's
        ``json`` part is the object its ``application/json`` alternative names."""
        response = self.response(method, path, status)
        if response is None:
            raise LookupError(f"{method} {path}: no such call in the twin")
        content = response.get("content") or {}
        main = content_type.split(";")[0].strip().lower()
        if main == "multipart/mixed":
            main = "application/json"
        if main not in content:
            raise LookupError(
                f"{method} {path} {status}: the twin names no {content_type!r} answer "
                f"(it names {sorted(content)})"
            )
        return content[main]["schema"]

    # -- checking a value --

    def errors(self, value: Any, schema: Any, where: str = "$") -> list[str]:
        """Every way *value* breaks *schema*, each naming where."""
        if schema is True or schema is None:
            return []
        if schema is False:
            return [f"{where}: no value is allowed here"]
        out: list[str] = []
        if "$ref" in schema:
            out += self.errors(value, self.ref(schema["$ref"]), where)
        if "type" in schema and not _is_type(value, schema["type"]):
            return out + [f"{where}: {_show(value)} is not of type {schema['type']}"]
        if "enum" in schema and value not in schema["enum"]:
            out.append(f"{where}: {_show(value)} is not one of {schema['enum']}")
        if "const" in schema and not _same(value, schema["const"]):
            out.append(f"{where}: {_show(value)} is not {schema['const']!r}")
        if isinstance(value, str):
            if "pattern" in schema and re.search(schema["pattern"], value) is None:
                out.append(f"{where}: {value!r} does not match {schema['pattern']}")
            if len(value) < schema.get("minLength", 0):
                out.append(f"{where}: {value!r} is shorter than {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                out.append(f"{where}: {value!r} is longer than {schema['maxLength']}")
        if _is_number(value):
            if "minimum" in schema and value < schema["minimum"]:
                out.append(f"{where}: {value} is under {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                out.append(f"{where}: {value} is over {schema['maximum']}")
        if isinstance(value, dict):
            out += self._object(value, schema, where)
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                out.append(f"{where}: fewer than {schema['minItems']} items")
            if "items" in schema:
                for n, item in enumerate(value):
                    out += self.errors(item, schema["items"], f"{where}[{n}]")
        for sub in schema.get("allOf", ()):
            out += self.errors(value, sub, where)
        if "anyOf" in schema and all(self.errors(value, s, where) for s in schema["anyOf"]):
            out.append(f"{where}: {_show(value)} matches none of anyOf")
        if "oneOf" in schema:
            matched = sum(not self.errors(value, s, where) for s in schema["oneOf"])
            if matched != 1:
                out.append(f"{where}: {_show(value)} matches {matched} of oneOf, not exactly 1")
        if "if" in schema:
            branch = "then" if not self.errors(value, schema["if"], where) else "else"
            if branch in schema:
                out += self.errors(value, schema[branch], where)
        return out

    def _object(self, value: dict, schema: dict, where: str) -> list[str]:
        out = []
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{where}: {key} is required")
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        for key, item in value.items():
            here = f"{where}.{key}"
            matched = False
            if key in properties:
                matched = True
                out += self.errors(item, properties[key], here)
            for pattern, sub in patterns.items():
                if re.search(pattern, key):
                    matched = True
                    out += self.errors(item, sub, here)
            if not matched and "additionalProperties" in schema:
                out += self.errors(item, schema["additionalProperties"], here)
            if "propertyNames" in schema:
                out += [
                    f"{where}: the key {key!r} breaks propertyNames ({e})"
                    for e in self.errors(key, schema["propertyNames"], here)
                ]
        return out

    def check_keywords(self) -> list[str]:
        """Every keyword of a schema in the twin that :meth:`errors` neither applies nor may
        ignore, by where it is: a twin using one fails, rather than being half checked."""
        found: list[str] = []

        def schema(node: Any, where: str) -> None:
            if not isinstance(node, dict):
                return
            for key, item in node.items():
                here = f"{where}/{key}"
                if key not in APPLIED | ANNOTATIONS:  # an unquoted comma in prose lands here too
                    found.append(here)
                if key in ("properties", "patternProperties"):
                    for name, sub in item.items():
                        schema(sub, f"{here}/{name}")
                elif key in ("allOf", "oneOf", "anyOf"):
                    for n, sub in enumerate(item):
                        schema(sub, f"{here}[{n}]")
                elif key in (
                    "items",
                    "additionalProperties",
                    "propertyNames",
                    "if",
                    "then",
                    "else",
                ):
                    schema(item, here)

        def find(node: Any, where: str) -> None:
            if isinstance(node, list):
                for n, item in enumerate(node):
                    find(item, f"{where}[{n}]")
            elif isinstance(node, dict):
                for key, item in node.items():
                    if key == "schema":
                        schema(item, f"{where}/schema")
                    else:
                        find(item, f"{where}/{key}")

        for name, sub in self.doc["components"]["schemas"].items():
            schema(sub, f"#/components/schemas/{name}")
        find(self.doc["paths"], "#/paths")
        find({k: v for k, v in self.doc["components"].items() if k != "schemas"}, "#/components")
        return found


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_type(value: Any, wanted: str | list) -> bool:
    if isinstance(wanted, list):
        return any(_is_type(value, one) for one in wanted)
    return {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
        "number": _is_number,
        "integer": lambda v: _is_number(v) and float(v).is_integer(),
    }[wanted](value)


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) and type(a) is type(b)


def _show(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "..."
