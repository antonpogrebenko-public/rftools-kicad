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

import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rftools_kicad.dialog_logic import DialogState
from rftools_kicad.settings import settings_path

TITLE = "rftools.io board calculations"

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
    """The report page in rftools.io's design: layer model, planned calculations,
    results, refusals. No script and no control (webui with controls=False)."""
    from rftools_kicad import webui

    session = webui.Session(state)
    return webui.page(session, theme="auto", controls=False, notes=notes,
                      budget_line=budget_line, intro=FULL_DIALOG)


def write_report(text: str, directory: str | None = None) -> str:
    """Write the page to a new file in the temp directory; returns its path."""
    directory = directory or tempfile.gettempdir()
    fd, path = tempfile.mkstemp(prefix="rftools-kicad-report-", suffix=".html", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
