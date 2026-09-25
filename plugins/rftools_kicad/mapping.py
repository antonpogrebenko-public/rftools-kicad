"""A layer of the model, plus net-class values, as calculator inputs (design Decision 2).

Every function here returns a :class:`Computation`: a calculator slug and its
complete input dict, in the calculator's declared order, with every value a
number. Nothing else leaves the plugin (spec: "The plugin SHALL send the
service only calculator inputs"); layer and class names stay in the plugin.

The calculators, and their keys, units, defaults and bounds, are the ones in
``calculators.json`` (extracted from the monorepo by
``scripts/extract_calculators.py``). Key names differ between calculators —
``dielectricConstant`` on ``microstrip-impedance``, ``differential-pair`` and
``via-calculator``, ``dielectricConst`` everywhere else — so each mapping
names its keys explicitly and :func:`_complete` refuses a key the calculator
does not declare.

Units: lengths in mm, copper thickness in µm, copper weight in oz (35 µm = 1
oz, the calculator's own convention). Values derived from the stackup are
computed as the golden generator computes them: nanometres summed as integers
and divided once.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rftools_kicad.stackup import NM_PER_MM, NM_PER_UM, LayerModel, Structure

MICROSTRIP = "microstrip-impedance"
COVERED_MICROSTRIP = "controlled-impedance"
STRIPLINE = "asymmetric-stripline"
COPLANAR = "coplanar-waveguide"
PAIR_OUTER = "differential-pair"
PAIR_INNER_SYMMETRIC = "edge-coupled-internal-symmetric"
PAIR_INNER_ASYMMETRIC = "edge-coupled-internal-asymmetric"
CURRENT = "trace-width-current"
VIA = "via-calculator"

#: The output each calculator's figure is read from.
PRIMARY_OUTPUT = {
    MICROSTRIP: "impedance",
    COVERED_MICROSTRIP: "impedance",
    STRIPLINE: "impedance",
    COPLANAR: "impedance",
    PAIR_OUTER: "zdiff",
    PAIR_INNER_SYMMETRIC: "diffImpedance",
    PAIR_INNER_ASYMMETRIC: "diffImpedance",
    CURRENT: "width2152mm",
    VIA: "impedance",
}

#: trace-width-current: 1 oz of copper is 35 µm.
OZ_UM = 35.0

#: controlled-impedance's traceType for a microstrip under a cover (solder mask).
TRACE_TYPE_EMBEDDED = 1
#: coplanar-waveguide's structure: 0 CPW, 1 grounded CPW.
CPW_UNGROUNDED, CPW_GROUNDED = 0, 1

CALCULATE = "calculate"
SOLVE = "solve"


class MappingError(ValueError):
    """A layer or a net class cannot be mapped to a calculator; the text says why."""


def _load_calculators() -> dict:
    path = Path(__file__).with_name("calculators.json")
    return json.loads(path.read_text(encoding="utf-8"))["calculators"]


#: slug -> {"title", "inputs": [{key, unit, default, min, max}], "outputs", "formulaRef"}
CALCULATORS: dict = _load_calculators()


# ── Values the dialog edits ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Options:
    """The user's choices that change inputs. Defaults are design Decision 2's."""

    #: Model an outer layer under its solder mask (controlled-impedance,
    #: traceType 1) instead of as bare microstrip.
    solder_mask_cover: bool = False
    #: trace-width-current: allowed temperature rise, °C.
    temp_rise_c: float = 10.0
    #: trace-width-current: trace length, mm (None: the calculator's default).
    trace_length_mm: float | None = None
    #: via-calculator: barrel plating, µm (None: the calculator's default, 25 µm).
    via_plating_um: float | None = None
    #: via-calculator: antipad clearance around the pad, mm (None: the net
    #: class's clearance).
    antipad_clearance_mm: float | None = None


DEFAULT_OPTIONS = Options()


@dataclass(frozen=True)
class NetClassValues:
    """The net-class fields the plugin reads, in nanometres (None: not set).

    kicad-python 0.8 names (``kipy.project_types.NetClass``): ``track_width``,
    ``diff_pair_track_width``, ``diff_pair_gap``, ``clearance``,
    ``via_diameter`` (``board.via_stack.copper_layers[0].size.x_nm``) and
    ``via_drill`` (``board.via_stack.drill.diameter.x_nm``).
    """

    name: str
    track_width_nm: int | None = None
    diff_pair_width_nm: int | None = None
    diff_pair_gap_nm: int | None = None
    clearance_nm: int | None = None
    via_diameter_nm: int | None = None
    via_drill_nm: int | None = None

    @classmethod
    def from_kipy(cls, netclass: Any) -> NetClassValues:
        return cls(
            name=netclass.name,
            track_width_nm=_positive(netclass.track_width),
            diff_pair_width_nm=_positive(netclass.diff_pair_track_width),
            diff_pair_gap_nm=_positive(netclass.diff_pair_gap),
            clearance_nm=_positive(netclass.clearance),
            via_diameter_nm=_positive(netclass.via_diameter),
            via_drill_nm=_positive(netclass.via_drill),
        )

    def mm(self, field_name: str) -> float | None:
        nm = getattr(self, field_name)
        return None if nm is None else nm / NM_PER_MM


# ── What is sent ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutOfRange:
    """An input outside the range the calculator is stated for."""

    key: str
    value: float
    min: float | None
    max: float | None
    unit: str

    def text(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        if self.min is not None and self.max is not None:
            span = f"{_n(self.min)}–{_n(self.max)}{unit}"
        elif self.min is not None:
            span = f"at least {_n(self.min)}{unit}"
        else:
            span = f"at most {_n(self.max)}{unit}"
        value = f"{_n(self.value)}{unit}"
        return f"{self.key} {value} is outside the calculator's stated range ({span})"


@dataclass(frozen=True)
class Computation:
    """One API request: ``calculate`` (forward) or ``solve`` (one input for a target).

    ``inputs`` is a tuple of ``(key, value)`` pairs in the calculator's
    declared order, so a Computation is hashable and its cache key is stable.
    ``output`` is the figure the dialog shows. :meth:`payload` is exactly what
    the service receives; nothing else here is sent.
    """

    kind: str
    slug: str
    inputs: tuple
    output: str
    solve_for: str | None = None
    target_value: float | None = None
    grid: float | None = None
    search_range: tuple | None = None
    out_of_range: tuple = field(default=(), compare=False)

    @property
    def input_dict(self) -> dict:
        return dict(self.inputs)

    def payload(self) -> dict:
        """The request body the service receives: a slug and numbers."""
        body: dict = {"slug": self.slug, "inputs": self.input_dict}
        if self.kind == SOLVE:
            body["solveFor"] = self.solve_for
            body["target"] = {"output": self.output, "value": self.target_value}
            if self.grid is not None:
                body["grid"] = self.grid
            if self.search_range is not None:
                body["range"] = list(self.search_range)
        return body

    def with_input(self, key: str, value: float) -> Computation:
        """The same computation with one input replaced (a forward check at a solved value)."""
        if key not in self.input_dict:
            raise MappingError(f"{self.slug} has no input {key}")
        inputs = tuple((k, value if k == key else v) for k, v in self.inputs)
        return replace(self, inputs=inputs, out_of_range=_out_of_range(self.slug, dict(inputs)))

    def as_solve(
        self,
        solve_for: str,
        target_value: float,
        grid: float | None = None,
        search_range: tuple | None = None,
    ) -> Computation:
        """A solve for *solve_for* bringing this computation's output to *target_value*.

        The current value of *solve_for* stays in the inputs: the service starts
        its search there and returns the crossing nearest it.
        """
        if solve_for not in self.input_dict:
            raise MappingError(f"{self.slug} has no input {solve_for} to solve for")
        return replace(
            self,
            kind=SOLVE,
            solve_for=solve_for,
            target_value=float(target_value),
            grid=grid,
            search_range=tuple(search_range) if search_range is not None else None,
        )


# ── Transmission lines ───────────────────────────────────────────────────────


def single_ended(
    model: LayerModel, layer: str, width_mm: float, options: Options = DEFAULT_OPTIONS
) -> Computation:
    """Single-ended impedance of a *width_mm* trace on *layer*.

    Outer layer: bare microstrip (``microstrip-impedance``), or, with
    ``options.solder_mask_cover``, ``controlled-impedance`` traceType 1 under
    the solder mask on that side. Inner layer: ``asymmetric-stripline`` between
    its two planes (the centred case when the heights are equal).
    """
    s = _structure(model, layer)
    t_um = s.copper_thickness_nm / NM_PER_UM
    if s.kind == "microstrip":
        h_mm = s.substrate_height_nm / NM_PER_MM
        if options.solder_mask_cover:
            mask = model.soldermask_over(layer)
            if mask is None:
                raise MappingError(
                    f"{layer} has no solder mask in the stackup to model as a cover."
                )
            if mask.thickness_nm is None or mask.epsilon_r is None:
                what = "thickness" if mask.thickness_nm is None else "relative permittivity (εr)"
                raise MappingError(
                    f"{mask.name} has no {what} in the stackup, so {layer} cannot be "
                    "computed under its solder mask. Compute it as bare microstrip instead."
                )
            return _complete(COVERED_MICROSTRIP, {
                "traceType": TRACE_TYPE_EMBEDDED,
                "traceWidth": width_mm,
                "substrateHeight": h_mm,
                "dielectricConst": s.epsilon_r,
                "copperThickness": t_um,
                "coverHeight": mask.thickness_nm / NM_PER_MM,
                "coverDielectric": mask.epsilon_r,
            })
        return _complete(MICROSTRIP, {
            "traceWidth": width_mm,
            "substrateHeight": h_mm,
            "dielectricConstant": s.epsilon_r,
            "copperThickness": t_um,
        })
    return _complete(STRIPLINE, {
        "traceWidth": width_mm,
        "heightToNearPlane": s.near_height_nm / NM_PER_MM,
        "heightToFarPlane": s.far_height_nm / NM_PER_MM,
        "copperThickness": t_um,
        "dielectricConst": s.epsilon_r,
    })


def coplanar(
    model: LayerModel, layer: str, width_mm: float, gap_mm: float, grounded: bool = True
) -> Computation:
    """Coplanar waveguide on an outer layer (grounded: the plane below is ground too)."""
    s = _structure(model, layer)
    if s.kind != "microstrip":
        raise MappingError(
            f"{layer} is an inner layer; coplanar waveguide is computed on outer layers."
        )
    return _complete(COPLANAR, {
        "structure": CPW_GROUNDED if grounded else CPW_UNGROUNDED,
        "traceWidth": width_mm,
        "gapWidth": gap_mm,
        "substrateHeight": s.substrate_height_nm / NM_PER_MM,
        "dielectricConst": s.epsilon_r,
        "copperThickness": s.copper_thickness_nm / NM_PER_UM,
    })


def differential(model: LayerModel, layer: str, width_mm: float, gap_mm: float) -> Computation:
    """Differential impedance of an edge-coupled pair on *layer*.

    Outer: ``differential-pair``. Inner: ``edge-coupled-internal-symmetric``
    when the trace is centred between its planes, whose ``planeSpacing`` is
    plane to plane and so includes the trace's copper thickness; otherwise
    ``edge-coupled-internal-asymmetric`` with ``heightAbove``/``heightBelow``,
    which are dielectric only, as the stripline heights are.
    """
    s = _structure(model, layer)
    t_um = s.copper_thickness_nm / NM_PER_UM
    if s.kind == "microstrip":
        return _complete(PAIR_OUTER, {
            "traceWidth": width_mm,
            "traceSpacing": gap_mm,
            "substrateHeight": s.substrate_height_nm / NM_PER_MM,
            "dielectricConstant": s.epsilon_r,
            "copperThickness": t_um,
        })
    if s.centred:
        plane_spacing_nm = s.height_above_nm + s.height_below_nm + s.copper_thickness_nm
        return _complete(PAIR_INNER_SYMMETRIC, {
            "traceWidth": width_mm,
            "traceSpacing": gap_mm,
            "planeSpacing": plane_spacing_nm / NM_PER_MM,
            "copperThickness": t_um,
            "dielectricConst": s.epsilon_r,
        })
    return _complete(PAIR_INNER_ASYMMETRIC, {
        "traceWidth": width_mm,
        "traceSpacing": gap_mm,
        "heightBelow": s.height_below_nm / NM_PER_MM,
        "heightAbove": s.height_above_nm / NM_PER_MM,
        "copperThickness": t_um,
        "dielectricConst": s.epsilon_r,
    })


# ── Current and vias ─────────────────────────────────────────────────────────


def trace_current(
    model: LayerModel, layer: str, current_a: float, options: Options = DEFAULT_OPTIONS
) -> Computation:
    """IPC-2152 width for *current_a* on *layer* (``trace-width-current``).

    ``copperWeight`` is the layer's copper in µm / 35; the calculator states
    0.5–4 oz, and a layer outside that is flagged in ``out_of_range``.
    ``isExternal`` is 1 on an outer layer. No reference plane is needed.
    """
    copper = model.copper_layer(layer)
    if copper.thickness_nm is None:
        raise MappingError(f"{layer} has no copper thickness in the stackup.")
    mapped = {
        "current": current_a,
        "copperWeight": copper.thickness_nm / NM_PER_UM / OZ_UM,
        "tempRise": options.temp_rise_c,
        "isExternal": 1 if copper.outer else 0,
    }
    defaults = []
    if options.trace_length_mm is None:
        defaults.append("traceLength")
    else:
        mapped["traceLength"] = options.trace_length_mm
    return _complete(CURRENT, mapped, from_defaults=defaults)


def via(
    model: LayerModel,
    pad_diameter_mm: float,
    drill_mm: float,
    antipad_diameter_mm: float,
    options: Options = DEFAULT_OPTIONS,
) -> Computation:
    """Via impedance, capacitance, inductance and current (``via-calculator``).

    The calculator's ``viaDiameter`` is the *drill* ("Via Drill Diameter") and
    ``padDiameter`` the pad: KiCad's net-class "via diameter" is the pad. The
    board thickness is the stackup's total, εr the thickness-weighted mean over
    every dielectric ply, and the plating the calculator's default unless the
    user sets one. ``signalLayer`` does not change any output; its default is sent.
    """
    if model.board_thickness_nm is None:
        raise MappingError(
            "The stackup does not give the board thickness, so vias are not computed."
        )
    if model.board_epsilon_r is None:
        raise MappingError(
            "A dielectric in the stackup has no thickness or εr, so vias are not computed."
        )
    mapped = {
        "viaDiameter": drill_mm,
        "padDiameter": pad_diameter_mm,
        "antipadDiameter": antipad_diameter_mm,
        "boardThickness": model.board_thickness_nm / NM_PER_MM,
        "dielectricConstant": model.board_epsilon_r,
    }
    defaults = ["signalLayer"]
    if options.via_plating_um is None:
        defaults.append("copperThickness")
    else:
        mapped["copperThickness"] = options.via_plating_um
    return _complete(VIA, mapped, from_defaults=defaults)


def via_for_class(
    model: LayerModel, netclass: NetClassValues, options: Options = DEFAULT_OPTIONS
) -> Computation:
    """:func:`via` from a net class: pad = via diameter, antipad = pad + 2 × clearance."""
    missing = [
        label for label, value in (
            ("via diameter", netclass.via_diameter_nm),
            ("via drill", netclass.via_drill_nm),
        ) if value is None
    ]
    if options.antipad_clearance_mm is None and netclass.clearance_nm is None:
        missing.append("clearance")
    if missing:
        raise MappingError(f"Net class {netclass.name} has no {' or '.join(missing)}.")
    if options.antipad_clearance_mm is None:
        antipad_mm = (netclass.via_diameter_nm + 2 * netclass.clearance_nm) / NM_PER_MM
    else:
        antipad_mm = netclass.via_diameter_nm / NM_PER_MM + 2 * options.antipad_clearance_mm
    return via(
        model,
        pad_diameter_mm=netclass.via_diameter_nm / NM_PER_MM,
        drill_mm=netclass.via_drill_nm / NM_PER_MM,
        antipad_diameter_mm=antipad_mm,
        options=options,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def calculator(slug: str) -> dict:
    try:
        return CALCULATORS[slug]
    except KeyError:
        raise MappingError(f"{slug} is not a calculator this plugin uses") from None


def input_spec(slug: str, key: str) -> dict:
    for spec in calculator(slug)["inputs"]:
        if spec["key"] == key:
            return spec
    raise MappingError(f"{slug} has no input {key}")


def _structure(model: LayerModel, layer: str) -> Structure:
    s = model.structure(layer)
    if s is None:
        model.copper_layer(layer)  # KeyError for a name that is not a copper layer
        reasons = " ".join(p.message for p in model.problems_for(layer))
        raise MappingError(reasons or f"{layer} is a reference plane, not a trace layer.")
    return s


def _complete(slug: str, mapped: Mapping[str, float], from_defaults=()) -> Computation:
    """Every input *slug* declares, in its order: the mapped value or its default."""
    specs = calculator(slug)["inputs"]
    declared = [spec["key"] for spec in specs]
    stray = [key for key in list(mapped) + list(from_defaults) if key not in declared]
    if stray:
        raise MappingError(f"{slug} has no input {', '.join(stray)}")
    inputs = []
    for spec in specs:
        key = spec["key"]
        if key in mapped:
            value = mapped[key]
        elif key in from_defaults:
            value = spec["default"]
        else:
            raise MappingError(f"{slug}: nothing maps {key}")
        if not _is_number(value):
            raise MappingError(f"{slug}.{key} is not a finite number: {value!r}")
        inputs.append((key, value))
    return Computation(
        kind=CALCULATE,
        slug=slug,
        inputs=tuple(inputs),
        output=PRIMARY_OUTPUT[slug],
        out_of_range=_out_of_range(slug, dict(inputs)),
    )


def _out_of_range(slug: str, inputs: Mapping[str, float]) -> tuple:
    flags = []
    for spec in calculator(slug)["inputs"]:
        value = inputs.get(spec["key"])
        if value is None:
            continue
        low, high = spec.get("min"), spec.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            flags.append(OutOfRange(spec["key"], value, low, high, spec.get("unit", "")))
    return tuple(flags)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _positive(value: int | None) -> int | None:
    return int(value) if value else None


def _n(value: float) -> str:
    return f"{value:.6g}"
