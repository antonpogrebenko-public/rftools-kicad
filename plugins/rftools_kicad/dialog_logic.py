"""Everything the results dialog decides, with no wx in it (task 4.1).

The wx dialog (:mod:`rftools_kicad.dialog`) and the HTML report
(:mod:`rftools_kicad.report`) are thin views over :class:`DialogState`, which
holds and tests without a display:

(a) the layer model under review: each copper layer, whether it is a
    reference plane (editable; the model is derived again with
    ``LayerModel.with_planes``), the structure computed on it (the model's own
    ``describe()``, which says when plies were combined) and its problems;
(b) the net-class form: per class, whether it is selected, a single-ended and
    a differential target, a coplanar gap, a current for IPC-2152, the layer it
    is routed on (a class is board-wide and a width is per layer: the width
    written is the routing layer's), other layers to compute on, and a via
    check; plus the solder-mask option and the manufacturing grid;
(c) the run budget (``budget.plan_run``, counted before any calculation
    request; the allowance comes from the usage endpoint, which is never
    metered) and the key flow (``settings``: the key link, a pasted key checked
    with a usage request before it is saved, only the key's public id shown);
(d) the run: one :class:`ResultRow` per planned figure with the class's
    current value, the suggested value, the value the API achieved, the
    formula reference, the range flag with the bound crossed, the engine
    version and whether the cache answered; refusals are listed, and rows
    computed before a refusal stay;
(e) the net-class write: the proposals the results support, the preview, the
    confirmed write and the restore, all through :mod:`rftools_kicad.netclasses`.
    On a KiCad that cannot take a write (10.0.6 and earlier, or a version
    that cannot be read) the write mode is :data:`MANUAL`: the preview is
    still made, marked with the reason, for entering the values by hand in
    Board Setup, and nothing is written or restored.

``services`` is ``main.Services``: ``api()``, ``save_key(key)``, ``settings``,
``options``, ``grid``, ``cache``, ``key_url``, ``history_path``, ``kicad``,
``board``, ``project``.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from rftools_kicad import netclasses as N
from rftools_kicad.api import Api, Figure, Refusal, normalise_usage, read_figure
from rftools_kicad.budget import (
    FIGURE_CURRENT,
    FIGURE_DIFF_IMPEDANCE,
    FIGURE_GAP_FOR_TARGET,
    FIGURE_IMPEDANCE,
    FIGURE_VIA,
    FIGURE_WIDTH_FOR_TARGET,
    Budget,
    ClassRequest,
    Plan,
    PlannedItem,
    plan_run,
)
from rftools_kicad.mapping import (
    CALCULATORS,
    COPLANAR,
    COVERED_MICROSTRIP,
    SOLVE,
    Computation,
    NetClassValues,
)
from rftools_kicad.settings import SOURCE_ENVIRONMENT, KeyError_, public_id, redact
from rftools_kicad.solve import DEFAULT_GRID_MM, TargetResult, run_target
from rftools_kicad.stackup import (
    NM_PER_MM,
    PLANES_FROM_ADJACENCY,
    PLANES_FROM_ROLES,
    LayerModel,
)

#: The results table's columns, in order (:meth:`ResultRow.cells`).
COLUMNS = (
    "Net class", "Layer", "Figure", "Current", "Suggested", "Achieved",
    "Formula", "Range", "Engine", "Cache",
)

OK = "ok"
REFUSED = "refused"
PROBLEM = "problem"
UNREACHABLE = "unreachable"
SKIPPED = "skipped"

#: The largest manufacturing grid accepted, mm.
MAX_GRID_MM = 1.0

#: How net-class values reach KiCad (:meth:`DialogState.write_mode`).
WRITE = "write"  # the plugin writes them, after a preview and a confirmation
MANUAL = "manual"  # this KiCad cannot take a write: the user enters them in Board Setup

_UNREAD = object()

PLANE_SOURCE_TEXT = {
    PLANES_FROM_ADJACENCY: (
        "KiCad does not say which copper layers are planes, so every copper layer is "
        "proposed as a reference plane for its neighbours, and each is also computed as a "
        "signal layer against them. Untick the signal layers to choose the planes yourself: "
        "then only unticked layers are computed."
    ),
    PLANES_FROM_ROLES: "Planes proposed from the layers' types (power or mixed).",
    "override": "Planes as you chose them. Unticked layers are computed as signal layers.",
}


# ── Form values ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassForm:
    """What the user entered for one net class. Numbers are kept as typed."""

    name: str
    selected: bool = False
    #: Compute the single-ended impedance at the class's track width.
    single_ended: bool = True
    impedance_target: str = ""  # Ω
    diff_target: str = ""  # Ω
    cpw_gap: str = ""  # mm; outer layers become grounded coplanar waveguide
    current: str = ""  # A, for IPC-2152
    #: The layer the class is routed on: its width is the one written, and
    #: its current is computed there.
    layer: str | None = None
    #: Further layers to compute the class's figures on.
    extra_layers: tuple = ()
    via: bool = False


@dataclass(frozen=True)
class LayerRow:
    name: str
    plane: bool
    outer: bool
    computed: bool
    text: str
    problems: tuple = ()


@dataclass(frozen=True)
class ResultRow:
    """One line of the results table, as text, plus what a proposal needs."""

    netclass: str
    layer: str | None
    figure: str
    label: str
    status: str = OK
    calculator: str = ""
    current: str = ""
    suggested: str = ""
    achieved: str = ""
    formula: str = ""
    range: str = ""
    engine: str = ""
    cached: bool = False
    note: str = ""
    suggested_mm: float | None = None
    achieved_value: float | None = None
    target: float | None = None
    refusal: Refusal | None = None
    computation: Computation | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def cells(self) -> tuple:
        return (
            self.netclass, self.layer or "board", self.label, self.current, self.suggested,
            self.achieved, self.formula, self.range, self.engine,
            "cached" if self.cached else "",
        )


@dataclass(frozen=True)
class RunSummary:
    rows: tuple
    refusals: tuple
    requests: int  # metered requests made
    message: str = ""


# ── The state ────────────────────────────────────────────────────────────────


class DialogState:
    """The dialog's data and decisions; see the module docstring."""

    def __init__(
        self,
        model: LayerModel,
        netclasses: list,
        services: Any,
        *,
        opener: Callable[[str], Any] | None = None,
    ) -> None:
        self.proposed_model = model
        self.model = model
        self.netclasses: list = list(netclasses)
        self.services = services
        self._opener = opener
        options = services.options
        self.solder_mask_cover = bool(getattr(options, "solder_mask_cover", False))
        self.grid_text = _num(services.grid or DEFAULT_GRID_MM)
        first = self.trace_layers()[0] if self.trace_layers() else None
        self.forms: dict = {nc.name: ClassForm(nc.name, layer=first) for nc in self.netclasses}
        self.usage: dict | None = None
        self.usage_refusal: Refusal | None = None
        self.results: tuple = ()
        self.refusals: tuple = ()
        self.results_stale = False
        self._api: Api | None = None
        self._kicad_version: Any = _UNREAD

    # ── (a) The layer model ──────────────────────────────────────────────────

    def trace_layers(self) -> list:
        return [s.layer for s in self.model.structures]

    def plane_source_text(self) -> str:
        return PLANE_SOURCE_TEXT.get(self.model.plane_source, "")

    def layer_rows(self) -> list:
        rows = []
        for copper in self.model.copper:
            structure = self.model.structure(copper.name)
            problems = tuple(p.message for p in self.model.problems_for(copper.name))
            plane = copper.name in self.model.planes
            if structure is not None:
                text = structure.describe()
            elif plane:
                text = f"{copper.name}: reference plane."
            else:
                text = f"{copper.name}: not computed."
            rows.append(LayerRow(copper.name, plane, copper.outer, structure is not None,
                                 text, problems))
        return rows

    def problems(self) -> list:
        return [p.message for p in self.model.problems]

    def set_plane(self, layer: str, plane: bool) -> None:
        """Mark or unmark *layer* as a reference plane and derive the model again."""
        planes = set(self.model.planes)
        if plane:
            planes.add(layer)
        else:
            planes.discard(layer)
        self._replace_model(self.model.with_planes(planes))

    def reset_planes(self) -> None:
        self._replace_model(self.proposed_model)

    def _replace_model(self, model: LayerModel) -> None:
        self.model = model
        trace = self.trace_layers()
        for name, form in self.forms.items():
            layer = form.layer if form.layer in trace else (trace[0] if trace else None)
            extra = tuple(x for x in form.extra_layers if x in trace and x != layer)
            self.forms[name] = replace(form, layer=layer, extra_layers=extra)
        if self.results:
            self.results_stale = True

    # ── (b) The net-class form ───────────────────────────────────────────────

    def netclass(self, name: str) -> NetClassValues:
        for nc in self.netclasses:
            if nc.name == name:
                return nc
        raise KeyError(name)

    def set_form(self, name: str, **values: Any) -> ClassForm:
        form = replace(self.forms[name], **values)
        self.forms[name] = form
        return form

    def select_all_for_report(self) -> None:
        """The report's defaults: every class, on every trace layer, its width's impedance."""
        trace = self.trace_layers()
        for name in self.forms:
            self.forms[name] = ClassForm(
                name, selected=True, single_ended=True,
                layer=trace[0] if trace else None, extra_layers=tuple(trace[1:]),
            )

    def grid(self) -> tuple:
        """``(grid in mm, None)`` or ``(None, error)``."""
        value, error = _number(self.grid_text, "The grid", "mm")
        if error:
            return None, error
        if value is None:
            return None, "Enter a manufacturing grid in mm (0.001 is a micrometre)."
        if value > MAX_GRID_MM:
            return None, f"The grid must be at most {MAX_GRID_MM:g} mm."
        return value, None

    def mapping_options(self) -> Any:
        return replace(self.services.options, solder_mask_cover=self.solder_mask_cover)

    def requests(self) -> tuple:
        """``(ClassRequests for the selected classes, errors)``."""
        requests, errors = [], []
        trace = self.trace_layers()
        for nc in self.netclasses:
            form = self.forms[nc.name]
            if not form.selected:
                continue
            fields = []
            for text, what, unit in (
                (form.impedance_target, "single-ended target", "Ω"),
                (form.diff_target, "differential target", "Ω"),
                (form.cpw_gap, "coplanar gap", "mm"),
                (form.current, "current", "A"),
            ):
                value, error = _number(text, f"{nc.name}: the {what}", unit)
                if error:
                    errors.append(error)
                fields.append(value)
            target, diff_target, gap, current = fields
            if form.layer is None or form.layer not in trace:
                errors.append(f"{nc.name}: choose the layer it is routed on.")
                continue
            if not (form.single_ended or target or diff_target or current or form.via):
                errors.append(
                    f"{nc.name}: nothing to compute. Tick single-ended or via, or enter a "
                    "target or a current."
                )
                continue
            layers = (form.layer,) + tuple(
                x for x in form.extra_layers if x != form.layer and x in trace
            )
            requests.append(ClassRequest(
                nc,
                layers=layers,
                single_ended=form.single_ended,
                impedance_target=target,
                diff_target=diff_target,
                current_a=current,
                current_layer=form.layer,
                via=form.via,
                cpw_gap_mm=gap,
            ))
        return requests, errors

    def errors(self) -> list:
        _, errors = self.requests()
        _, grid_error = self.grid()
        return errors + ([grid_error] if grid_error else [])

    def plan(self) -> Plan:
        requests, _ = self.requests()
        grid, _ = self.grid()
        return plan_run(self.model, requests, self.mapping_options(), grid or DEFAULT_GRID_MM)

    def planned_rows(self) -> list:
        """``(class, layer, figure, calculator, what is sent or the problem)`` per item."""
        rows = []
        for item in self.plan().items:
            c = item.computation
            if c is None:
                rows.append((item.netclass, item.layer or "board", figure_label(item), "",
                             item.problem))
                continue
            sent = ", ".join(f"{k} {_num(v)}" for k, v in c.inputs)
            if c.kind == SOLVE:
                sent += f"; solve {c.solve_for} for {c.output} = {_num(c.target_value)}"
            rows.append((item.netclass, item.layer or "board", figure_label(item), c.slug, sent))
        return rows

    # ── (c) Key and budget ───────────────────────────────────────────────────

    @property
    def has_key(self) -> bool:
        return bool(self.services.settings.api_key)

    def key_status(self) -> str:
        settings = self.services.settings
        if not settings.api_key:
            return "No API key. Get a free key, paste it below and save it."
        where = (
            "from the RFTOOLS_API_KEY environment variable"
            if settings.key_source == SOURCE_ENVIRONMENT else "saved in the plugin's settings"
        )
        return f"API key {settings.key_id} ({where})."

    def open_key_link(self) -> str:
        url = self.services.key_url
        opener = self._opener
        if opener is None:
            import webbrowser

            opener = webbrowser.open
        opener(url)
        return url

    def save_key(self, text: str) -> tuple:
        """``(saved, message)``: the key is checked with a usage request, then saved."""
        text = (text or "").strip()
        try:
            usage = self.services.save_key(text)
        except KeyError_ as exc:
            return False, redact(str(exc), (text,))
        self._api = None
        self.usage = normalise_usage(usage) if usage else None
        self.usage_refusal = None
        message = f"Saved key {public_id(text)}."
        if self.usage and self.usage.get("remaining") is not None:
            message += f" {self._remaining_text()} remain this month."
        if self.services.settings.key_source == SOURCE_ENVIRONMENT:
            message += " RFTOOLS_API_KEY is set, and the plugin uses it instead."
        return True, message

    def api(self) -> tuple:
        """``(Api, None)`` or ``(None, why there is none)``."""
        if self._api is None:
            try:
                self._api = self.services.api()
            except Exception as exc:  # no key, or rftools-io did not load
                return None, redact(str(exc))
        return self._api, None

    def refresh_usage(self) -> None:
        """Read the allowance (``GET /v1/usage``: never metered)."""
        api, error = self.api()
        if api is None:
            self.usage, self.usage_refusal = None, None
            return
        self.usage, self.usage_refusal = api.usage()

    def budget(self) -> Budget:
        """The calls the selections need, against the allowance last read. Makes no request."""
        computations = self.plan().computations
        cache = self.services.cache
        cached = sum(1 for c in computations if cache is not None and cache.get(c.payload()))
        usage = self.usage or {}
        return Budget(
            calls=len(computations) - cached,
            cached=cached,
            total=len(computations),
            remaining=usage.get("remaining"),
            allowance=usage.get("allowance"),
            used=usage.get("used"),
            reset_at=usage.get("resetAt"),
            usage_refusal=self.usage_refusal,
        )

    def budget_line(self) -> str:
        budget = self.budget()
        if not self.has_key:
            calls = budget.calls
            return (
                f"This run will use at most {calls} API call{'' if calls == 1 else 's'}. "
                "Set an API key to see the allowance remaining."
            )
        if budget.remaining is None and budget.usage_refusal is None:
            head = budget.text().split(". The allowance")[0]
            return f"{head}. Check the allowance to see how many remain."
        return budget.text()

    def run_check(self) -> tuple:
        """``(can run, message)``: the confirmation text before a run, or why it cannot run."""
        errors = self.errors()
        if errors:
            return False, "Correct these first:\n" + "\n".join(errors)
        if not self.has_key:
            return False, f"Set an API key first. Get a free key at {self.services.key_url}"
        plan = self.plan()
        if not plan.computations:
            if plan.problems:
                return False, "\n".join(i.problem for i in plan.problems)
            return False, "Select a net class and what to compute."
        budget = self.budget()
        message = budget.text()
        if budget.fits is False:
            message += (
                f"\nThat is more than the {budget.remaining} left: the run stops when the "
                "allowance is spent, and what was computed by then stays shown."
            )
        return True, message

    def _remaining_text(self) -> str:
        usage = self.usage or {}
        text = f"{usage.get('remaining')}"
        if usage.get("allowance") is not None:
            text += f" of {usage['allowance']}"
        return text

    # ── (d) The run ──────────────────────────────────────────────────────────

    def run(
        self,
        progress: Callable[[int, int, str], Any] | None = None,
        *,
        cached_only: bool = False,
        skip_reason: str = "",
    ) -> RunSummary:
        """Compute the plan. ``cached_only`` answers from the cache and spends nothing."""
        plan = self.plan()
        api, error = self.api()
        if api is None:
            if not cached_only:
                return RunSummary(self.results, self.refusals, 0, error)
            api = Api(None, self.services.cache)
        api.reset()
        before = api.requests
        rows = []
        items = plan.items
        for index, item in enumerate(items):
            if progress is not None:
                progress(index, len(items), f"{item.netclass} {item.layer or ''}".strip())
            computation = item.computation
            if computation is None:
                rows.append(self._problem_row(item))
            elif cached_only and api.cached(computation) is None:
                rows.append(self._skipped_row(item, skip_reason))
            elif computation.kind == SOLVE:
                rows.append(self._target_row(item, run_target(api, computation)))
            else:
                rows.append(self._figure_row(item, read_figure(api.run(computation))))
        if progress is not None:
            progress(len(items), len(items), "")
        self.results = tuple(rows)
        self.results_stale = False
        seen = []
        for row in rows:
            if row.refusal is not None and row.refusal.message not in seen:
                seen.append(row.refusal.message)
        self.refusals = tuple(seen)
        if api.last_usage is not None:
            self.usage = api.last_usage
        return RunSummary(self.results, self.refusals, api.requests - before)

    def _problem_row(self, item: PlannedItem) -> ResultRow:
        return ResultRow(item.netclass, item.layer, item.figure, figure_label(item),
                         status=PROBLEM, note=item.problem or "")

    def _skipped_row(self, item: PlannedItem, reason: str) -> ResultRow:
        c = item.computation
        return ResultRow(item.netclass, item.layer, item.figure, figure_label(item),
                         status=SKIPPED, calculator=c.slug,
                         current=self._current_text(item), note=reason or "Not computed.",
                         computation=c)

    def _figure_row(self, item: PlannedItem, figure: Figure) -> ResultRow:
        c = figure.computation
        base = dict(
            netclass=item.netclass, layer=item.layer, figure=item.figure,
            label=figure_label(item), calculator=c.slug, current=self._current_text(item),
            cached=figure.cached, computation=c,
        )
        if figure.refusal is not None:
            return ResultRow(**base, status=REFUSED, note=figure.refusal.message,
                             refusal=figure.refusal)
        provenance = figure.provenance
        notes = list(figure.errors) + list(figure.warnings)
        row = dict(
            formula=_formula(c.slug, provenance),
            range=_range_text(figure.outside, provenance, c),
            engine=(provenance or {}).get("version") or "",
            note="; ".join(str(n) for n in notes),
        )
        if figure.value is None:
            return ResultRow(**base, **row, status=PROBLEM,
                             achieved="no value returned")
        if item.figure == FIGURE_CURRENT:
            return ResultRow(**base, **row, suggested=_mm(figure.value),
                             suggested_mm=float(figure.value))
        if item.figure == FIGURE_VIA:
            return ResultRow(**base, **row, achieved=_via_text(figure.values or {}),
                             achieved_value=float(figure.value))
        return ResultRow(**base, **row, achieved=_ohm(figure.value),
                         achieved_value=float(figure.value))

    def _target_row(self, item: PlannedItem, result: TargetResult) -> ResultRow:
        c = result.computation
        base = dict(
            netclass=item.netclass, layer=item.layer, figure=item.figure,
            label=figure_label(item), calculator=c.slug, current=self._current_text(item),
            cached=result.cached, computation=c, target=c.target_value,
        )
        if result.refusal is not None:
            return ResultRow(**base, status=REFUSED, note=result.refusal.message,
                             refusal=result.refusal)
        if result.value is None:
            return ResultRow(**base, status=PROBLEM, note="The API returned no value.")
        provenance = result.provenance
        row = dict(
            formula=_formula(c.slug, provenance),
            range=_range_text(result.outside, provenance, c.with_input(c.solve_for, result.value)),
            engine=(provenance or {}).get("version") or "",
            achieved=_ohm(result.achieved) if result.achieved is not None else "",
            achieved_value=result.achieved,
        )
        warnings = "; ".join(str(w) for w in result.warnings)
        if not result.reached:
            nearest = _ohm(result.achieved) if result.achieved is not None else "?"
            note = (
                f"{_num(c.target_value)} Ω is not reachable on {item.layer} within the "
                f"calculator's range; the nearest is {nearest} at {_mm(result.value)}."
            )
            return ResultRow(
                **base, **row, status=UNREACHABLE,
                suggested=f"unreachable (nearest {_mm(result.value)})",
                note="; ".join(x for x in (note, warnings) if x),
            )
        return ResultRow(**base, **row, suggested=_mm(result.value),
                         suggested_mm=float(result.value), note=warnings)

    def _current_text(self, item: PlannedItem) -> str:
        try:
            nc = self.netclass(item.netclass)
        except KeyError:
            return ""
        width = nc.mm("track_width_nm")
        pair_w, pair_g = nc.mm("diff_pair_width_nm"), nc.mm("diff_pair_gap_nm")
        if item.figure in (FIGURE_IMPEDANCE, FIGURE_WIDTH_FOR_TARGET, FIGURE_CURRENT):
            return _mm(width) if width is not None else "not set"
        if item.figure == FIGURE_DIFF_IMPEDANCE:
            return f"{_mm_bare(pair_w)} / {_mm(pair_g)} (width / gap)"
        if item.figure == FIGURE_GAP_FOR_TARGET:
            return _mm(pair_g) if pair_g is not None else "not set"
        if item.figure == FIGURE_VIA:
            pad, drill = nc.mm("via_diameter_nm"), nc.mm("via_drill_nm")
            return f"{_mm_bare(pad)} / {_mm(drill)} (pad / drill)"
        return ""

    # ── (e) The net-class write ──────────────────────────────────────────────

    def proposals(self) -> tuple:
        """``({class: {field: nm}}, notes)`` from the results, for the selected classes.

        Track width: the routing layer's width for the single-ended target;
        without a target, the IPC-2152 width for the class's current (rounded
        up to the grid) when the class is narrower. Differential-pair gap: the
        routing layer's gap for the differential target, at the class's pair
        width, which is kept.
        """
        if self.results_stale:
            return {}, ["The layer model changed after the run; run again first."]
        grid, _ = self.grid()
        grid_nm = N.mm_to_nm(grid or DEFAULT_GRID_MM)
        proposals: dict = {}
        notes: list = []
        for nc in self.netclasses:
            form = self.forms.get(nc.name)
            if form is None or not form.selected or form.layer is None:
                continue
            rows = [r for r in self.results if r.netclass == nc.name and r.layer == form.layer]
            width = _first(rows, FIGURE_WIDTH_FOR_TARGET)
            gap = _first(rows, FIGURE_GAP_FOR_TARGET)
            current = _first(rows, FIGURE_CURRENT)
            fields: dict = {}
            if width is not None and width.ok:
                fields[N.TRACK_WIDTH] = N.mm_to_nm(width.suggested_mm)
                notes.append(
                    f"{nc.name}: track width for {_num(width.target)} Ω on {form.layer} "
                    f"({width.calculator})."
                )
            elif width is not None and width.status == UNREACHABLE:
                notes.append(f"{nc.name}: no track width proposed; {width.note}")
            if current is not None and current.ok and current.suggested_mm is not None:
                ipc_nm = math.ceil(current.suggested_mm * NM_PER_MM / grid_nm - 1e-9) * grid_nm
                amps = current.computation.input_dict.get("current")
                if N.TRACK_WIDTH in fields:
                    if ipc_nm > fields[N.TRACK_WIDTH]:
                        notes.append(
                            f"{nc.name}: the width for the impedance target is narrower than "
                            f"the IPC-2152 width for {_num(amps)} A ({N.nm_text(ipc_nm)}); "
                            "the impedance width is proposed."
                        )
                elif nc.track_width_nm is None or ipc_nm > nc.track_width_nm:
                    fields[N.TRACK_WIDTH] = ipc_nm
                    notes.append(
                        f"{nc.name}: track width is the IPC-2152 width for {_num(amps)} A "
                        f"on {form.layer}, rounded up to the grid."
                    )
            if gap is not None and gap.ok:
                fields[N.DIFF_PAIR_GAP] = N.mm_to_nm(gap.suggested_mm)
                notes.append(
                    f"{nc.name}: pair gap for {_num(gap.target)} Ω differential on "
                    f"{form.layer}, at the class's pair width ({gap.calculator})."
                )
            elif gap is not None and gap.status == UNREACHABLE:
                notes.append(f"{nc.name}: no pair gap proposed; {gap.note}")
            if fields:
                proposals[nc.name] = fields
        return proposals, notes

    def kicad_version(self) -> tuple | None:
        """KiCad's ``(major, minor, patch)``, read once (``services.kicad.get_version()``)."""
        if self._kicad_version is _UNREAD:
            self._kicad_version = N.kicad_version(getattr(self.services, "kicad", None))
        return self._kicad_version

    def write_availability(self) -> tuple:
        return N.availability(self.services.project, self.kicad_version())

    def write_mode(self) -> tuple:
        """``(WRITE, None)``, ``(MANUAL, why the values are entered by hand)`` or
        ``(None, why neither)`` (no project, or kicad-python cannot send the write)."""
        project = self.services.project
        if project is not None:
            blocked = N.write_blocked(self.kicad_version())
            if blocked is not None:
                return MANUAL, blocked
        method, reason = self.write_availability()
        return (WRITE, None) if method is not None else (None, reason)

    def preview(self) -> tuple:
        """``(netclasses.Preview, None)`` or ``(None, why there is nothing to preview)``.

        In :data:`MANUAL` mode the preview's ``manual`` holds the reason: it is
        for entering the values in Board Setup, and :meth:`apply` refuses it.
        """
        mode, reason = self.write_mode()
        if mode is None:
            return None, reason
        proposals, notes = self.proposals()
        if not proposals:
            reason = " ".join(notes) or (
                "The results propose no change. Enter a target (or a current) for a selected "
                "class and run first."
            )
            return None, reason
        try:
            preview = N.make_preview(self.services.project, proposals, notes)
        except Exception as exc:  # KiCad did not answer
            return None, f"Could not read the net classes from KiCad ({type(exc).__name__}: {exc})."
        if preview.empty:
            return None, "Every selected class already has the proposed values."
        if mode == MANUAL:
            preview = replace(preview, manual=reason)
        return preview, None

    def apply(self, preview: N.Preview, confirm: Callable[[N.Preview], bool]) -> N.WriteResult:
        project = self.services.project
        try:
            result = N.apply(
                project, preview, confirm, version=self.kicad_version(),
                board=getattr(self.services, "board", None),
                history_file=self.services.history_path, project_key=N.project_path(project),
            )
        except Exception as exc:  # a connection lost mid-write, for one
            return N.WriteResult(False, f"The write failed ({type(exc).__name__}: {exc}).")
        if result.sent:
            self.reload_netclasses()
        return result

    def restore_preview(self) -> tuple:
        """As :meth:`preview`, for writing back the latest write. The history is
        read in :data:`MANUAL` mode too, and the values shown for entering by hand."""
        mode, reason = self.write_mode()
        if mode is None:
            return None, reason
        project = self.services.project
        try:
            preview, why = N.restore_preview(
                project, history_file=self.services.history_path,
                project_key=N.project_path(project),
            )
        except Exception as exc:
            return None, f"Could not prepare the restore ({type(exc).__name__}: {exc})."
        if preview is not None and mode == MANUAL:
            preview = replace(preview, manual=reason)
        return preview, why

    def reload_netclasses(self) -> None:
        """Read the classes again after a write, so the current values shown are KiCad's."""
        try:
            fresh = [NetClassValues.from_kipy(nc) for nc in self.services.project.get_net_classes()]
        except Exception:
            return
        self.netclasses = fresh
        for nc in fresh:
            if nc.name not in self.forms:
                self.forms[nc.name] = ClassForm(nc.name, layer=(self.trace_layers() or [None])[0])

    # ── Options kept between runs ────────────────────────────────────────────

    def saved_options(self) -> dict:
        options = dict(self.services.settings.options or {})
        options["solder_mask_cover"] = self.solder_mask_cover
        grid, error = self.grid()
        if error is None:
            options["grid"] = grid
        return options


