"""Word export engine — sidecar management, context building, template rendering.

Each exported .docx gets a companion <filename>.meta.json sidecar stored in the
same folder.  The sidecar holds document-level metadata (title, revision history,
author, etc.) that is preserved across re-exports while test procedure data is
regenerated from the live procedure JSON files every time.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import project_local_state
from .board_netlist import location_label

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Sidecar helpers
# ---------------------------------------------------------------------------


def sidecar_path_for(output_path: Path) -> Path:
    """Return the sidecar JSON path that accompanies *output_path*.

    One rule for every format — suffix-qualified: ``summary.docx`` ->
    ``summary.docx.meta.json``, ``summary.md`` -> ``summary.md.meta.json`` —
    so same-stem outputs sharing a folder never collide on a sidecar."""
    return output_path.with_name(output_path.name + ".meta.json")


def create_default_sidecar(schema: dict | None = None) -> dict:
    """Return defaults materialized from *schema* (default: the built-in
    report schema — unchanged for legacy / editor callers)."""
    from .report_schema import load_builtin_schema, materialize_defaults

    defaults = materialize_defaults(schema if schema is not None else load_builtin_schema())
    defaults["template_path_override"] = ""
    return defaults


def load_sidecar(path: Path, *, schema: dict | None = None) -> dict:
    """Read sidecar from *path*, returning defaults if missing or invalid.

    *schema*, when given, materializes those defaults from that report-package
    schema instead of the built-in one (``None`` keeps today's behavior for
    the editor / Word paths)."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Merge with defaults so new keys are always present
            merged = create_default_sidecar(schema=schema)
            merged.update(data)
            return merged
        except Exception:
            log.warning("Could not parse sidecar %s — using defaults", path)
    return create_default_sidecar(schema=schema)


def text_file_bytes(text: str) -> bytes:
    """The bytes a text-mode ``write_text(text, encoding="utf-8")`` lands (platform newlines)."""
    return text.replace("\n", os.linesep).encode("utf-8")


def sidecar_bytes(data: dict) -> bytes:
    """The sidecar file :func:`save_sidecar` writes, as bytes (for the evidence store)."""
    return text_file_bytes(json.dumps(data, indent=2, ensure_ascii=False))


