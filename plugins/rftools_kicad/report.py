"""The results as a local HTML page, for a KiCad whose Python cannot load wx (task 4.2).

``main.presenter`` falls back to :func:`present` when ``rftools_kicad.dialog``
cannot be imported, and the dialog falls back to it when wx imports but cannot
open a window. There is no form, so the run uses fixed defaults
(:meth:`DialogState.select_all_for_report`): every net class, on every trace
layer, the impedance at its current track width. No targets, currents or vias
(each needs a value only the dialog asks for), and forward figures only.

The page offers **no write control of any kind**: no form, input, button or
script. Net classes are written only from the dialog, after a preview and a
confirmation.

The run spends calls only when it fits the allowance the usage endpoint
reports (never metered). When it does not, or the allowance cannot be read,
only what the result cache already holds is shown, and the page says so.
"""
from __future__ import annotations

import html
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rftools_kicad import __version__
from rftools_kicad.dialog_logic import COLUMNS, DialogState
from rftools_kicad.settings import redact, settings_path

TITLE = "rftools.io board calculations"
PRIVACY_URL = "https://rftools.io/privacy/"

FULL_DIALOG = (
    "This is the report view: the plugin's dialog needs wxPython, which this Python "
    "could not load, so the page shows the impedance of every net class's track width "
    "on every signal layer and nothing else. Targets, currents, vias and writing "
    "widths to net classes are in the dialog. To get it: on Linux, install your "
    "distribution's wxPython package for the system python3 (python3-wxgtk4.0 on "
    "Debian and Ubuntu) and use that python3 as KiCad's plugin interpreter, since no "
    "other Python there has wx; on macOS and Windows, right-click the plugin in "
    "Preferences › Plugins › Action Plugins and choose Recreate Plugin Environment, "
    "which installs wxPython with the plugin."
)

_STYLE = """
:root{--fg:#1b1f24;--muted:#57606a;--line:#d0d7de;--bg:#fff;--warn:#9a3412;--ok:#116329}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9da7b3;--line:#3d444d;
--bg:#0d1117;--warn:#f0883e;--ok:#56d364}}
body{font:15px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);
max-width:72em;margin:2em auto;padding:0 1em}
h1{font-size:1.5em}h2{font-size:1.15em;margin-top:2em}
table{border-collapse:collapse;width:100%;margin:.5em 0;font-size:.92em}
th,td{border-bottom:1px solid var(--line);padding:.3em .5em;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}
.note,.muted{color:var(--muted)}.warn{color:var(--warn)}.ok{color:var(--ok)}
.scroll{overflow-x:auto}
"""


def present(
    model: Any,
    netclasses: list,
    services: Any,
    *,
    opener: Callable[[str], Any] | None = None,
    directory: str | None = None,
) -> int:
    """Compute the default figures, write the report, open it in the browser."""
    state = DialogState(model, netclasses, services)
    state.select_all_for_report()
    notes, budget_line = run_defaults(state)
    path = write_report(render(state, notes, budget_line), directory)
    if opener is None:
        from rftools_kicad.browser import open_url

        opener = open_url
    opener(Path(path).as_uri())
    print(f"{TITLE}: the report is at {path}", file=sys.stderr)
    return 0


def run_defaults(state: DialogState) -> tuple:
    """Run the report's figures within the allowance.

    Returns ``(notes for the page, the budget line as it stood before the run)``.
    """
    notes: list = []
    if not state.has_key:
        notes.append(
            f"No API key is set, so only results already cached are shown. Get a free key at "
            f"{state.services.key_url} and set it in the RFTOOLS_API_KEY environment variable, "
            f"or as \"apiKey\" in {settings_path()}."
        )
        line = state.budget_line()
        state.run(cached_only=True, skip_reason="Not computed: no API key is set.")
        return notes, line
    state.refresh_usage()
    budget = state.budget()
    line = state.budget_line()
    if budget.calls == 0 or budget.fits:
        state.run()
        return notes, line
    if budget.remaining is None:
        why = "the allowance remaining could not be read"
        if budget.usage_refusal is not None:
            why += f" ({budget.usage_refusal.message})"
    else:
        why = f"it needs {budget.calls} API calls and {budget.remaining} remain this month"
    notes.append(f"Only cached results are shown: {why}.")
    state.run(cached_only=True, skip_reason=f"Not computed: {why}.")
    return notes, line


