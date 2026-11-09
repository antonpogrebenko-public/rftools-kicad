"""The plugin's window and report as HTML in rftools.io's design.

kicad-plugin-site-design, spec ``kicad-plugin/presentation``. This module only
renders: every figure, note and decision comes from ``dialog_logic.DialogState``,
exactly as the native dialog (``dialog.py``) shows them. The page is built from
the plugin's own files (``ui/``): the site's tokens and logo (copied by
``scripts/vendor_design.py``), the parts in ``plugin.css``, and the site's fonts,
inlined as data URIs. Nothing is loaded over the network.

Two uses:

- the window (``controls=True``): one step at a time, with the actions the
  page's script (``ui/bridge.js``) posts back to :func:`handle` (task 5.3);
- the browser report (``controls=False``): every section in one document, with
  no script and no control.
"""
from __future__ import annotations

import base64
import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from rftools_kicad import __version__
from rftools_kicad.dialog_logic import (
    MANUAL,
    OK,
    DialogState,
)
from rftools_kicad.settings import redact

UI_DIR = Path(__file__).resolve().parent / "ui"
TITLE = "rftools.io board calculations"
PRIVACY_URL = "https://rftools.io/privacy/"

STEPS = ("layers", "classes", "results")
STEP_LABELS = {"layers": "Layer model", "classes": "Net classes", "results": "Results"}
REGIONS = ("header", "main", "footer", "panel")
#: None, or the panel shown over the window.
PANELS = ("key", "confirm_run", "write", "restore", "values", "notice")

_FONTS = (
    ("Instrument Sans", "InstrumentSans-latin-wght.woff2", "400 700"),
    ("IBM Plex Mono", "IBMPlexMono-latin-400.woff2", "400"),
    ("IBM Plex Mono", "IBMPlexMono-latin-500.woff2", "500"),
    ("IBM Plex Mono", "IBMPlexMono-latin-600.woff2", "600"),
)


@dataclass
class Session:
    """What the window shows: the state, the step, an open panel and its subject."""

    state: DialogState
    step: str = "layers"
    panel: str | None = None
    #: The text of a notice, or of the run confirmation.
    notice: str = ""
    notice_error: bool = False
    #: The netclasses.Preview a write, restore or values panel shows.
    preview: Any = None


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# ── Assets ───────────────────────────────────────────────────────────────────