def save_sidecar(path: Path, data: dict) -> None:
    """Write *data* as the sidecar JSON at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
#  Template resolution
# ---------------------------------------------------------------------------


def get_project_template(
    project_path: Path | None = None,
    sidecar: dict | None = None,
) -> Path | None:
    """Resolve the Word export template.

    Priority:
    1. Per-export override from sidecar (``template_path_override``).
    2. The default docx export package's template
       (``export_packages.get_default_export_package(kind="docx")``: stored
       project default -> built-in Word package -> first discovered). A stored
       markdown default never resolves here — the ``kind`` filter skips it.

    Returns ``None`` if no docx package is discoverable at all.
    """
    # 1. per-export override — honored only for an existing .docx file: every
    # consumer of this resolver renders docx, so a sidecar must not hand it a
    # .md.j2 (or arbitrary) path.
    if sidecar:
        override = sidecar.get("template_path_override", "")
        if override:
            p = Path(override)
            if p.is_file() and p.suffix.lower() == ".docx":
                return p
            log.warning(
                "Ignoring template override %s (missing or not .docx) — "
                "falling back to package resolution",
                override,
            )

    # 2. default export package (project tier, else app builtin)
    from procedo.core import export_packages

    pkg = export_packages.get_default_export_package(project_path, kind="docx")
    return pkg.template_path if pkg is not None else None


def get_template_schema(template_path: Path) -> dict:
    """Load the report-package schema that belongs to *template_path*
    (``<stem>.schema.json`` or ``schema.json`` beside it, bound by its
    ``template`` key). Legacy templates without one use the built-in schema.
    Raises ``SchemaError`` only for a present-but-malformed sibling.
    """
    from .report_schema import load_schema_for_template

    return load_schema_for_template(template_path)


# ---------------------------------------------------------------------------
#  Image resolution
# ---------------------------------------------------------------------------


def _resolve_image(
    project_root: Path,
    component: str,
    pin: int | str | None,
    variant: str,
) -> Path | None:
    """Find a cached image in the machine-local ``.media_cache/<variant>/images/``."""
    cache = project_local_state.media_cache_dir(project_root)
    cache_dir = cache / variant / "images" if cache is not None else None
    if cache_dir is None or not cache_dir.is_dir():
        return None

    candidates: list[str] = []
    if pin is not None:
        candidates.append(f"{component}_pad{pin}.png")
        candidates.append(f"{component}_{pin}.png")
    candidates.append(f"{component}.png")

    for filename in candidates:
        path = cache_dir / filename
        if path.is_file():
            return path
    return None


def _count_uncached_images(project_root: Path, all_media_refs: list[dict]) -> tuple[int, int]:
    """Return *(total, uncached)* counts for the given media refs."""
    total = 0
    uncached = 0
    for ref in all_media_refs:
        component = ref.get("component", "")
        pin = ref.get("pin")
        if not component:
            continue
        total += 1
        if not _resolve_image(project_root, component, pin, "zoomed"):
            uncached += 1
    return total, uncached


# ---------------------------------------------------------------------------
#  Context building
# ---------------------------------------------------------------------------


def _collect_all_media_refs(
    procedures: list,
    project_root: Path | None = None,
) -> list[dict]:
    """Collect every media ref dict across all procedures' steps + expected.

    Phase 6.2d: pulls through pack_extractors so the JSON shape is
    owned by the active rules pack. Returns a flat list of
    ``{component, pin, caption}`` dicts (the shape the downstream
    image-generation pre-flight consumes). Skips media items without
    a component since those can't drive image lookup.
    """
    from procedo.core import pack_extractors

    pack_extractors.prime_export_rows(list(procedures), project_path=project_root)
    refs: list[dict] = []
    for proc in procedures:
        items: list = []
        for step in pack_extractors.extract_export_steps(proc, project_path=project_root):
            items.extend(step.media)
        for cond in pack_extractors.extract_export_expected(proc, project_path=project_root):
            items.extend(cond.media)
        for m in items:
            if not m.component:
                continue
            refs.append(
                {
                    "component": m.component,
                    "pin": m.pin,
                    "caption": m.caption,
                }
            )
    return refs


def _type_label(item) -> str:
    """``"psu"`` or ``"iobox, iobox-sim"`` when a subtype exists."""
    return f"{item.type}, {item.subtype}" if item.subtype else item.type


def _eng_rating(value: float, unit: str) -> str:
    """Render an SI-base rating in engineering form: ``0.4`` A -> ``400.0 mA``,
    ``24.0`` V -> ``24.0 V``. Sub-unit values scale to milli/micro so the
    report shows the magnitude the operator declared (mA channels read as
    mA, not 0.00x A)."""
    for factor, prefix in ((1.0, ""), (1e-3, "m")):
        if value >= factor:
            scaled = round(value / factor, 9)
            return f"{scaled} {prefix}{unit}"
    return f"{round(value / 1e-6, 9)} u{unit}"


def _channel_requirements_text(channels: tuple) -> str:
    """Operator-readable channel-requirements summary for one item.

    ``"CH1: max 24.0 V / 2.0 A"`` per rated channel ("; "-joined);
    ``"4 channels"`` when only bare channels are declared (no
    ratings); ``""`` when no channels at all. Ratings are SI base
    units (V/A/W), ``None`` = undeclared → omitted.
    """
    if not channels:
        return ""
    parts = []
    rated = False
    for ch in channels:
        ratings = [
            _eng_rating(value, unit)
            for value, unit in ((ch.max_voltage, "V"), (ch.max_current, "A"), (ch.max_power, "W"))
            if value is not None
        ]
        if ratings:
            rated = True
            parts.append(f"CH{ch.channel}: max " + " / ".join(ratings))
        else:
            parts.append(f"CH{ch.channel}")
    if not rated:
        return f"{len(channels)} channel" + ("s" if len(channels) != 1 else "")
    return "; ".join(parts)


def format_equipment_line(item) -> str:
    """One operator-readable line per ``EquipmentRequirement`` —
    ``"PSU1 (psu) — CH1: max 24.0 V / 2.0 A"``. Shared seam for the
    Word context build and the Markdown summary export."""
    label = f"{item.id} ({_type_label(item)})"
    channels = _channel_requirements_text(item.channels)
    return f"{label} — {channels}" if channels else label


def _build_equipment_list(items: list) -> list[dict]:
    """Map ``EquipmentRequirement`` domain items into template dicts.

    Takes pack-extractor domain objects (``.id``, ``.type``,
    ``.subtype``, ``.channels`` with ``.channel``/ratings), never raw
    JSON — the pack owns the JSON shape. ``channels`` carries the
    per-channel requirements summary (see
    ``_channel_requirements_text``); ``type`` folds the subtype in so
    the template's ``({{ eq.type }})`` shows it.
    """
    result = []
    for item in items:
        entry: dict[str, Any] = {
            "id": item.id,
            "type": _type_label(item),
            "channels": _channel_requirements_text(item.channels),
        }
        if item.subtype:
            entry["subtype"] = item.subtype
        result.append(entry)
    return result


def _compute_total_equipment(
    procedures: list,
    project_root: Path | None,
) -> list[dict]:
    """Deduplicate equipment across all selected procedures (keyed by id+type).

    Ratings are MAX-MERGED across tests: if one test needs ELOAD1 at 4 A and
    another at 10 A, the project summary shows the worst-case 10 A (per
    channel, per rating). Channel sets are unioned. The merge itself is
    single-sourced in :mod:`procedo.core.equipment_merge` (task #35).
    """
    from procedo.core import pack_extractors
    from procedo.core.equipment_merge import merge_equipment_requirements

    items: list = []
    for proc in procedures:
        items.extend(
            pack_extractors.extract_equipment_requirements(
                proc,
                project_path=project_root,
            )
        )
    return _build_equipment_list(merge_equipment_requirements(items))


def compressed_png(data: bytes) -> bytes:
    """*data* re-saved as a zlib-compressed PNG (lossless), when that is smaller.
    Some instruments write their screenshots uncompressed (~1.8 MB for a
    1024x600 scope capture, ~60 KB once compressed)."""
    import io

    from PIL import Image  # lazy, like _jpeg_descriptor

    out = io.BytesIO()
    with Image.open(io.BytesIO(data)) as im:
        im.save(out, "PNG")
    return out.getvalue() if out.tell() < len(data) else data


_JPEG_MAX_WIDTH = 900  # px \u2014 measured on real cache renders: q88@900px with
_JPEG_QUALITY = 88  # subsampling=0 is ~32% of the PNG bytes; a ~10% target
#                        needs 600px + q80 + subsampling=2 (softer, ~200 DPI)


def _jpeg_descriptor(img_path: Path):
    """Re-encode *img_path* into an in-memory JPEG file-like descriptor for
    ``InlineImage``: alpha composited onto white, RGB, downscaled to
    ``_JPEG_MAX_WIDTH`` (LANCZOS, only when wider), quality ``_JPEG_QUALITY``,
    chroma subsampling off (4:4:4 — board renders carry colored text and fine
    edges that 4:2:0 smears; the small size increase is accepted).
    """
    import io

    from PIL import Image  # lazy, like docxtpl for the Word pipeline

    with Image.open(img_path) as im:
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
    if flat.width > _JPEG_MAX_WIDTH:
        height = max(1, round(flat.height * _JPEG_MAX_WIDTH / flat.width))
        flat = flat.resize((_JPEG_MAX_WIDTH, height), Image.LANCZOS)
    buf = io.BytesIO()
    flat.save(buf, format="JPEG", quality=_JPEG_QUALITY, subsampling=0)
    buf.seek(0)
    return buf


def _build_media_list(
    media_items: tuple,
    project_root: Path | None,
    doc,
    jpeg: bool = True,
) -> list[dict]:
    """Build list of media dicts for one step / expected condition.

    Phase 6.2d: takes a tuple of pack-domain ``MediaRef`` objects
    (attribute access \u2014 ``.type``, ``.caption``, ``.component``,
    ``.pin``) rather than raw JSON dicts. The pack owns the JSON
    shape; this function only knows the stable domain.

    When *doc* is a ``DocxTemplate`` instance and images exist, returns
    ``InlineImage`` objects for embedding. Otherwise returns metadata
    with resolved paths.

    *jpeg* (the sidecar's ``jpeg_images``, default True) re-encodes each
    image as an in-memory JPEG (see :func:`_jpeg_descriptor`) before
    ``InlineImage``; False keeps the original PNG path byte-identical.
    """
    from docx.shared import Mm
    from docxtpl import InlineImage

    result = []
    for m in media_items:
        component = m.component or ""
        pin = m.pin  # already str | None after extractor coercion

        built_caption = location_label(component, pin)
        entry: dict[str, Any] = {"caption": built_caption or m.caption or ""}

        if project_root and component:
            for variant in ("wide", "zoomed"):
                img_path = _resolve_image(project_root, component, pin, variant)
                if img_path and img_path.is_file():
                    try:
                        descriptor = _jpeg_descriptor(img_path) if jpeg else str(img_path)
                        entry[f"{variant}_image"] = InlineImage(doc, descriptor, width=Mm(75))
                    except Exception:
                        entry[f"{variant}_image"] = f"\u26a0 Image error: {built_caption}"
                else:
                    entry[f"{variant}_image"] = f"\u26a0 Image not found: {built_caption}"
        result.append(entry)
    return result


def images_included(sidecar: dict) -> bool:
    """The sidecar's ``include_images`` (absent = False): board images are opt-in,
    both their generation and their place in the document."""
    return bool(sidecar.get("include_images", False))


def alphabetical_order(sidecar: dict) -> bool:
    """The sidecar's ``alphabetical_order`` (absent = False): tests are written
    sorted by id instead of in the order they were picked."""
    return bool(sidecar.get("alphabetical_order", False))


def titles_shown(sidecar: dict) -> bool:
    """The sidecar's ``show_titles`` (absent = True): a test's title appears in
    its heading, ``id (title)``; hidden, the heading is the id alone."""
    return bool(sidecar.get("show_titles", True))


PATH_SEP = " \u203a "  # the separator of a group's parent path in a heading: "A \u203a B \u203a "


def group_sections(names: list[str], project_root: Path | None) -> list[dict[str, Any]]:
    """The export's tests arranged by the project's test groups (``core.test_groups``).

    *names* are the exported tests' folder names. Returns sections in document order, each
    ``{"name", "path", "depth", "parent", "count", "ungrouped", "names"}``: a group with tests in
    it (its own ``names`` are the direct ones, ``count`` all below it, sub-groups included),
    parents before children, tests in tree order; the tests in no group come last in a section
    named "Not in a group". With no groups (no file, or only ungrouped tests) it is one section
    with an empty name, depth 0 and *names* unchanged: the export reads as it always did."""
    from . import test_groups

    if project_root is None:
        return [_section("", [], 0, list(names), len(names), False)]
    tree = test_groups.load(Path(project_root), list(names))
    if not any(isinstance(node, test_groups.Group) for _, node in test_groups.walk(tree)):
        return [_section("", [], 0, list(names), len(names), False)]
    sections = []
    for parent, node in test_groups.walk(tree):
        if not isinstance(node, test_groups.Group):
            continue
        path = (*parent, node.name)
        total = len(test_groups.tests_in(tree, path))
        if total:
            own = [c for c in node.children if isinstance(c, str)]
            sections.append(_section(node.name, list(path), len(path), own, total, False))
    loose = [c for c in tree.children if isinstance(c, str)]
    if loose:
        sections.append(_section("Not in a group", [], 1, loose, len(loose), True))
    return sections


def _section(name, path, depth, names, count, ungrouped) -> dict[str, Any]:
    return {
        "name": name,
        "path": path,
        "depth": depth,
        "parent": "".join(p + PATH_SEP for p in path[:-1]),
        "count": count,
        "count_text": f"{count} test" + ("" if count == 1 else "s"),
        "ungrouped": ungrouped,
        "names": names,
    }


def _fill_sections(
    sections: list[dict[str, Any]], by_name: dict[str, dict], sort_key=None
) -> list[dict]:
    """Give each section its ``tests`` (the entries of its ``names``, optionally sorted), renumber
    ``idx`` in document order and return the flat list; ``names`` is dropped."""
    flat: list[dict] = []
    for section in sections:
        entries = [by_name[n] for n in section.pop("names") if n in by_name]
        if sort_key is not None:
            entries.sort(key=sort_key)
        section["tests"] = entries
        flat.extend(entries)
    for idx, test in enumerate(flat, start=1):
        test["idx"] = idx
    return flat


def build_template_context(
    sidecar: dict,
    procedures: list,
    project_root: Path | None,
    doc,
    schema: dict | None = None,
) -> dict:
    """Merge sidecar metadata with fresh test procedure data.

    *doc* is the ``DocxTemplate`` instance (needed for ``InlineImage``).
    *project_root* is the project's root directory (for image resolution).
    The sidecar's ``include_images`` (see :func:`images_included`) decides
    whether steps carry media at all, its ``alphabetical_order`` (see
    :func:`alphabetical_order`) whether tests are sorted by id, its
    ``show_titles`` (see :func:`titles_shown`) whether headings carry the title; its ``jpeg_images`` (absent = True;
    engine-owned, not a schema field) selects JPEG re-encoding of them.
    """
    from procedo.core import pack_extractors

    jpeg = bool(sidecar.get("jpeg_images", True))
    images = images_included(sidecar)
    show_titles = titles_shown(sidecar)

    # One batched project-venv call renders every procedure's export
    # rows up front; the per-procedure calls below then hit the cache.
    pack_extractors.prime_export_rows(list(procedures), project_path=project_root)

    tests = []
    for idx, proc in enumerate(procedures, start=1):
        # Phase 6.2c: read id / description / requirement via the pack's
        # extract_meta extractor. v2.0.1 puts `requirement` under
        # `meta.requirement` (not top-level) \u2014 the legacy
        # `proc.get("requirement", "")` always returned "" on v2.0.1
        # procedures. Dropped `name` (no such field in v2.0.1) \u2014 it
        # was always "" too.
        meta = pack_extractors.extract_meta(proc, project_path=project_root)
        # The extractor returns a ProcedureMeta or None (latter only
        # when no pack is registered, which is a degenerate state).
        # Coerce to an empty-summary in that case rather than scatter
        # None-handling through the template build.
        m_id = meta.id if meta is not None else ""
        m_title = meta.title if (meta is not None and show_titles) else ""
        m_desc = meta.description if meta is not None else ""
        m_req = meta.requirement if meta is not None else ""

        test_entry: dict[str, Any] = {
            "idx": idx,
            "id": m_id,
            "requirement": m_req,
            "description": m_desc,
            "equipment": _build_equipment_list(
                pack_extractors.extract_equipment_requirements(
                    proc,
                    project_path=project_root,
                ),
            ),
            "steps": [],
            "expected": [],
        }

        # Build display heading \u2014 just the id (v2.0.1 has no separate
        # `name` field).
        test_entry["heading"] = (
            f"{m_id} ({m_title})" if (m_title and m_title != m_id) else m_id
        )  # "id (title)" when a distinct title is present, else id
        test_entry["title"] = m_title

        # Steps + Expected: read via pack_extractors' export view, which
        # expands block steps (for/table/alloc) to every canonical line
        # so macro procedures export exactly as they read in
        # procedure_text.md. Row idx is the canonical step number
        # ("" for @-macro lines); older wheels fall back to the flat
        # per-step view inside the wrapper.
        for step in pack_extractors.extract_export_steps(proc, project_path=project_root):
            test_entry["steps"].append(
                {
                    "idx": step.idx,
                    "text": step.text,
                    "media": _build_media_list(step.media, project_root, doc, jpeg=jpeg)
                    if images
                    else [],
                }
            )

        for cond in pack_extractors.extract_export_expected(proc, project_path=project_root):
            test_entry["expected"].append(
                {
                    "text": cond.text,
                    "media": _build_media_list(cond.media, project_root, doc, jpeg=jpeg)
                    if images
                    else [],
                }
            )

        tests.append(test_entry)

    # Tests in the project's group order; ``sections`` carries the headings (see group_sections).
    # (a test's id is its folder name, which is what the groups file names)
    keys = [t["id"] or f"#{i}" for i, t in enumerate(tests)]
    sections = group_sections(keys, project_root)
    by_name = dict(zip(keys, tests, strict=True))
    alpha = (lambda t: t["id"].casefold()) if alphabetical_order(sidecar) else None
    tests = _fill_sections(sections, by_name, alpha)

    # Document-level metadata, schema-driven (single source: report_schema).
    # The execution payload (regenerated from the procedures) layers on top.
    from .report_schema import build_metadata_context, load_builtin_schema

    context: dict[str, Any] = build_metadata_context(schema or load_builtin_schema(), sidecar)
    context["total_equipment"] = _compute_total_equipment(procedures, project_root)
    context["tests"] = tests
    context["sections"] = sections
    return context


def build_markdown_context(procedures: list, project_root: Path | None) -> dict:
    """Context for a ``kind: "markdown"`` export package (Markdown summary).

    Byte-identical port of the legacy report-controller closure's data flow:
    each procedure is loaded (a failure becomes a "could not load" entry), the
    export-row cache is primed once with the loaded docs, then every procedure
    contributes heading, requirement, description, equipment lines
    (:func:`format_equipment_line` — the shared seam with the Word context)
    and the full-fidelity export rows for steps / success conditions. No
    docxtpl doc, no InlineImage, no cross-procedure equipment merge — the
    Markdown summary lists per-test equipment only. *procedures* are
    ``ProcedureInfo``-likes (``.name`` + ``.load_procedure_data()``).
    """
    from procedo.core import pack_extractors

    procs_data = []  # (display index, procedure, data-or-None)
    for idx, proc in enumerate(procedures, start=1):
        try:
            procs_data.append((idx, proc, proc.load_procedure_data()))
        except Exception:
            procs_data.append((idx, proc, None))

    pack_extractors.prime_export_rows(
        [d for _, _, d in procs_data if d is not None],
        project_path=project_root,
    )

    tests = []
    for idx, proc, data in procs_data:
        if data is None:
            tests.append({"idx": idx, "heading": proc.name, "load_failed": True})
            continue
        meta = pack_extractors.extract_meta(data, project_path=project_root)
        step_rows = pack_extractors.extract_export_steps(data, project_path=project_root)
        expected_rows = pack_extractors.extract_export_expected(data, project_path=project_root)
        equipment = pack_extractors.extract_equipment_requirements(data, project_path=project_root)
        # v2.0.1 has no top-level `name`; prefer the wheel-computed display name,
        # fall back to the folder name.
        heading = (meta.display_name if meta else "") or proc.name
        tests.append(
            {
                "idx": idx,
                "heading": heading,
                "load_failed": False,
                "requirement": meta.requirement if meta else "",
                "description": meta.description if meta else "",
                "equipment_lines": [format_equipment_line(item) for item in equipment],
                "steps": [{"idx": row.idx, "text": row.text} for row in step_rows],
                "expected": [{"text": row.text} for row in expected_rows],
            }
        )
    sections = group_sections([p.name for p in procedures], project_root)
    tests = _fill_sections(sections, {p.name: t for p, t in zip(procedures, tests, strict=True)})
    return {"tests": tests, "sections": sections}


# ---------------------------------------------------------------------------
#  Rendering
# ---------------------------------------------------------------------------


def render_word(
    template_path: Path,
    context: dict,
    output_path: Path,
) -> None:
    """Render *template_path* with *context* and save to *output_path*."""
    from docxtpl import DocxTemplate

    doc = DocxTemplate(str(template_path))
    doc.render(context, autoescape=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    log.info("Word document saved to %s", output_path)


def render_markdown_text(template_path: Path, context: dict) -> str:
    """Render the Jinja2 text template at *template_path* with *context*.

    Plain-text pipeline for ``kind: "markdown"`` export packages: no
    autoescaping, the template's trailing newline kept."""
    import jinja2  # lazy, like docxtpl for the Word pipeline

    env = jinja2.Environment(keep_trailing_newline=True, autoescape=False)
    return env.from_string(template_path.read_text(encoding="utf-8")).render(context)


def render_markdown(template_path: Path, context: dict, output_path: Path) -> None:
    """:func:`render_markdown_text` saved to *output_path* exactly the way the
    legacy controller wrote it (``Path.write_text`` with UTF-8 and platform
    newline translation) so the golden-pinned bytes survive the controller swap."""
    text = render_markdown_text(template_path, context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    log.info("Markdown summary saved to %s", output_path)


def render_report(
    template_path: Path,
    sidecar: dict,
    procedures: list,
    project_root: Path | None,
    output_path: Path,
) -> None:
    """One-call report render: build the context (binding any InlineImages to
    the template's OWN DocxTemplate) and save. The single shared entry point for
    both the main app and the workflow editor."""
    from docxtpl import DocxTemplate

    doc = DocxTemplate(str(template_path))
    context = build_template_context(
        sidecar,
        procedures,
        project_root,
        doc,
        schema=get_template_schema(template_path),
    )
    doc.render(context, autoescape=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    log.info("Word report saved to %s", output_path)
