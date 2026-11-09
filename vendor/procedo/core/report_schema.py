"""Data-driven report field schema — load + validate + materialize defaults.

A **report package** is a Word template plus a ``schema.json`` sitting beside it.
The schema declares the editable doc-metadata fields (scalars, records, lists) and
the package identity (``package_id`` / ``package_version`` / ``template`` — the
template file name it belongs to). The built-in package lives in
``packages/builtin/procedo/exports/word_export/`` and reproduces the historical hardcoded
``word_export.create_default_sidecar`` field set exactly (golden-tested).

Resolution: :func:`load_schema_for_template` looks for ``<stem>.schema.json`` then
``schema.json`` next to the template; a sibling that names a different template is
ignored (warning) and the built-in schema is used. Field VALUES stay per-output in
the export sidecar (the "manifest"), which is stamped with ``report_package``
(:func:`package_ref`) so a later load can detect a package mismatch.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCALAR, RECORD, LIST = "scalar", "record", "list"

# The manifest format version. A schema may declare it under "$schema"; absent
# means v1 (compatibility with pre-versioning packages). Any other value is
# rejected so a future v2 manifest fails loudly instead of half-rendering.
SCHEMA_VERSION = "report-schema-v1"

# Engine-owned keys a user schema may NOT declare (execution payload + plumbing).
RESERVED_KEYS = frozenset(
    {
        "tests",
        "scenario",
        "total_equipment",
        "report_package",
        "template_path_override",
        "jpeg_images",
        "include_images",
        "alphabetical_order",
        "show_titles",
    }
)

# Widget hints DocForm understands: "multiline" renders a QPlainTextEdit,
# "line" (or omitting the key) a QLineEdit. Anything else is unknown.
_WIDGETS = ("line", "multiline")


class SchemaError(ValueError):
    """A report schema is structurally malformed."""


def builtin_package_dir() -> Path:
    """The built-in Word report package: the app package's ``exports/word_export/``
    holding the Word template and its ``schema.json`` side by side. Served
    directly from the app tier by ``export_packages`` discovery — never copied
    into projects."""
    from .package_store import app_package_root

    return app_package_root() / "exports" / "word_export"


def builtin_schema_path() -> Path:
    """Built-in report schema location (inside :func:`builtin_package_dir`)."""
    return builtin_package_dir() / "schema.json"


def builtin_template_path() -> Path:
    """The built-in package's Word template, named by its schema's ``template`` key."""
    return builtin_package_dir() / load_builtin_schema()["template"]


def load_schema(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_schema(data)
    return data


def schema_candidates_for_template(template_path: Path) -> list[Path]:
    """Where a template's package schema may sit, most specific first:
    ``<stem>.schema.json`` (several packages can share one folder, e.g. a
    project's ``config/templates/exports/``) then the plain ``schema.json``."""
    tp = Path(template_path)
    return [tp.with_name(f"{tp.stem}.schema.json"), tp.with_name("schema.json")]


def schema_path_for_template(template_path: Path) -> Path | None:
    """The schema file that belongs to *template_path*, or None when the
    template ships without one (legacy) or its sibling names another template."""
    name = Path(template_path).name
    for cand in schema_candidates_for_template(template_path):
        if not cand.is_file():
            continue
        try:
            declared = json.loads(cand.read_text(encoding="utf-8")).get("template")
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            log.warning("Ignoring unreadable report schema %s: %s", cand, exc)
            continue
        if declared == name:
            return cand
        log.warning("Ignoring report schema %s: it belongs to %r, not %r", cand, declared, name)
    return None


def load_schema_for_template(template_path: Path, fallback: Path | None = None) -> dict:
    """Load the package schema for *template_path*; legacy templates (no matching
    sibling schema) use *fallback* or the built-in schema. Raises ``SchemaError``
    only for a sibling that is present, matching, and malformed."""
    sibling = schema_path_for_template(template_path)
    if sibling is not None:
        return load_schema(sibling)
    if fallback is not None:
        return load_schema(fallback)
    return load_builtin_schema()


def load_builtin_schema() -> dict:
    return load_schema(builtin_schema_path())


def package_ref(schema: dict) -> dict:
    """``{"id", "version"}`` identity of a schema's report package — what the export
    sidecar (manifest) records under ``report_package`` so a later load can spot a
    mismatch against the template's current package."""
    return {"id": schema["package_id"], "version": schema["package_version"]}


# ---------------------------------------------------------------------------
#  Validation (the built-in must be perfect; package schemas validate fail-soft
#  at a higher layer — the caller drops a bad field and warns, never blanks the form)
# ---------------------------------------------------------------------------


def validate_schema(schema: dict) -> None:
    if not isinstance(schema, dict):
        raise SchemaError(f"schema must be a JSON object, got {type(schema).__name__}")
    version = schema.get("$schema")
    if version is not None and version != SCHEMA_VERSION:
        raise SchemaError(
            f"unsupported schema version {version!r} (expected {SCHEMA_VERSION!r}; absent means v1)"
        )
    for key in ("package_id", "package_version", "template"):
        value = schema.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SchemaError(f"schema.{key} must be a non-empty string")
    # Optional export-package keys (see export_packages): display title + render kind.
    title = schema.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise SchemaError("schema.title must be a non-empty string when present")
    kind = schema.get("kind")
    if kind is not None and kind not in ("docx", "markdown", "html"):
        raise SchemaError(f"schema.kind must be 'docx', 'markdown' or 'html', got {kind!r}")
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise SchemaError("schema.fields must be a list")
    # The HTML pipeline computes its context from test results and never
    # consumes fields — a fields-bearing html manifest is a silent trap.
    if kind == "html" and fields:
        raise SchemaError("schema.fields must be [] for kind 'html' (fields are never rendered)")
    seen: set[str] = set()
    for f in fields:
        _validate_field(f, seen)


def _validate_field(f: Any, seen: set[str]) -> None:
    if not isinstance(f, dict):
        raise SchemaError(f"field entry must be an object, got {f!r}")
    key = f.get("key")
    if not isinstance(key, str) or not key.strip():
        raise SchemaError(f"field needs a non-empty string key: {f!r}")
    if key in seen:
        raise SchemaError(f"duplicate field key: {key}")
    if key in RESERVED_KEYS:
        raise SchemaError(f"field key is reserved (engine-owned): {key}")
    seen.add(key)
    t = f.get("type", SCALAR)
    if t not in (SCALAR, RECORD, LIST):
        raise SchemaError(f"field {key}: bad type {t!r}")
    _validate_presentation(f, f"field {key}")
    if t == RECORD:
        _validate_subfields(f.get("fields"), key)
    elif t == LIST:
        item = f.get("item")
        if not isinstance(item, dict):
            raise SchemaError(f"list field {key}: missing item")
        item_type = item.get("type", SCALAR)
        if item_type not in (SCALAR, RECORD):
            raise SchemaError(
                f"list field {key}: item.type must be 'scalar' or 'record', got {item_type!r}"
            )
        if item_type == RECORD:
            _validate_subfields(item.get("fields"), key)
        _validate_cardinality(f.get("cardinality"), key)


def _validate_presentation(f: dict, where: str) -> None:
    """Presentation hints the form renders verbatim — a non-string label/group
    would crash DocForm (e.g. ``.replace()`` on an int group title)."""
    for key in ("label", "group", "placeholder", "default"):
        value = f.get(key)
        if value is not None and not isinstance(value, str):
            raise SchemaError(f"{where}: {key} must be a string, got {value!r}")
    widget = f.get("widget")
    if widget is not None and widget not in _WIDGETS:
        raise SchemaError(f"{where}: widget must be 'line' or 'multiline', got {widget!r}")


def _validate_cardinality(card: Any, owner: str) -> None:
    if card is None:  # absent means growable
        return
    if not isinstance(card, dict):
        raise SchemaError(f"list field {owner}: cardinality must be an object, got {card!r}")
    mode = card.get("mode")
    if mode not in ("growable", "fixed"):
        raise SchemaError(
            f"list field {owner}: cardinality.mode must be 'growable' or 'fixed', got {mode!r}"
        )
    if mode == "fixed":
        count = card.get("count")
        # bool is an int in Python — count: true must not pass as 1.
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise SchemaError(f"fixed list {owner}: count must be an integer >= 1, got {count!r}")


def _validate_subfields(subs: Any, owner: str) -> None:
    # Sub-keys are names WITHIN the parent's value only (build_metadata_context
    # emits top-level keys; list_keys yields sub-keys as table-column names), so
    # they can never shadow the engine-owned RESERVED_KEYS — no reserved check.
    if not isinstance(subs, list) or not subs:
        raise SchemaError(f"{owner}: record needs a non-empty fields[]")
    sub_seen: set[str] = set()
    for s in subs:
        if not isinstance(s, dict):
            raise SchemaError(f"{owner}: sub-field entry must be an object, got {s!r}")
        k = s.get("key")
        if not isinstance(k, str) or not k.strip():
            raise SchemaError(f"{owner}: sub-field needs a non-empty string key: {s!r}")
        if k in sub_seen:
            raise SchemaError(f"{owner}: duplicate sub-field key {k!r}")
        sub_seen.add(k)
        where = f"{owner}.{k}"
        _validate_presentation(s, where)
        stretch = s.get("stretch")
        # The documented contract is 0/1 (bool is an int, True == 1) — any
        # other int (stretch: 42) is a typo, not a column weight.
        if stretch is not None and (not isinstance(stretch, int) or stretch not in (0, 1)):
            raise SchemaError(f"{where}: stretch must be 0, 1 or a bool, got {stretch!r}")
        width = s.get("width")
        if width is not None and (
            isinstance(width, bool) or not isinstance(width, int) or width < 1
        ):
            raise SchemaError(f"{where}: width must be an integer >= 1, got {width!r}")


# ---------------------------------------------------------------------------
#  Default materialization
# ---------------------------------------------------------------------------


def materialize_defaults(schema: dict) -> dict:
    """Default value dict from a schema: empty scalars, blank records, [] for a
    growable list, N blank items for a fixed list."""
    return {f["key"]: _default_value(f) for f in schema["fields"]}


def list_keys(field: dict) -> list[str]:
    """The ordered record sub-keys of a list/record field — the SINGLE source that
    replaces the triplicated positional key arrays in the dialog."""
    item = field.get("item", field)
    return [s["key"] for s in item.get("fields", [])]


def _default_value(f: dict) -> Any:
    t = f.get("type", SCALAR)
    if t == SCALAR:
        return ""
    if t == RECORD:
        return {s["key"]: "" for s in f["fields"]}
    if t == LIST:
        card = f.get("cardinality", {"mode": "growable"})
        if card.get("mode") == "fixed":
            return [_item_default(f["item"]) for _ in range(int(card["count"]))]
        return []
    return ""


def _item_default(item: dict) -> Any:
    if item.get("type", SCALAR) == RECORD:
        return {s["key"]: "" for s in item["fields"]}
    return ""


# ---------------------------------------------------------------------------
#  Render context (the document-metadata block fed to a template)
# ---------------------------------------------------------------------------


def build_metadata_context(schema: dict, sidecar: dict) -> dict:
    """Map a sidecar into the document-metadata render context: every schema
    field, with an empty-container fallback (scalar -> "", record -> {}, list ->
    []). The single source for the metadata block that the Word export (and, later,
    the HTML report) layer their execution payload on top of."""
    return {f["key"]: sidecar.get(f["key"], _empty_context_value(f)) for f in schema["fields"]}


def _empty_context_value(f: dict) -> Any:
    t = f.get("type", SCALAR)
    if t == RECORD:
        return {}
    if t == LIST:
        return []
    return ""