# ── Text ─────────────────────────────────────────────────────────────────────


def figure_label(item: PlannedItem) -> str:
    c = item.computation
    target = _num(c.target_value) if c is not None and c.target_value is not None else None
    if item.figure == FIGURE_IMPEDANCE:
        label = "Z₀ at the class width"
        if c is not None and c.slug == COPLANAR:
            label += f" (grounded CPW, gap {_mm(c.input_dict['gapWidth'])})"
        elif c is not None and c.slug == COVERED_MICROSTRIP:
            label += " (under solder mask)"
        return label
    if item.figure == FIGURE_WIDTH_FOR_TARGET:
        return f"Width for {target} Ω" if target else "Width for the target"
    if item.figure == FIGURE_DIFF_IMPEDANCE:
        return "Zdiff at the class width and gap"
    if item.figure == FIGURE_GAP_FOR_TARGET:
        return f"Gap for {target} Ω differential" if target else "Gap for the target"
    if item.figure == FIGURE_CURRENT:
        amps = c.input_dict.get("current") if c is not None else None
        return f"IPC-2152 width for {_num(amps)} A" if amps is not None else "IPC-2152 width"
    if item.figure == FIGURE_VIA:
        return "Via Z, C, L and current"
    return item.figure


def _formula(slug: str, provenance: dict | None) -> str:
    ref = (provenance or {}).get("formulaRef")
    if ref:
        return str(ref)
    return str(CALCULATORS.get(slug, {}).get("formulaRef") or "")