def _read(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _styles() -> str:
    faces = []
    for family, name, weight in _FONTS:
        data = base64.b64encode((UI_DIR / "fonts" / name).read_bytes()).decode("ascii")
        faces.append(
            f'@font-face{{font-family:"{family}";font-weight:{weight};font-style:normal;'
            f'font-display:block;src:url(data:font/woff2;base64,{data}) format("woff2")}}'
        )
    return "\n".join(faces) + "\n" + _read("tokens.css") + "\n" + _read("plugin.css")


def _bridge() -> str:
    path = UI_DIR / "bridge.js"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ── Page ─────────────────────────────────────────────────────────────────────


def page(
    session: Session,
    *,
    theme: str,
    controls: bool = True,
    notes: Sequence[str] = (),
    budget_line: str | None = None,
) -> str:
    """The whole document: *theme* is "light", "dark" or "auto" (the system's)."""
    regions = render_regions(session, controls=controls, notes=notes, budget_line=budget_line)
    body_class = "" if controls else ' class="doc"'
    parts = [
        "<!doctype html>",
        f'<html lang="en" data-theme="{e(theme)}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{e(TITLE)}</title><style>{_styles()}</style></head>",
        f"<body{body_class}>{_read('logo.svg')}<div class=\"win\">",
        f'<header class="top" id="r-header">{regions["header"]}</header>',
        f'<main class="main" id="r-main">{regions["main"]}</main>',
        f'<footer class="foot" id="r-footer">{regions["footer"]}</footer>',
        f'<div id="r-panel">{regions["panel"]}</div>',
        "</div>",
    ]
    if controls:
        parts.append(f"<script>{_bridge()}</script>")
    parts.append("</body></html>\n")
    return _redacted(session, "".join(parts))


def render_regions(
    session: Session,
    *,
    controls: bool = True,
    notes: Sequence[str] = (),
    budget_line: str | None = None,
) -> dict[str, str]:
    """The inner HTML of each region in :data:`REGIONS`."""
    state = session.state
    if controls:
        main = {"layers": _layers, "classes": _classes, "results": _results}[session.step](
            session, controls=True
        )
    else:
        main = _report_main(session, notes, budget_line)
    regions = {
        "header": _header(session, controls),
        "main": main,
        "footer": _footer(session, budget_line) if controls else _report_footer(),
        "panel": _panel(session) if controls and session.panel else "",
    }
    key = state.services.settings.api_key
    return {name: redact(text, (key,)) if key else text for name, text in regions.items()}


def _redacted(session: Session, text: str) -> str:
    key = session.state.services.settings.api_key
    return redact(text, (key,)) if key else text


# ── Header ───────────────────────────────────────────────────────────────────


def _header(session: Session, controls: bool) -> str:
    state = session.state
    model = state.model
    thickness = (
        f" · {model.board_thickness_nm / 1e6:.3f} mm" if model.board_thickness_nm else ""
    )
    parts = [
        '<div class="brand"><svg class="mark"><use href="#rf-mark"/></svg>'
        '<svg class="words"><use href="#rf-wordmark"/></svg>'
        '<span class="sep">/</span><span class="name">Board calculations</span></div>',
        f'<span class="chip">{len(model.copper)} copper layers{e(thickness)}</span>',
        '<span class="spacer"></span>',
    ]
    if controls:
        steps = []
        for index, step in enumerate(STEPS, start=1):
            on = ' class="on"' if step == session.step else ""
            steps.append(
                f'<button type="button"{on} data-action="step" data-step="{step}">'
                f"<b>{index}</b>{e(STEP_LABELS[step])}</button>"
            )
        parts.append(f'<nav class="seg">{"".join(steps)}</nav><span class="spacer"></span>')
    parts.append(_allowance(state, controls))
    return "".join(parts)


def _allowance(state: DialogState, controls: bool) -> str:
    usage = state.usage or {}
    remaining, allowance = usage.get("remaining"), usage.get("allowance")
    if not state.has_key:
        rows = '<div class="row"><span class="eyebrow">No key yet</span></div>'
    elif remaining is None or not allowance:
        rows = (f'<div class="row"><span class="eyebrow">{e(usage.get("tier") or "Key")}</span>'
                "</div><div class=\"row\">Check the allowance</div>")
    else:
        cells = min(int(allowance), 10)
        filled = round(cells * max(0, int(remaining)) / int(allowance))
        meter = "".join('<i class="f"></i>' if i < filled else "<i></i>" for i in range(cells))
        reset = str(usage.get("resetAt") or "")[:10]
        rows = (
            f'<div class="row"><span class="eyebrow">{e(usage.get("tier") or "")}</span>'
            f'<div class="meter">{meter}</div></div>'
            f'<div class="row"><strong>{e(remaining)} of {e(allowance)}</strong> calls left'
            + (f" · resets {e(reset)}" if reset else "") + "</div>"
        )
    if controls:
        return f'<button type="button" class="allow" data-action="key">{rows}</button>'
    return f'<div class="allow">{rows}</div>'


# ── Step 1: the layer model ──────────────────────────────────────────────────


def _layers(session: Session, controls: bool) -> str:
    state = session.state
    rows = []
    for row in state.layer_rows():
        if controls:
            plane = (
                f'<button type="button" class="sw" aria-pressed="{str(row.plane).lower()}" '
                f'aria-label="{e(row.name)} is a reference plane" data-action="plane" '
                f'data-layer="{e(row.name)}" data-plane="{0 if row.plane else 1}"></button>'
            )
        else:
            plane = "yes" if row.plane else "no"
        problems = "".join(f'<small class="warn-text">{e(p)}</small>' for p in row.problems)
        rows.append(
            f"<tr><td>{plane}</td><td class=\"lname\">{e(row.name)}</td>"
            f'<td class="desc">{e(row.text)}{problems}</td></tr>'
        )
    table = (
        '<div class="panel scroll"><table class="tbl"><thead><tr><th>Plane</th>'
        "<th>Copper layer</th><th>Computed as</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
    )
    other = [p for p in state.problems()
             if not any(p in r.problems for r in state.layer_rows())]
    callout = (
        f'<div class="callout"><span>{e(state.plane_source_text())}</span>'
        '<span class="spacer"></span>'
        + ('<button type="button" class="btn link" data-action="reset_planes">'
           "Reset to the proposal</button>" if controls else "")
        + "</div>"
    )
    warnings = "".join(f'<div class="callout warn">{e(p)}</div>' for p in other)
    head = (
        '<div class="head"><h1>Layer model</h1><p>Check how each copper layer will be '
        "computed. Nothing is sent until you run.</p></div>" if controls
        else '<div class="head"><h2>Layer model</h2></div>'
    )
    return (
        f"{head}<div class=\"layers\"><div class=\"panel stack\">"
        f'<div class="eyebrow">Stackup · from KiCad</div>{stackup_svg(state)}</div>'
        f'<div style="display:flex;flex-direction:column;gap:14px;min-width:0">'
        f"{table}{callout}{warnings}</div></div>"
    )


def stackup_svg(state: DialogState) -> str:
    """The board's stackup as a cross-section, top to bottom, not to scale."""
    layers = list((state.model.stackup or {}).get("layers") or [])
    planes = set(state.model.planes)
    width, left, bar, label_x = 420, 4, 210, 226
    y, out = 6, []
    for layer in layers:
        kind, name = layer.get("type"), str(layer.get("name", ""))
        if kind == "soldermask":
            out.append(f'<rect x="{left}" y="{y}" width="{bar}" height="6" fill="var(--muted)"/>')
            text = f"{name} · {_um(layer.get('thicknessNm'))}"
            out.append(_label(label_x, y + 8, text, "subtle"))
            y += 16
        elif kind == "copper":
            if name in planes:
                out.append(f'<rect x="{left}" y="{y}" width="{bar}" height="6" '
                           'fill="var(--foreground)"/>')
                out.append(_label(label_x, y + 8, name, "foreground", "plane"))
            else:
                for x, w in ((34, 36), (100, 26), (134, 26)):
                    out.append(f'<rect x="{left + x}" y="{y}" width="{w}" height="7" '
                               'fill="none" stroke="var(--trace)" stroke-width="1.5"/>')
                text = f"{name} · {_um(layer.get('thicknessNm'))}"
                out.append(_label(label_x, y + 8, text, "foreground"))
            y += 18
        elif kind == "dielectric":
            plies = layer.get("sublayers") or []
            thick = sum(int(p.get("thicknessNm") or 0) for p in plies)
            height = max(28, min(150, thick / 9000))
            out.append(f'<rect x="{left}" y="{y - 4}" width="{bar}" height="{height:.0f}" '
                       'fill="url(#rf-hatch)" stroke="var(--border)"/>')
            ers = sorted({p.get("epsilonR") for p in plies if p.get("epsilonR") is not None})
            er = "/".join(f"{x:.2f}" for x in ers) or "?"
            text = f"{thick / 1e6:.3f} mm · εr {er}"
            out.append(_label(label_x, y - 4 + height / 2 + 4, text, "subtle"))
            y += height - 2
    y += 8
    out.append(_label(left, y + 10, "not to scale · thicknesses in mm unless stated", "subtle"))
    return (
        f'<svg viewBox="0 0 {width} {y + 16:.0f}" role="img" aria-label="The board\'s stackup">'
        '<defs><pattern id="rf-hatch" width="7" height="7" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="7" '
        'stroke="var(--graticule)" stroke-width="2.4"/></pattern></defs>'
        + "".join(out) + "</svg>"
    )


def _label(x: float, y: float, text: str, colour: str, tag: str = "") -> str:
    extra = (f'<tspan fill="var(--pass)" dx="8">{e(tag)}</tspan>' if tag else "")
    return (f'<text x="{x}" y="{y:.0f}" font-size="11" fill="var(--{colour})">'
            f"{e(text)}{extra}</text>")


def _um(nm: Any) -> str:
    return "?" if nm is None else f"{int(nm) / 1000:g} µm"


# ── Step 2: the net classes ──────────────────────────────────────────────────

_FORM_FIELDS = (
    ("impedance_target", "Ω"), ("diff_target", "Ω"), ("cpw_gap", "mm"), ("current", "A"),
)


def _classes(session: Session, controls: bool) -> str:
    state = session.state
    layers = state.trace_layers()
    mask = (
        '<label class="check"><input type="checkbox" data-action="mask"'
        + (" checked" if state.solder_mask_cover else "")
        + "> Outer layers under their solder mask</label>"
    )
    grid = (
        '<label class="check">Manufacturing grid <input type="text" class="num" '
        f'data-action="grid" value="{e(state.grid_text)}"> mm</label>'
    )
    head_cells = ("Net class", "Routed on", "Also on", "Z₀", "Z₀ target Ω", "Zdiff target Ω",
                  "CPW gap mm", "Current A", "Via", "Class values (mm)")
    rows = []
    for nc in state.netclasses:
        form = state.forms[nc.name]
        name = e(nc.name)
        base = f'data-action="form" data-name="{name}"'
        options = "".join(
            f'<option value="{e(layer)}"{" selected" if layer == form.layer else ""}>'
            f"{e(layer)}</option>"
            for layer in layers
        ) or "<option>(no signal layer)</option>"
        also = "".join(
            f'<label><input type="checkbox" {base} data-field="extra_layers" '
            f'value="{e(layer)}"{" checked" if layer in form.extra_layers else ""}> '
            f"{e(layer)}</label>"
            for layer in layers if layer != form.layer
        )
        count = f" {len(form.extra_layers)}" if form.extra_layers else ""
        cells = [
            f'<label class="check"><input type="checkbox" {base} data-field="selected"'
            f'{" checked" if form.selected else ""}> {name}</label>',
            f'<select {base} data-field="layer"{"" if layers else " disabled"}>{options}</select>',
            f'<details class="also"><summary>Also on{count}…</summary>{also}</details>'
            if len(layers) > 1 else "",
            f'<input type="checkbox" {base} data-field="single_ended"'
            f'{" checked" if form.single_ended else ""} title="Impedance at the class\'s '
            'current track width">',
        ]
        for field, unit in _FORM_FIELDS:
            cells.append(f'<input type="text" class="num" {base} data-field="{field}" '
                         f'value="{e(getattr(form, field))}" aria-label="{e(unit)}">')
        cells.append(f'<input type="checkbox" {base} data-field="via"'
                     f'{" checked" if form.via else ""} title="Via impedance, capacitance, '
                     'inductance and current capacity">')
        cells.append(f'<span class="cv">{e(_class_values(nc))}</span>')
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    table = (
        '<div class="panel scroll"><table class="tbl"><thead><tr>'
        + "".join(f"<th>{e(h)}</th>" for h in head_cells)
        + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return (
        '<div class="head"><h1>Net classes</h1><p>Tick the classes to compute. A class is '
        "board-wide but an impedance width is per layer: the width written to a class is the "
        "one for the layer it is routed on. Targets, gap and current are optional; each "
        "computation is one API call, and repeats are answered from the cache for free.</p></div>"
        f'<div class="panel form-row">{mask}{grid}</div>{table}'
    )


def _class_values(nc: Any) -> str:
    def mm(nm: int | None) -> str:
        return "–" if nm is None else f"{nm / 1e6:g}"

    return (
        f"track {mm(nc.track_width_nm)} · pair {mm(nc.diff_pair_width_nm)}/"
        f"{mm(nc.diff_pair_gap_nm)} · via {mm(nc.via_diameter_nm)}/{mm(nc.via_drill_nm)}"
    )


# ── Step 3: the results ──────────────────────────────────────────────────────


def _results(session: Session, controls: bool) -> str:
    state = session.state
    head = (
        '<div class="head"><h1>Results</h1><p>Every number is the one rftools.io\'s web '
        "calculators give for the same inputs.</p></div>" if controls
        else '<div class="head"><h2>Results</h2></div>'
    )
    stale = (
        '<div class="callout warn">The layer model changed after this run: run again before '
        "applying widths.</div>" if state.results_stale else ""
    )
    if not state.results:
        body = '<p class="note">Nothing was computed.</p>'
    else:
        body = '<div class="cards">' + "".join(_card(row) for row in state.results) + "</div>"
    refusals = "".join(f'<div class="callout warn">{e(r)}</div>' for r in state.refusals)
    return head + stale + body + refusals


def split_value(text: str) -> tuple:
    """("0.4907 mm", "rftools thermal model (board-specific)") from the row's text."""
    head, sep, rest = text.partition(", ")
    if sep and any(ch.isdigit() for ch in head):
        return head, rest
    return text, ""


def _range_badge(value: str) -> str:
    return "in range" if value == "inside" else value


def _card(row: Any) -> str:
    ok = row.status == OK
    badge = (
        '<span class="badge muted">cached</span>' if row.cached
        else (f'<span class="badge pass">{e(_range_badge(row.range))}</span>' if ok and row.range
              else (f'<span class="badge warn">{e(row.status)}</span>' if not ok else ""))
    )
    value, method = split_value(row.suggested or row.achieved or "–")
    reads = []
    if row.suggested and row.achieved:
        reads.append(("Achieved", row.achieved))
    if row.current:
        reads.append(("Class now", row.current))
    reads_html = "".join(f"<div><span>{e(k)}</span><b>{e(v)}</b></div>" for k, v in reads)
    meta = " · ".join(e(x) for x in (row.formula, row.range and f"range {row.range}",
                                     row.engine, "cached" if row.cached else "") if x)
    return (
        f'<article class="panel card{"" if ok else " bad"}">'
        f'<div class="who"><span class="cls">{e(row.netclass)}</span>'
        f'<span class="on">{e(row.layer or "board")}</span></div>'
        f'<div class="what"><span class="eyebrow">{e(row.label)}</span>{badge}</div>'
        f'<div class="big">{e(value)}</div>'
        + (f'<div class="method">{e(method)}</div>' if method else "")
        + (f'<div class="reads">{reads_html}</div>' if reads_html else "")
        + (f'<div class="notes">{e(row.note)}</div>' if row.note else "")
        + (f'<div class="meta">{meta}</div>' if meta else "")
        + "</article>"
    )


# ── Footer ───────────────────────────────────────────────────────────────────


def _footer(session: Session, budget_line: str | None) -> str:
    state = session.state
    info = [f"<span>{e(budget_line or state.budget_line())}</span>"]
    info += [f'<span class="warn-text">{e(err)}</span>' for err in state.errors()]
    mode, reason = state.write_mode()
    if mode == MANUAL and reason:
        info.append(f'<span class="warn-text">{e(reason)}</span>')
    buttons = ['<button type="button" class="btn ghost" data-action="close">Close</button>']
    step = session.step
    if step == "layers":
        buttons.append('<button type="button" class="btn primary" data-action="step" '
                       'data-step="classes">Net classes →</button>')
    elif step == "classes":
        buttons.append('<button type="button" class="btn" data-action="step" '
                       'data-step="layers">← Layer model</button>')
        buttons.append('<button type="button" class="btn primary" data-action="run">'
                       "Run</button>")
    else:
        can_write = bool(state.results) and not state.results_stale and mode is not None
        manual = mode == MANUAL
        disabled = "" if can_write else " disabled"
        buttons.append('<button type="button" class="btn" data-action="restore">'
                       f'{"Previous values…" if manual else "Restore…"}</button>')
        buttons.append(f'<button type="button" class="btn" data-action="write"{disabled}>'
                       f'{"Values for Board Setup…" if manual else "Write to net classes…"}'
                       "</button>")
        buttons.append('<button type="button" class="btn primary" data-action="run">'
                       "Run again</button>")
    return (f'<div class="info">{"".join(info)}</div>'
            f'<div class="actions">{"".join(buttons)}</div>')


# ── Panels ───────────────────────────────────────────────────────────────────


def _panel(session: Session) -> str:
    state = session.state
    kind = session.panel
    if kind == "key":
        body = (
            '<div class="sh"><div class="eyebrow">API key</div><h2>Connect to rftools.io</h2>'
            f"<p>{e(state.key_status())}</p><p>The plugin computes through rftools.io's API, "
            "with the same calculators as the website. Computing again what was already "
            "computed is answered from the cache, for free.</p></div>"
            '<div class="keyrow">'
            f'<button type="button" class="btn primary" data-action="get_key" '
            f'data-href="{e(state.services.key_url)}">Get a free key ↗</button>'
            '<input type="password" id="key-field" autocomplete="off" '
            'placeholder="Paste your key · rfc_…" aria-label="API key">'
            '<button type="button" class="btn" data-action="save_key">Save key</button>'
            '<button type="button" class="btn ghost" data-action="check_allowance">'
            "Check allowance</button></div>"
            + (f'<div class="keyrow"><span class="{_notice_class(session)}">'
               f"{e(session.notice)}</span></div>" if session.notice else "")
            + '<div class="trust"><span><b>Only calculator inputs are sent</b>: a calculator '
            "name and numbers, never the board file, net, class or layer names, or the project."
            "</span><span><b>Your key stays on this computer</b>, in a settings file only you "
            "can read.</span></div>"
            '<div class="sf"><span class="spacer"></span><button type="button" class="btn" '
            'data-action="close_panel">Close</button></div>'
        )
    elif kind == "confirm_run":
        body = (
            f'<div class="sh"><h2>Run</h2><p>{e(session.notice)}</p></div>'
            '<div class="sf"><span class="spacer"></span>'
            '<button type="button" class="btn ghost" data-action="close_panel">Cancel</button>'
            '<button type="button" class="btn primary" data-action="run_confirmed">Run now'
            "</button></div>"
        )
    elif kind in ("write", "restore", "values"):
        body = _preview_panel(session)
    else:  # notice
        cls = "warn-text" if session.notice_error else ""
        body = (
            f'<div class="sh"><h2>{e(TITLE)}</h2><p class="{cls}">{e(session.notice)}</p></div>'
            '<div class="sf"><span class="spacer"></span><button type="button" '
            'class="btn primary" data-action="close_panel">OK</button></div>'
        )
    sheet = f'<div class="sheet" role="dialog" aria-modal="true">{body}</div>'
    return f'<div class="scrim">{sheet}</div>'


def _notice_class(session: Session) -> str:
    return "warn-text" if session.notice_error else "note"


def _preview_panel(session: Session) -> str:
    from rftools_kicad import netclasses as N

    preview = session.preview
    restore = preview.action == N.ACTION_RESTORE
    manual = bool(preview.manual)
    if manual:
        title = "Previous net-class values" if restore else "Net-class values for Board Setup"
        intro = f"{preview.manual} Keep this window open while you enter them, or copy them."
        heads = ("Net class", "Board Setup column", "Current", "Enter")
        labels = N.BOARD_SETUP_COLUMNS
    else:
        title = "Restore net classes" if restore else "Write to net classes"
        intro = (
            "Only the values marked below change: track width, differential-pair width and "
            "differential-pair gap. Every other setting of every class stays as it is. The "
            "previous values are recorded, so Restore can write them back."
        )
        heads = ("Net class", "Field", "Current", "Restored to" if restore else "Proposed")
        labels = N.FIELD_LABELS
    rows = []
    for netclass in preview.classes:
        for label, current, proposed, changes in netclass.rows(labels):
            cls = ' class="changed"' if changes else ""
            rows.append(f"<tr><td>{e(netclass.netclass)}</td><td>{e(label)}</td>"
                        f"<td>{e(current)}</td><td{cls}>{e(proposed)}</td></tr>")
    notes = "".join(f"<p>{e(n)}</p>" for n in preview.notes)
    if manual:
        actions = (
            '<button type="button" class="btn" data-action="copy_values">Copy values</button>'
            f'<span class="note">{e(session.notice)}</span><span class="spacer"></span>'
            '<button type="button" class="btn primary" data-action="close_panel">Close</button>'
        )
    else:
        count = len(preview.classes)
        verb = "Restore" if restore else "Write"
        actions = (
            '<span class="spacer"></span>'
            '<button type="button" class="btn ghost" data-action="close_panel">Cancel</button>'
            f'<button type="button" class="btn primary" data-action="confirm">{verb} {count} '
            f'class{"" if count == 1 else "es"}</button>'
        )
    return (
        f'<div class="sh"><h2>{e(title)}</h2><p>{e(intro)}</p></div>'
        '<div class="scroll"><table class="tbl"><thead><tr>'
        + "".join(f"<th>{e(h)}</th>" for h in heads)
        + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        + (f'<div class="sh">{notes}</div>' if notes else "")
        + f'<div class="sf">{actions}</div>'
    )


# ── The browser report ───────────────────────────────────────────────────────


def _report_main(session: Session, notes: Sequence[str], budget_line: str | None) -> str:
    state = session.state
    parts = [
        f'<div class="head"><h1>{e(TITLE)}</h1><p>Plugin {e(__version__)}. '
        f"{e(state.key_status())}</p></div>"
    ]
    parts += [f'<div class="callout warn">{e(n)}</div>' for n in notes]
    parts.append(_layers(session, controls=False))
    parts.append('<div class="head"><h2>Net classes</h2></div>')
    parts.append(_simple_table(
        ("Net class", "Track width", "Diff pair width / gap", "Clearance", "Via pad / drill"),
        [(nc.name, _mm(nc.track_width_nm),
          f"{_mm(nc.diff_pair_width_nm)} / {_mm(nc.diff_pair_gap_nm)}",
          _mm(nc.clearance_nm), f"{_mm(nc.via_diameter_nm)} / {_mm(nc.via_drill_nm)}")
         for nc in state.netclasses],
    ))
    parts.append('<div class="head"><h2>Planned calculations</h2></div>')
    parts.append(f"<p>{e(budget_line or state.budget_line())}</p>")
    parts.append('<p class="note">Sent to rftools.io for each: the calculator and these numbers, '
                 "never the board, its names or the project.</p>")
    parts.append(_simple_table(("Net class", "Layer", "Figure", "Calculator", "Inputs"),
                               state.planned_rows()))
    parts.append(_results(session, controls=False))
    return "".join(parts)


def _report_footer() -> str:
    return (
        '<div class="info"><span>Nothing was written to the board or the project. Privacy: '
        f'<a href="{e(PRIVACY_URL)}">{e(PRIVACY_URL)}</a></span></div>'
    )


def _simple_table(head: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    return (
        '<div class="panel scroll"><table class="tbl"><thead><tr>'
        + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def _mm(nm: int | None) -> str:
    return "not set" if nm is None else f"{nm / 1e6:g} mm"


# ── Actions: what the page posts back (task 5.3) ─────────────────────────────


class UiPort(Protocol):
    """What the window does for an action: the browser, the clipboard, closing."""

    def open_url(self, url: str) -> bool: ...

    def copy(self, text: str) -> bool: ...

    def close(self) -> None: ...

    def progress(self, done: int, count: int, label: str) -> None: ...


@dataclass(frozen=True)
class Update:
    """The regions to render again after an action, and whether to close."""

    regions: tuple = ()
    close: bool = False


ALL = REGIONS
_FORM_BOOLS = ("selected", "single_ended", "via")
_FORM_TEXTS = ("impedance_target", "diff_target", "cpw_gap", "current", "layer")


def start(state: DialogState) -> Session:
    """The window as it opens: on the layer model, with the key panel if there is no key."""
    return Session(state, panel=None if state.has_key else "key")


def handle(session: Session, message: Any, ui: UiPort) -> Update:
    """Do what the page asked, through the same DialogState calls as dialog.py."""
    action = _ACTIONS.get(str(message.get("action") or ""))
    return action(session, message, ui) if action else Update(())


def _notice(session: Session, text: str, error: bool = False) -> Update:
    session.panel, session.notice, session.notice_error = "notice", text, error
    session.preview = None
    return Update(("header", "footer", "panel"))


def _step(session: Session, message: Any, ui: UiPort) -> Update:
    step = str(message.get("step") or "")
    if step not in STEPS:
        return Update(())
    session.step, session.panel = step, None
    return Update(ALL)


def _plane(session: Session, message: Any, ui: UiPort) -> Update:
    layer = str(message.get("layer") or "")
    if layer not in session.state.model.copper_names():
        return Update(())
    session.state.set_plane(layer, str(message.get("plane")) in ("1", "true", "True"))
    return Update(("main", "footer"))


def _reset_planes(session: Session, message: Any, ui: UiPort) -> Update:
    session.state.reset_planes()
    return Update(("main", "footer"))


def _form(session: Session, message: Any, ui: UiPort) -> Update:
    """A field of the class form; the browser shows the value already, so main stays."""
    state, name, field = session.state, str(message.get("name")), str(message.get("field"))
    if name not in state.forms:
        return Update(())
    value = message.get("value")
    if field in _FORM_BOOLS:
        state.set_form(name, **{field: bool(value)})
    elif field in _FORM_TEXTS:
        state.set_form(name, **{field: "" if value is None else str(value)})
    elif field == "extra_layers":
        layers = state.trace_layers()
        state.set_form(name, extra_layers=tuple(x for x in (value or ()) if x in layers))
    else:
        return Update(())
    return Update(("footer",))


def _mask(session: Session, message: Any, ui: UiPort) -> Update:
    session.state.solder_mask_cover = bool(message.get("on"))
    return Update(("footer",))


def _grid(session: Session, message: Any, ui: UiPort) -> Update:
    session.state.grid_text = str(message.get("text") or "")
    return Update(("footer",))


def _run(session: Session, message: Any, ui: UiPort) -> Update:
    """dialog.on_run's first half: the free usage read, then the check."""
    state = session.state
    state.refresh_usage()
    ok, text = state.run_check()
    if not ok:
        return _notice(session, text)
    session.panel, session.notice, session.notice_error = "confirm_run", text, False
    return Update(("header", "footer", "panel"))


def _run_confirmed(session: Session, message: Any, ui: UiPort) -> Update:
    summary = session.state.run(lambda done, count, label: ui.progress(done, count, label or ""))
    session.step, session.panel, session.notice = "results", None, ""
    if summary.message:
        _notice(session, summary.message, error=True)
    return Update(ALL)


def _key(session: Session, message: Any, ui: UiPort) -> Update:
    session.panel, session.notice, session.notice_error = "key", "", False
    return Update(("panel",))


def _get_key(session: Session, message: Any, ui: UiPort) -> Update:
    """dialog.on_get_key: open the key link; when no browser opens, copy it."""
    url = session.state.services.key_url
    if ui.open_url(url):
        return Update(())
    how = ("It is copied to the clipboard; paste it into your browser." if ui.copy(url)
           else "Copy it into your browser.")
    session.panel, session.notice_error = "key", False
    session.notice = f"Your browser could not be opened from KiCad. {url} {how}"
    return Update(("panel",))


def _save_key(session: Session, message: Any, ui: UiPort) -> Update:
    saved, text = session.state.save_key(str(message.get("text") or ""))
    session.panel, session.notice, session.notice_error = "key", text, not saved
    return Update(("header", "footer", "panel"))


def _check_allowance(session: Session, message: Any, ui: UiPort) -> Update:
    session.state.refresh_usage()
    return Update(("header", "footer", "panel"))


def _show_preview(session: Session, preview: Any, reason: Any, panel: str) -> Update:
    """dialog._preview: a reason, the values to enter by hand, or the write to confirm."""
    if preview is None:
        return _notice(session, reason or "")
    session.preview, session.notice, session.notice_error = preview, "", False
    session.panel = "values" if preview.manual else panel
    return Update(("panel",))


def _write(session: Session, message: Any, ui: UiPort) -> Update:
    return _show_preview(session, *session.state.preview(), panel="write")


def _restore(session: Session, message: Any, ui: UiPort) -> Update:
    return _show_preview(session, *session.state.restore_preview(), panel="restore")


def _confirm(session: Session, message: Any, ui: UiPort) -> Update:
    """The user confirmed the preview the panel shows: state.apply, as dialog._write."""
    from rftools_kicad import netclasses as N

    preview = session.preview
    if preview is None or preview.manual or session.panel not in ("write", "restore"):
        return Update(())
    result = session.state.apply(preview, lambda shown: shown is preview)
    if result.cancelled:
        session.panel, session.preview = None, None
        return Update(("panel",))
    _notice(session, result.message, error=not result.applied)
    session.notice_error = result.status == N.STATUS_DAMAGED or not result.applied
    return Update(ALL)


def _copy_values(session: Session, message: Any, ui: UiPort) -> Update:
    if session.preview is None or not session.preview.manual:
        return Update(())
    copied = ui.copy(session.preview.manual_text())
    session.notice = "Copied." if copied else "The clipboard could not be opened."
    return Update(("panel",))


def _close_panel(session: Session, message: Any, ui: UiPort) -> Update:
    session.panel, session.notice, session.notice_error, session.preview = None, "", False, None
    return Update(("panel",))


def _open(session: Session, message: Any, ui: UiPort) -> Update:
    href = str(message.get("href") or "")
    if href.startswith("https://"):
        ui.open_url(href)
    return Update(())


def _close(session: Session, message: Any, ui: UiPort) -> Update:
    ui.close()
    return Update((), close=True)


_ACTIONS = {
    "ready": lambda session, message, ui: Update(()),
    "step": _step, "plane": _plane, "reset_planes": _reset_planes,
    "form": _form, "mask": _mask, "grid": _grid,
    "run": _run, "run_confirmed": _run_confirmed,
    "key": _key, "get_key": _get_key, "save_key": _save_key,
    "check_allowance": _check_allowance,
    "write": _write, "restore": _restore, "confirm": _confirm, "copy_values": _copy_values,
    "close_panel": _close_panel, "open": _open, "close": _close,
}


@dataclass(frozen=True)
class Reply:
    """What the window does with a page message: a script to run, close, or ready."""

    script: str | None = None
    close: bool = False
    ready: bool = False


def reply(session: Session, raw: str, ui: UiPort) -> Reply:
    """The page posted *raw* (JSON): handle it and say which regions to replace."""
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return Reply()
    if not isinstance(message, dict):
        return Reply()
    if message.get("action") == "ready":
        return Reply(ready=True)
    update = handle(session, message, ui)
    if update.close:
        return Reply(close=True)
    if not update.regions:
        return Reply()
    return Reply(script=update_script(session, update.regions))


def update_script(session: Session, names: Sequence[str]) -> str:
    """The script that replaces regions *names* with their current HTML."""
    regions = render_regions(session)
    return f"rftoolsUpdate({json.dumps({name: regions[name] for name in names})});"
