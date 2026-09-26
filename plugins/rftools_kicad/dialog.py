"""The wxPython dialog (task 4.1): a thin view over :class:`dialog_logic.DialogState`.

Every decision — the layer model under review, the class form, the budget,
the key flow, the run, the proposals and the write — is in
:mod:`rftools_kicad.dialog_logic`, which is tested without a display. This
module only lays out widgets, copies what the user types into the state, and
shows what the state says.

``import wx`` at the top is deliberate: when it fails, ``main.presenter``
falls back to the HTML report (:mod:`rftools_kicad.report`), which has no
write control. When wx imports but cannot open a window (no display), the
report is used too.

Layout: three tabs — 1 Layer model, 2 Net classes, 3 Results — above the key,
the run budget and the buttons (Run, Apply to net classes…, Restore…, Close).
On a KiCad that cannot take a net-class write (10.0.6 and earlier), the write
buttons become "Values for Board Setup…" and "Previous values…": they show
the same preview with Board Setup's column names, the reason, and Copy values
(the clipboard) instead of a write.
"""
from __future__ import annotations

from typing import Any

import wx
import wx.lib.scrolledpanel as scrolled

from rftools_kicad import netclasses as N
from rftools_kicad.dialog_logic import COLUMNS, MANUAL, OK, DialogState

TITLE = "rftools.io board calculations"

APPLY_LABEL = "Apply to net classes…"
RESTORE_LABEL = "Restore previous values…"
MANUAL_APPLY_LABEL = "Values for Board Setup…"
MANUAL_RESTORE_LABEL = "Previous values…"
COPY_LABEL = "Copy values"

#: Result columns' widths, in DIPs: COLUMNS then the note.
COLUMN_WIDTHS = (110, 70, 190, 110, 150, 90, 220, 170, 130, 60, 320)

WARN = wx.Colour(170, 60, 20)


def present(model: Any, netclasses: list, services: Any) -> int:
    """Show the dialog; returns the process exit code (main.present's contract)."""
    try:
        app = wx.App(False)
    except (Exception, SystemExit):  # wx exits when it cannot reach a display
        from rftools_kicad import report

        return report.present(model, netclasses, services)
    state = DialogState(model, netclasses, services)
    dialog = MainDialog(None, state)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
    _save_options(state, services)
    del app
    return 0


def _save_options(state: DialogState, services: Any) -> None:
    from rftools_kicad.settings import save_options

    try:
        save_options(state.saved_options(), path=services.settings.path)
    except OSError:
        pass  # options are a convenience; losing them costs nothing