def render(state: DialogState, notes: list | tuple = (), budget_line: str | None = None) -> str:
    """The report page: layer model, planned calculations, results, refusals. No controls."""
    e = html.escape
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{e(TITLE)}</title><style>{_STYLE}</style></head><body>",
        f"<h1>{e(TITLE)}</h1>",
        f'<p class="muted">Plugin {e(__version__)}. {e(state.key_status())}</p>',
        f'<p class="note">{e(FULL_DIALOG)}</p>',
    ]
    for note in notes:
        parts.append(f'<p class="warn">{e(note)}</p>')

    parts.append("<h2>Layer model</h2>")
    parts.append(f'<p class="note">{e(state.plane_source_text())}</p>')
    parts.append(_table(
        ("Copper layer", "Plane", "Computed as"),
        [(r.name, "yes" if r.plane else "no", r.text) for r in state.layer_rows()],
    ))
    problems = state.problems()
    if problems:
        parts.append('<ul class="warn">' + "".join(f"<li>{e(p)}</li>" for p in problems)
                     + "</ul>")

    parts.append("<h2>Net classes</h2>")
    parts.append(_table(
        ("Net class", "Track width", "Diff pair width / gap", "Clearance", "Via pad / drill"),
        [(
            nc.name, _mm(nc.track_width_nm),
            f"{_mm(nc.diff_pair_width_nm)} / {_mm(nc.diff_pair_gap_nm)}",
            _mm(nc.clearance_nm), f"{_mm(nc.via_diameter_nm)} / {_mm(nc.via_drill_nm)}",
        ) for nc in state.netclasses],
    ))

    parts.append("<h2>Planned calculations</h2>")
    parts.append(f"<p>{e(budget_line or state.budget_line())}</p>")
    parts.append(
        '<p class="note">Sent to rftools.io for each: the calculator and these numbers, '
        "never the board, its names or the project.</p>"
    )
    parts.append(_table(
        ("Net class", "Layer", "Figure", "Calculator", "Inputs"), state.planned_rows()
    ))

    parts.append("<h2>Results</h2>")
    if state.results:
        rows = []
        for row in state.results:
            rows.append(row.cells() + (row.note,))
        parts.append(_table(COLUMNS + ("Note",), rows))
    else:
        parts.append('<p class="note">Nothing was computed.</p>')
    if state.refusals:
        parts.append("<h2>Refusals</h2>")
        parts.append('<ul class="warn">' + "".join(
            f"<li>{e(r)}</li>" for r in state.refusals
        ) + "</ul>")

    parts.append(
        '<p class="muted">Nothing was written to the board or the project. Privacy: '
        f'<a href="{e(PRIVACY_URL)}">{e(PRIVACY_URL)}</a></p>'
    )
    parts.append("</body></html>\n")
    # A key must never reach the page, whatever a message carried.
    return redact("\n".join(parts), (state.services.settings.api_key,))


def write_report(text: str, directory: str | None = None) -> str:
    """Write the page to a new file in the temp directory; returns its path."""
    directory = directory or tempfile.gettempdir()
    fd, path = tempfile.mkstemp(prefix="rftools-kicad-report-", suffix=".html", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _table(head: tuple, rows: list) -> str:
    e = html.escape
    out = ['<div class="scroll"><table><thead><tr>']
    out += [f"<th>{e(str(h))}</th>" for h in head]
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{e(str(cell))}</td>" for cell in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _mm(nm: int | None) -> str:
    return "not set" if nm is None else f"{nm / 1e6:g} mm"