def _range_text(outside: tuple, provenance: dict | None, computation: Computation) -> str:
    """"inside", or "outside: " and each bound crossed (from the provenance's validRange)."""
    if (provenance or {}).get("validRange") is not None:
        notes = list(outside)
    else:  # no range from the service: the calculator's stated bounds
        notes = [flag.text() for flag in computation.out_of_range]
    return "outside: " + "; ".join(notes) if notes else "inside"


def _via_text(values: dict) -> str:
    parts = []
    for key, label, unit in (
        ("impedance", "Z", "Ω"),
        ("capacitancePF", "C", "pF"),
        ("inductanceNH", "L", "nH"),
        ("currentCapacityA", "I", "A"),
    ):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{label} {value:.4g} {unit}")
    return ", ".join(parts)


def _first(rows, figure: str) -> ResultRow | None:
    for row in rows:
        if row.figure == figure:
            return row
    return None


def _number(text: str, what: str, unit: str) -> tuple:
    """``(value or None when blank, error or None)``: a positive finite number."""
    text = (text or "").strip()
    if not text:
        return None, None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None, f"{what} must be a number in {unit}, not {text!r}."
    if not math.isfinite(value) or value <= 0:
        return None, f"{what} must be more than 0 {unit}."
    return value, None


def _num(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:.6g}"


def _mm_bare(value: float | None) -> str:
    return "?" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")


def _mm(value: float | None) -> str:
    return f"{_mm_bare(value)} mm"


def _ohm(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f} Ω"