class MainDialog(wx.Dialog):
    def __init__(self, parent: Any, state: DialogState) -> None:
        super().__init__(
            parent, title=TITLE,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.state = state
        self.book = wx.Notebook(self)
        self.layers_page = scrolled.ScrolledPanel(self.book)
        self.classes_page = scrolled.ScrolledPanel(self.book)
        self.results_page = wx.Panel(self.book)
        self.book.AddPage(self.layers_page, "1  Layer model")
        self.book.AddPage(self.classes_page, "2  Net classes")
        self.book.AddPage(self.results_page, "3  Results")

        self._build_layers()
        self._build_classes()
        self._build_results()
        bottom = self._build_bottom()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.book, 1, wx.EXPAND | wx.ALL, self.FromDIP(8))
        sizer.Add(bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(8))
        self.SetSizer(sizer)
        self.SetSize(self.FromDIP(wx.Size(1180, 760)))
        self.CentreOnScreen()
        self._refresh()
        if state.has_key:
            wx.CallAfter(self.on_check_allowance, None)

    # ── Page 1: the layer model ──────────────────────────────────────────────

    def _build_layers(self) -> None:
        page = self.layers_page
        page.DestroyChildren()
        gap = self.FromDIP(6)
        sizer = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(page, label=(
            "Review the model before anything is computed. Tick the copper layers that are "
            "reference planes; the structures are derived again at once. "
            + self.state.plane_source_text()
        ))
        intro.Wrap(self.FromDIP(1050))
        sizer.Add(intro, 0, wx.ALL, gap)

        grid = wx.FlexGridSizer(cols=3, vgap=gap, hgap=self.FromDIP(12))
        grid.AddGrowableCol(2, 1)
        for label in ("Plane", "Copper layer", "Computed as"):
            grid.Add(_bold(wx.StaticText(page, label=label)))
        for row in self.state.layer_rows():
            box = wx.CheckBox(page)
            box.SetValue(row.plane)
            box.Bind(wx.EVT_CHECKBOX, lambda event, name=row.name: self.on_plane(name, event))
            grid.Add(box, 0, wx.ALIGN_TOP)
            grid.Add(_bold(wx.StaticText(page, label=row.name)), 0, wx.ALIGN_TOP)
            text = row.text + ("".join("\n• " + p for p in row.problems) if row.problems else "")
            detail = wx.StaticText(page, label=text)
            detail.Wrap(self.FromDIP(900))
            if row.problems:
                detail.SetForegroundColour(WARN)
            grid.Add(detail, 1, wx.EXPAND)
        sizer.Add(grid, 0, wx.EXPAND | wx.ALL, gap)

        other = [p for p in self.state.problems()
                 if not any(p in r.problems for r in self.state.layer_rows())]
        for problem in other:
            text = wx.StaticText(page, label="• " + problem)
            text.SetForegroundColour(WARN)
            text.Wrap(self.FromDIP(1050))
            sizer.Add(text, 0, wx.LEFT | wx.RIGHT, gap)
        reset = wx.Button(page, label="Reset to the proposal")
        reset.Bind(wx.EVT_BUTTON, self.on_reset_planes)
        sizer.Add(reset, 0, wx.ALL, gap)
        page.SetSizer(sizer)
        page.SetupScrolling(scroll_x=False)
        page.Layout()

    def on_plane(self, name: str, event: Any) -> None:
        self.state.set_plane(name, event.IsChecked())
        wx.CallAfter(self._model_changed)  # the checkbox is rebuilt; leave its handler first

    def on_reset_planes(self, event: Any) -> None:
        self.state.reset_planes()
        wx.CallAfter(self._model_changed)

    def _model_changed(self) -> None:
        self._build_layers()
        self._build_classes()
        self._fill_results()
        self._refresh()

    # ── Page 2: the net classes ──────────────────────────────────────────────

    def _build_classes(self) -> None:
        page = self.classes_page
        page.DestroyChildren()
        state = self.state
        gap = self.FromDIP(6)
        sizer = wx.BoxSizer(wx.VERTICAL)

        options = wx.BoxSizer(wx.HORIZONTAL)
        mask = wx.CheckBox(page, label="Outer layers under their solder mask")
        mask.SetValue(state.solder_mask_cover)
        mask.SetToolTip(
            "Computes outer layers with controlled-impedance under the stackup's solder mask "
            "instead of as bare microstrip."
        )
        mask.Bind(wx.EVT_CHECKBOX, self.on_mask)
        options.Add(mask, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(24))
        options.Add(wx.StaticText(page, label="Manufacturing grid"), 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap)
        grid_text = wx.TextCtrl(page, value=state.grid_text, size=self.FromDIP(wx.Size(70, -1)))
        grid_text.Bind(wx.EVT_TEXT, self.on_grid)
        options.Add(grid_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap)
        options.Add(wx.StaticText(page, label="mm"), 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(options, 0, wx.ALL, gap)

        intro = wx.StaticText(page, label=(
            "Tick the classes to compute. A class is board-wide but an impedance width is "
            "per layer: the width written to a class is the one for the layer it is routed "
            "on. Targets, gap and current are optional. A coplanar gap computes outer layers "
            "as grounded coplanar waveguide. Each computation is one API call; repeats are "
            "answered from the cache for free."
        ))
        intro.Wrap(self.FromDIP(1050))
        sizer.Add(intro, 0, wx.ALL, gap)

        heads = ("Net class", "Routed on", "Also on", "Z₀", "Z₀ target Ω", "Zdiff target Ω",
                 "CPW gap mm", "Current A", "Via", "Class values (mm)")
        table = wx.FlexGridSizer(cols=len(heads), vgap=gap, hgap=self.FromDIP(10))
        for head in heads:
            table.Add(_bold(wx.StaticText(page, label=head)), 0, wx.ALIGN_BOTTOM)
        layers = state.trace_layers()
        number = self.FromDIP(wx.Size(70, -1))
        for nc in state.netclasses:
            form = state.forms[nc.name]
            name = nc.name
            select = wx.CheckBox(page, label=name)
            select.SetValue(form.selected)
            select.Bind(wx.EVT_CHECKBOX, lambda e, n=name: self.on_form(n, selected=e.IsChecked()))
            table.Add(select, 0, wx.ALIGN_CENTER_VERTICAL)

            choice = wx.Choice(page, choices=layers or ["(no signal layer)"])
            if form.layer in layers:
                choice.SetSelection(layers.index(form.layer))
            choice.Enable(bool(layers))
            choice.Bind(wx.EVT_CHOICE, lambda e, n=name: self.on_form(
                n, layer=e.GetString()))
            table.Add(choice, 0, wx.ALIGN_CENTER_VERTICAL)

            also = wx.Button(page, label=_also_label(form.extra_layers),
                             style=wx.BU_EXACTFIT)
            also.Enable(len(layers) > 1)
            also.Bind(wx.EVT_BUTTON, lambda e, n=name, b=also: self.on_also(n, b))
            table.Add(also, 0, wx.ALIGN_CENTER_VERTICAL)

            se = wx.CheckBox(page)
            se.SetValue(form.single_ended)
            se.SetToolTip("Impedance at the class's current track width")
            se.Bind(wx.EVT_CHECKBOX, lambda e, n=name: self.on_form(n, single_ended=e.IsChecked()))
            table.Add(se, 0, wx.ALIGN_CENTER)

            for key in ("impedance_target", "diff_target", "cpw_gap", "current"):
                text = wx.TextCtrl(page, value=getattr(form, key), size=number)
                text.Bind(wx.EVT_TEXT, lambda e, n=name, k=key: self.on_form(
                    n, **{k: e.GetString()}))
                table.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)

            via = wx.CheckBox(page)
            via.SetValue(form.via)
            via.SetToolTip("Via impedance, capacitance, inductance and current capacity")
            via.Bind(wx.EVT_CHECKBOX, lambda e, n=name: self.on_form(n, via=e.IsChecked()))
            table.Add(via, 0, wx.ALIGN_CENTER)

            table.Add(wx.StaticText(page, label=_class_values(nc)), 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(table, 0, wx.ALL, gap)

        self.form_errors = wx.StaticText(page, label="")
        self.form_errors.SetForegroundColour(WARN)
        sizer.Add(self.form_errors, 0, wx.ALL, gap)
        page.SetSizer(sizer)
        page.SetupScrolling()
        page.Layout()

    def on_form(self, name: str, **values: Any) -> None:
        self.state.set_form(name, **values)
        self._refresh()

    def on_also(self, name: str, button: Any) -> None:
        form = self.state.forms[name]
        others = [x for x in self.state.trace_layers() if x != form.layer]
        with wx.MultiChoiceDialog(self, f"Also compute {name} on:", "Layers", others) as dialog:
            dialog.SetSelections([others.index(x) for x in form.extra_layers if x in others])
            if dialog.ShowModal() != wx.ID_OK:
                return
            chosen = tuple(others[i] for i in dialog.GetSelections())
        self.state.set_form(name, extra_layers=chosen)
        button.SetLabel(_also_label(chosen))
        self.classes_page.Layout()
        self._refresh()

    def on_mask(self, event: Any) -> None:
        self.state.solder_mask_cover = event.IsChecked()
        self._refresh()

    def on_grid(self, event: Any) -> None:
        self.state.grid_text = event.GetString()
        self._refresh()

    # ── Page 3: the results ──────────────────────────────────────────────────

    def _build_results(self) -> None:
        page = self.results_page
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.stale = wx.StaticText(page, label="")
        self.stale.SetForegroundColour(WARN)
        sizer.Add(self.stale, 0, wx.ALL, self.FromDIP(4))
        self.table = wx.ListCtrl(page, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES)
        headers = COLUMNS + ("Note",)
        if len(headers) != len(COLUMN_WIDTHS):  # zip(strict=True) needs Python 3.10
            raise ValueError("COLUMN_WIDTHS needs one width per result column and the note")
        for index, (head, width) in enumerate(zip(headers, COLUMN_WIDTHS)):
            self.table.InsertColumn(index, head, width=self.FromDIP(width))
        sizer.Add(self.table, 1, wx.EXPAND | wx.ALL, self.FromDIP(4))
        self.refusal_text = wx.StaticText(page, label="")
        self.refusal_text.SetForegroundColour(WARN)
        sizer.Add(self.refusal_text, 0, wx.ALL, self.FromDIP(4))
        page.SetSizer(sizer)

    def _fill_results(self) -> None:
        self.table.DeleteAllItems()
        for row in self.state.results:
            index = self.table.InsertItem(self.table.GetItemCount(), row.cells()[0])
            for column, cell in enumerate(row.cells()[1:] + (row.note,), start=1):
                self.table.SetItem(index, column, cell)
            if row.status != OK:
                self.table.SetItemTextColour(index, WARN)
        self.stale.SetLabel(
            "The layer model changed after this run: run again before applying widths."
            if self.state.results_stale else ""
        )
        self.refusal_text.SetLabel("\n".join(self.state.refusals))
        self.refusal_text.Wrap(self.FromDIP(1100))
        self.results_page.Layout()

    # ── The key, the budget and the buttons ──────────────────────────────────

    def on_get_key(self, _event: Any = None) -> None:
        """Open the key link; when no browser opens, copy it and say where it is."""
        from rftools_kicad.browser import open_url

        url = self.state.services.key_url
        if open_url(url, wx_module=wx):
            return
        copied = False
        if wx.TheClipboard.Open():
            try:
                copied = bool(wx.TheClipboard.SetData(wx.TextDataObject(url)))
                wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()
        how = "It is copied to the clipboard; paste it into your browser." if copied else \
            "Copy it into your browser."
        wx.MessageBox(f"Your browser could not be opened from KiCad.\n\n{url}\n\n{how}",
                      "Get a free key", wx.OK | wx.ICON_INFORMATION, self)

    def _build_bottom(self) -> Any:
        panel = wx.Panel(self)
        gap = self.FromDIP(6)
        sizer = wx.BoxSizer(wx.VERTICAL)

        key = wx.BoxSizer(wx.HORIZONTAL)
        self.key_status = wx.StaticText(panel, label="")
        key.Add(self.key_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap)
        get_key = wx.Button(panel, label="Get a free key")
        get_key.SetToolTip(self.state.services.key_url)
        get_key.Bind(wx.EVT_BUTTON, self.on_get_key)
        key.Add(get_key, 0, wx.RIGHT, gap)
        self.key_field = wx.TextCtrl(panel, style=wx.TE_PASSWORD,
                                     size=self.FromDIP(wx.Size(260, -1)))
        self.key_field.SetHint("Paste a key (rfc_…)")
        key.Add(self.key_field, 0, wx.RIGHT, gap)
        save = wx.Button(panel, label="Save key")
        save.Bind(wx.EVT_BUTTON, self.on_save_key)
        key.Add(save)
        sizer.Add(key, 0, wx.EXPAND | wx.BOTTOM, gap)

        budget = wx.BoxSizer(wx.HORIZONTAL)
        self.budget_text = _bold(wx.StaticText(panel, label=""))
        budget.Add(self.budget_text, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap)
        check = wx.Button(panel, label="Check allowance")
        check.SetToolTip("Reads the calls left this month. Reading it is free.")
        check.Bind(wx.EVT_BUTTON, self.on_check_allowance)
        budget.Add(check)
        sizer.Add(budget, 0, wx.EXPAND | wx.BOTTOM, gap)

        self.write_note = wx.StaticText(panel, label="")
        self.write_note.SetForegroundColour(WARN)
        sizer.Add(self.write_note, 0, wx.EXPAND | wx.BOTTOM, gap)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.run_button = wx.Button(panel, label="Run")
        self.run_button.Bind(wx.EVT_BUTTON, self.on_run)
        self.apply_button = wx.Button(panel, label=APPLY_LABEL)
        self.apply_button.Bind(wx.EVT_BUTTON, self.on_apply)
        self.restore_button = wx.Button(panel, label=RESTORE_LABEL)
        self.restore_button.Bind(wx.EVT_BUTTON, self.on_restore)
        close = wx.Button(panel, id=wx.ID_CLOSE, label="Close")
        close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.SetEscapeId(wx.ID_CLOSE)
        for button in (self.run_button, self.apply_button, self.restore_button):
            buttons.Add(button, 0, wx.RIGHT, gap)
        buttons.AddStretchSpacer()
        buttons.Add(close)
        sizer.Add(buttons, 0, wx.EXPAND)
        panel.SetSizer(sizer)
        return panel

    def _refresh(self) -> None:
        state = self.state
        self.key_status.SetLabel(state.key_status())
        self.budget_text.SetLabel(state.budget_line())
        if hasattr(self, "form_errors"):
            self.form_errors.SetLabel("\n".join(state.errors()))
        mode, reason = state.write_mode()
        manual = mode == MANUAL
        self.apply_button.SetLabel(MANUAL_APPLY_LABEL if manual else APPLY_LABEL)
        self.restore_button.SetLabel(MANUAL_RESTORE_LABEL if manual else RESTORE_LABEL)
        can_apply = bool(state.results) and not state.results_stale and mode is not None
        self.apply_button.Enable(can_apply)
        self.apply_button.SetToolTip(reason or "Preview the widths and gaps, then confirm.")
        note = reason if manual else ""
        if self.write_note.GetLabel() != note:
            self.write_note.SetLabel(note)
            self.write_note.Wrap(self.FromDIP(1100))
        self.Layout()

    def on_check_allowance(self, event: Any) -> None:
        with wx.BusyCursor():
            self.state.refresh_usage()
        self._refresh()

    def on_save_key(self, event: Any) -> None:
        with wx.BusyCursor():
            saved, message = self.state.save_key(self.key_field.GetValue())
        if saved:
            self.key_field.SetValue("")
        wx.MessageBox(message, TITLE, wx.OK | (wx.ICON_INFORMATION if saved else wx.ICON_ERROR),
                      self)
        self._refresh()

    def on_run(self, event: Any) -> None:
        state = self.state
        with wx.BusyCursor():
            state.refresh_usage()  # free
        self._refresh()
        ok, message = state.run_check()
        if not ok:
            wx.MessageBox(message, TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        if wx.MessageBox(message + "\n\nRun now?", TITLE,
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        total = max(1, len(state.plan().items))
        progress = wx.ProgressDialog(TITLE, "Computing…", maximum=total, parent=self,
                                     style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)
        try:
            summary = state.run(
                lambda done, count, label: progress.Update(min(done, total), label or " ")
            )
        finally:
            progress.Destroy()
        self._fill_results()
        self.book.SetSelection(2)
        self._refresh()
        if summary.message:
            wx.MessageBox(summary.message, TITLE, wx.OK | wx.ICON_ERROR, self)

    def on_apply(self, event: Any) -> None:
        self._preview(*self.state.preview())

    def on_restore(self, event: Any) -> None:
        self._preview(*self.state.restore_preview())

    def _preview(self, preview: Any, reason: str | None) -> None:
        if preview is None:
            wx.MessageBox(reason, TITLE, wx.OK | wx.ICON_INFORMATION, self)
        elif preview.manual:  # this KiCad cannot take the write: the values, to enter by hand
            with PreviewDialog(self, preview) as dialog:
                dialog.ShowModal()
        else:
            self._write(preview)

    def _write(self, preview: Any) -> None:
        result = self.state.apply(preview, self._confirm)
        if result.cancelled:
            return
        if result.applied:
            icon = wx.ICON_INFORMATION
        elif result.status == N.STATUS_DAMAGED:
            icon = wx.ICON_ERROR
        else:
            icon = wx.ICON_WARNING
        wx.MessageBox(result.message, TITLE, wx.OK | icon, self)
        self._build_classes()  # the classes' current values, as KiCad now reports them
        self._refresh()

    def _confirm(self, preview: Any) -> bool:
        with PreviewDialog(self, preview) as dialog:
            return dialog.ShowModal() == wx.ID_OK


class PreviewDialog(wx.Dialog):
    """Every field of each class, current → proposed.

    For a write, OK writes and Cancel sends nothing. For a preview marked
    ``manual`` (a KiCad that cannot take the write), the fields carry Board
    Setup's column names under the reason, and the buttons are Copy values and
    Close: nothing can be written from it.
    """

    def __init__(self, parent: Any, preview: Any) -> None:
        restore = preview.action == N.ACTION_RESTORE
        self.preview = preview
        manual = bool(preview.manual)
        if manual:
            title = "Previous net-class values" if restore else "Net-class values for Board Setup"
        else:
            title = "Restore net classes" if restore else "Apply to net classes"
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        gap = self.FromDIP(8)
        sizer = wx.BoxSizer(wx.VERTICAL)
        if manual:
            text = (
                f"{preview.manual} Keep this window open while you enter them, or copy them."
            )
        else:
            text = (
                "Only the values marked below change: track width, differential-pair width "
                "and differential-pair gap. Every other setting of every class stays as it is. "
                "The previous values are recorded, so Restore can write them back."
            )
        intro = wx.StaticText(self, label=text)
        intro.Wrap(self.FromDIP(640))
        sizer.Add(intro, 0, wx.ALL, gap)
        table = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_HRULES,
                            size=self.FromDIP(wx.Size(660, 220)))
        if manual:
            heads = ("Net class", "Board Setup column", "Current", "Enter")
        else:
            heads = ("Net class", "Field", "Current", "Restored to" if restore else "Proposed")
        for index, head in enumerate(heads):
            table.InsertColumn(index, head, width=self.FromDIP(150))
        labels = N.BOARD_SETUP_COLUMNS if manual else N.FIELD_LABELS
        for netclass in preview.classes:
            for label, current, proposed, changes in netclass.rows(labels):
                row = table.InsertItem(table.GetItemCount(), netclass.netclass)
                table.SetItem(row, 1, label)
                table.SetItem(row, 2, current)
                table.SetItem(row, 3, proposed)
                if changes:
                    table.SetItemFont(row, _bold_font(table))
        sizer.Add(table, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, gap)
        if preview.notes:
            notes = wx.StaticText(self, label="\n".join(preview.notes))
            notes.Wrap(self.FromDIP(640))
            sizer.Add(notes, 0, wx.ALL, gap)
        if manual:
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            copy = wx.Button(self, label=COPY_LABEL)
            copy.Bind(wx.EVT_BUTTON, self.on_copy)
            buttons.Add(copy, 0, wx.RIGHT, gap)
            self.copied = wx.StaticText(self, label="")
            buttons.Add(self.copied, 0, wx.ALIGN_CENTER_VERTICAL)
            buttons.AddStretchSpacer()
            close = wx.Button(self, wx.ID_CLOSE, label="Close")
            close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
            self.SetEscapeId(wx.ID_CLOSE)
            buttons.Add(close)
        else:
            buttons = wx.StdDialogButtonSizer()
            ok = wx.Button(self, wx.ID_OK, label="Restore" if restore else "Write to net classes")
            buttons.AddButton(ok)
            buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
            buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, gap)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()

    def on_copy(self, event: Any) -> None:
        copied = copy_text(self.preview.manual_text())
        self.copied.SetLabel("Copied." if copied else "The clipboard could not be opened.")
        self.Layout()


def copy_text(text: str) -> bool:
    """Put *text* on the clipboard; False when the clipboard cannot be opened."""
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        return False
    try:
        clipboard.SetData(wx.TextDataObject(text))
    finally:
        clipboard.Close()
    clipboard.Flush()  # keep it after the plugin exits, where the platform can
    return True


# ── Helpers ──────────────────────────────────────────────────────────────────


def _bold(control: Any) -> Any:
    control.SetFont(_bold_font(control))
    return control


def _bold_font(control: Any) -> Any:
    font = control.GetFont()
    font.SetWeight(wx.FONTWEIGHT_BOLD)
    return font


def _also_label(layers: tuple) -> str:
    return f"Also on {len(layers)}…" if layers else "Also on…"


def _class_values(nc: Any) -> str:
    def mm(nm: int | None) -> str:
        return "–" if nm is None else f"{nm / 1e6:g}"

    return (
        f"track {mm(nc.track_width_nm)} · pair {mm(nc.diff_pair_width_nm)}/"
        f"{mm(nc.diff_pair_gap_nm)} · via {mm(nc.via_diameter_nm)}/{mm(nc.via_drill_nm)}"
    )
