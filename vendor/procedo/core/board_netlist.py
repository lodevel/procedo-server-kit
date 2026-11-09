"""What both board browsers (Bench's explorer, Studio's netlist panel) list of a board's
components and nets, filtered, the project's hidden net-name prefixes, and what a
pictured location is called beside its picture. Qt-free.

A component is ``{"refdes", "side", "pins": [{"name", "net"} | name]}``, a net
``{"net", "nodes": [{"refdes", "pin"}]}`` — the board inventory the ODB++ tool lists.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from . import config_manager

SECTION = "net_explorer"  # config.json: {"net_explorer": {"hide_prefixes": [...]}}
DEFAULT_HIDE_PREFIXES = ("Net",)  # Altium's auto-generated net names


def read_hide_prefixes(config_dir: Path | None) -> list[str]:
    """The net-name prefixes the browsers hide by default, from ``config.json`` in
    *config_dir* (a project's or a template's ``config`` folder; None: no project), else
    :data:`DEFAULT_HIDE_PREFIXES`."""
    section = (
        config_manager.read_config_section(Path(config_dir), SECTION)
        if config_dir is not None
        else {}
    )
    stored = section.get("hide_prefixes") if isinstance(section, dict) else None
    return [str(p) for p in stored] if isinstance(stored, list) else list(DEFAULT_HIDE_PREFIXES)


def write_hide_prefixes(config_dir: Path, prefixes) -> None:
    """Store *prefixes* in *config_dir*; nothing is written when they read so already (a
    focus-out must not wake a file watcher)."""
    prefixes = [str(p) for p in prefixes]
    if prefixes != read_hide_prefixes(config_dir):
        config_manager.write_config_section(Path(config_dir), SECTION, {"hide_prefixes": prefixes})


def pin_name(pin) -> str:
    return (pin.get("name") if isinstance(pin, dict) else str(pin)) or ""


def pin_net(pin) -> str:
    return (pin.get("net") if isinstance(pin, dict) else "") or ""


# archive path -> (its size and time when read, {refdes: [(pin name, net)]})
_BOARD_PINS: dict[str, tuple[tuple, dict]] = {}


def board_pins(archive: Path | None, list_components=None) -> dict:
    """``{refdes: [(pin name, net)]}`` of the board in *archive*, read with
    ``list_components()`` once per version of the file — slow (the ODB tool, or its
    cache), so only on a background thread. Without ``list_components``, what was read
    already, with no I/O at all (the GUI thread's way); empty when nothing was.

    A listing that fails is no netlist (the pictures never depend on it) and is asked
    again next time; an empty one is kept: the listing already answers every failure
    of the ODB tool with an empty list, and asking again would rerun the tool each step."""
    if archive is None:
        return {}
    key = str(archive)
    if list_components is None:
        return _BOARD_PINS.get(key, ((), {}))[1]
    try:
        stat = Path(archive).stat()
    except OSError:
        return {}
    version = (stat.st_size, stat.st_mtime_ns)
    known = _BOARD_PINS.get(key)
    if known is None or known[0] != version:
        try:
            pins = {
                c["refdes"]: [(pin_name(p), pin_net(p)) for p in c.get("pins") or ()]
                for c in list_components()
                if isinstance(c, dict) and c.get("refdes")
            }
        except Exception:  # noqa: BLE001 — a damaged listing: no netlist, never no pictures
            return {}
        known = _BOARD_PINS[key] = version, pins
    return known[1]


def location_net(pins: dict, component: str, pin) -> str:
    """The net at a pictured location, when there is exactly one: pin *pin*'s net, or —
    no pin named — the net of a part whose pins all sit on one net (a test point). ""
    for a part with several nets and no pin, or a location the board does not have."""
    nets = pins.get(component) or ()
    if pin not in (None, ""):
        nets = [(name, net) for name, net in nets if name == str(pin)]
    found = {net for _name, net in nets}
    return found.pop() if len(found) == 1 else ""


MENTION_START = (
    r"(?<![\w.#+\-])"  # a location mentioned in text starts a word: not inside a longer name
)
MENTION_END = r"(?!\w|\.\w)"  # ...and ends one: "U46.1" is not "U46" (a full stop after a name, a range dash are no part of it)
MENTION_PIN = r"(?:\s+pins?\s+|#)"  # between a mentioned location and its pin: "P15 pin 29", "P3#8"
_PAREN = r"\s*\(([^()]*)\)"  # the "(…)" directly after it
_PROSE = re.compile(r"[a-z]+[,:;]?")  # an English word: "(with 10:1 probe)" is a note, not a name
_PINNED = re.compile(
    r"[^\s()#]+" + MENTION_PIN + r"\d+(?:\s*[/-]\s*\d+)?"
)  # "P3#9", "P15 pin 29": a location


def location_name(text: str, component: str, pin) -> str:
    """The name *text* gives a location: the "(…)" directly after its first mention
    that has one — ``TP PMU.I0 (I#BOARD_PR_0)``, ``P15 pin 29/30 (+AVDD_PR_16V)``,
    ``J4 pins 6-10 (PWR_CAP)``, ``P3#8 (ENA)`` — verbatim; "" when none does. A note
    there — a lower-case word in it (``(with 10:1 probe)``) or no letter or digit
    (``(-)``) — or another location (``GND (P3#9)``) is not a name."""
    c = re.escape(component or "")
    if not c or not text:
        return ""
    if pin in (None, ""):
        mentions = [
            (m.start(), m.group(1))
            for m in re.finditer(MENTION_START + rf"(?:TP\s+)?{c}" + _PAREN, text)
        ]
    else:
        n = int(pin) if str(pin).isdigit() else None
        mentions = [
            (m.start(), m.group(4))
            for m in re.finditer(
                MENTION_START + c + MENTION_PIN + r"(\d+)(?:\s*([/-])\s*(\d+))?" + _PAREN, text
            )
            if _names_pin(n, *m.group(1, 2, 3))
        ]
    return next((name.strip() for _at, name in sorted(mentions) if _is_name(name)), "")


def _names_pin(pin: int | None, first: str, sep: str | None, last: str | None) -> bool:
    """``P15 pin 29``, ``pins 29/30`` (either) or ``pins 6-10`` (any between) names *pin*."""
    if sep == "/":
        return pin in (int(first), int(last))
    if sep == "-":
        return pin is not None and int(first) <= pin <= int(last)
    return pin == int(first)


def _is_name(text: str) -> bool:
    """A "(…)" names a location unless it is a note or another location (``GND (P3#9)``)."""
    words = text.split()
    return (
        any(ch.isalnum() for ch in text)
        and not any(_PROSE.fullmatch(w) for w in words)
        and not _PINNED.fullmatch(text.strip())
    )


def name_refs(refs: list, text: str) -> list:
    """Give each of *refs* (the media refs of a step's *text*) that has no ``name`` yet
    the one *text* gives its location (:func:`location_name`)."""
    for ref in refs:
        if not getattr(ref, "name", ""):
            ref.name = location_name(text or "", ref.component, ref.pin)
    return refs


def media_caption(pins: dict, ref) -> str:
    """A media ref's caption, then what its location is called: the procedure's name for
    it (``ref.name``, :func:`location_name`), else the one net there
    (:func:`location_net`) — ``TP9 · +28V_SW``; else the caption as it is."""
    name = getattr(ref, "name", "") or location_net(pins, ref.component, ref.pin)
    return f"{ref.caption} · {name}" if name and ref.caption else ref.caption


def link_caption(pins: dict, refs: list, component: str, pin) -> str:
    """The caption of a component link's picture (``pin`` as the link gives it): that of
    the shown ref it names (:func:`media_caption`), else the location's own."""
    ref = next(
        (
            r
            for r in refs
            if (r.component, str(r.pin or "")) == (component, str(pin or "")) and r.caption
        ),
        None,
    )
    if ref is None:
        ref = SimpleNamespace(component=component, pin=pin, caption=location_label(component, pin))
    return media_caption(pins, ref)


def location_label(component: str, pin) -> str:
    """A location as the pictures name it: ``P4`` or ``P4 pin 3``."""
    return component + (f" pin {pin}" if pin else "")


def filter_components(components: list, query: str) -> list:
    """Components whose refdes, any pin name, or any pin net contains *query*
    (case-insensitive). Empty query -> all."""
    q = (query or "").strip().lower()
    if not q:
        return list(components)
    out = []
    for c in components:
        if q in (c.get("refdes", "") or "").lower():
            out.append(c)
            continue
        for p in c.get("pins", []) or ():
            if q in pin_name(p).lower() or q in pin_net(p).lower():
                out.append(c)
                break
    return out


def is_hidden_net(name: str, hide_prefixes) -> bool:
    """True if *name* starts (case-insensitive) with any of *hide_prefixes*
    (e.g. ``["Net"]`` to hide Altium autogen names like ``NetD16_A``)."""
    n = (name or "").lower()
    return any(n.startswith((p or "").lower()) for p in (hide_prefixes or ()) if p)


def filter_nets(nets: list, query: str, hide_prefixes=(), show_hidden: bool = True) -> list:
    """Nets whose name or any node (refdes/pin) contains *query*, with
    auto-named nets (*hide_prefixes*) dropped unless *show_hidden*. Empty
    query -> all."""
    q = (query or "").strip().lower()
    out = []
    for n in nets:
        name = n.get("net", "") or ""
        if not show_hidden and is_hidden_net(name, hide_prefixes):
            continue
        if not q or q in name.lower():
            out.append(n)
            continue
        for nd in n.get("nodes", []) or ():
            if (
                q in (nd.get("refdes", "") or "").lower()
                or q in str(nd.get("pin", "") or "").lower()
            ):
                out.append(n)
                break
    return out
