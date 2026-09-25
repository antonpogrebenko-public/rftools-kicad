"""What a run will compute, and how many API calls it will spend, before any is spent.

:func:`plan_run` turns the user's selections — per net class: the layers it is
routed on, a single-ended or differential target, a current, whether to check
its via — into the list of computations a run makes (design Decision 3):

    per layer   a solve for the width (or a pair's gap) that reaches the target,
                and a forward call for the class's current width (or width and gap)
    per class   one IPC-2152 width for the current, on the layer the user names
                (else the first), and one via check

A class given a coplanar gap is computed as grounded coplanar waveguide
(``coplanar-waveguide``, structure 1) on the outer layers, where the plane
inward is the waveguide's ground: its single-ended figure and its width for a
target are taken at that gap to the side grounds. On an inner layer the same
class is stripline, as without the gap.

:func:`estimate` counts them — one call per computation, identical requests
once, minus those the result cache already answers — and reads the account's
remaining allowance from the usage endpoint, which is never metered. It makes
no calculation request (spec: "The plugin SHALL state the API calls a run will
use"). For the four-layer reference board (a 50 Ω class and a 90 Ω pair, each
on two layers, with current and via checks) that is 4 solves + 4 forward
figures + 2 current + 2 via = 12 calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from rftools_kicad.api import Api, Refusal
from rftools_kicad.cache import cache_key
from rftools_kicad.mapping import (
    DEFAULT_OPTIONS,
    Computation,
    MappingError,
    NetClassValues,
    Options,
    coplanar,
    differential,
    input_spec,
    single_ended,
    trace_current,
    via_for_class,
)
from rftools_kicad.solve import DEFAULT_GRID_MM, target_request
from rftools_kicad.stackup import LayerModel

FIGURE_IMPEDANCE = "impedance"  # forward: the class's track width
FIGURE_WIDTH_FOR_TARGET = "width-for-target"  # solve: traceWidth
FIGURE_DIFF_IMPEDANCE = "differential-impedance"  # forward: the class's pair width and gap
FIGURE_GAP_FOR_TARGET = "gap-for-target"  # solve: traceSpacing
FIGURE_CURRENT = "current-width"  # forward: IPC-2152 width
FIGURE_VIA = "via"  # forward: via Z, C, L, current


@dataclass(frozen=True)
class ClassRequest:
    """What the user asked of one net class."""

    netclass: NetClassValues
    #: The copper layers the class is routed on.
    layers: tuple = ()
    #: Compute single-ended impedance at the class's track width on each layer.
    single_ended: bool = True
    #: Single-ended target, Ω: a width per layer (implies ``single_ended``).
    impedance_target: float | None = None
    #: Compute differential impedance at the class's pair width and gap.
    differential: bool = False
    #: Differential target, Ω: a gap per layer at the class's pair width
    #: (implies ``differential``).
    diff_target: float | None = None
    #: Current, A: one IPC-2152 width, on ``current_layer`` (else the first layer).
    current_a: float | None = None
    current_layer: str | None = None
    #: Check the class's via.
    via: bool = False
    #: Coplanar gap to the side grounds, mm: single-ended figures on outer
    #: layers are grounded coplanar waveguide at this gap.
    cpw_gap_mm: float | None = None


@dataclass(frozen=True)
class PlannedItem:
    """One figure of a run: its computation, or why it cannot be computed."""

    netclass: str
    layer: str | None
    figure: str
    computation: Computation | None = None
    problem: str | None = None


@dataclass(frozen=True)
class Plan:
    items: tuple

    @property
    def computations(self) -> tuple:
        """Every distinct request the run makes, in order (identical ones once)."""
        seen = set()
        out = []
        for item in self.items:
            if item.computation is None:
                continue
            key = cache_key(item.computation.payload())
            if key not in seen:
                seen.add(key)
                out.append(item.computation)
        return tuple(out)

    @property
    def problems(self) -> tuple:
        return tuple(item for item in self.items if item.problem is not None)


@dataclass(frozen=True)
class Budget:
    """The calls a run will spend against what the account has left."""

    calls: int  # the most calls the run will spend: one per uncached computation
    cached: int  # computations the result cache answers for nothing
    total: int  # distinct computations in the plan
    remaining: int | None = None
    allowance: int | None = None
    used: int | None = None
    reset_at: str | None = None
    usage_refusal: Refusal | None = None

    @property
    def fits(self) -> bool | None:
        """Whether the run fits the allowance left; None when that is unknown."""
        return None if self.remaining is None else self.calls <= self.remaining

    def text(self) -> str:
        calls = f"This run will use at most {self.calls} API call{'' if self.calls == 1 else 's'}"
        if self.cached:
            calls += f" ({self.cached} more answered from the cache)"
        if self.remaining is None:
            reason = f": {self.usage_refusal.message}" if self.usage_refusal else ""
            return f"{calls}. The allowance remaining could not be read{reason}"
        left = f"{self.remaining}"
        if self.allowance is not None:
            left += f" of {self.allowance}"
        reset = f", resetting {self.reset_at}" if self.reset_at else ""
        return f"{calls}; {left} remain this month{reset}."


def plan_run(
    model: LayerModel,
    requests: list | tuple,
    options: Options = DEFAULT_OPTIONS,
    grid: float | None = DEFAULT_GRID_MM,
) -> Plan:
    items: list = []
    for request in requests:
        name = request.netclass.name
        for layer in request.layers:
            if request.single_ended or request.impedance_target is not None:
                items.extend(_single_ended_items(model, request, layer, options, grid))
            if request.differential or request.diff_target is not None:
                items.extend(_pair_items(model, request, layer, grid))
        if request.current_a is not None:
            layer = request.current_layer or (request.layers[0] if request.layers else None)
            if layer is None:
                items.append(PlannedItem(
                    name, None, FIGURE_CURRENT,
                    problem=f"Choose the layer net class {name}'s current flows on.",
                ))
            else:
                items.append(_item(
                    name, layer, FIGURE_CURRENT,
                    trace_current, model, layer, request.current_a, options,
                ))
        if request.via:
            items.append(_item(
                name, None, FIGURE_VIA, via_for_class, model, request.netclass, options
            ))
    return Plan(tuple(items))


def estimate(plan: Plan, api: Api) -> Budget:
    """Count the run's calls and read the allowance left; spends nothing."""
    computations = plan.computations
    cached = sum(1 for c in computations if api.cached(c) is not None)
    usage, refusal = api.usage()
    usage = usage or {}
    return Budget(
        calls=len(computations) - cached,
        cached=cached,
        total=len(computations),
        remaining=usage.get("remaining"),
        allowance=usage.get("allowance"),
        used=usage.get("used"),
        reset_at=usage.get("resetAt"),
        usage_refusal=refusal,
    )


# ── Items ────────────────────────────────────────────────────────────────────


def _single_ended_items(model, request, layer, options, grid):
    nc = request.netclass
    width_mm = nc.mm("track_width_nm")
    items = []
    gap = request.cpw_gap_mm
    if width_mm is not None:
        items.append(_item(
            nc.name, layer, FIGURE_IMPEDANCE, _se_forward, model, layer, width_mm, options, gap
        ))
    elif request.impedance_target is None:
        items.append(PlannedItem(
            nc.name, layer, FIGURE_IMPEDANCE,
            problem=f"Net class {nc.name} has no track width.",
        ))
    if request.impedance_target is not None:
        items.append(_item(
            nc.name, layer, FIGURE_WIDTH_FOR_TARGET,
            _width_solve, model, layer, width_mm, request.impedance_target, options, grid, gap,
        ))
    return items


def _se_forward(model, layer, width_mm, options, cpw_gap_mm=None) -> Computation:
    """Single-ended impedance on *layer*: coplanar on an outer layer when a gap is given."""
    structure = model.structure(layer)
    if cpw_gap_mm is not None and structure is not None and structure.kind == "microstrip":
        return coplanar(model, layer, width_mm, cpw_gap_mm, grounded=True)
    return single_ended(model, layer, width_mm, options)


def _width_solve(model, layer, width_mm, target, options, grid, cpw_gap_mm=None) -> Computation:
    """The width solve, starting at the class's width, else the calculator's default."""
    if width_mm is None:
        slug = _se_forward(model, layer, 1.0, options, cpw_gap_mm).slug
        width_mm = input_spec(slug, "traceWidth")["default"]
    return target_request(_se_forward(model, layer, width_mm, options, cpw_gap_mm), target, grid)


def _gap_solve(model, layer, width_mm, gap_mm, target, grid) -> Computation:
    """The gap solve at the class's pair width, starting at its gap, else the default."""
    if gap_mm is None:
        slug = differential(model, layer, width_mm, 1.0).slug
        gap_mm = input_spec(slug, "traceSpacing")["default"]
    return target_request(differential(model, layer, width_mm, gap_mm), target, grid)


def _pair_items(model, request, layer, grid):
    nc = request.netclass
    width_mm, gap_mm = nc.mm("diff_pair_width_nm"), nc.mm("diff_pair_gap_nm")
    items = []
    if width_mm is None:
        items.append(PlannedItem(
            nc.name, layer, FIGURE_DIFF_IMPEDANCE,
            problem=f"Net class {nc.name} has no differential-pair width.",
        ))
        return items
    if gap_mm is not None:
        items.append(_item(
            nc.name, layer, FIGURE_DIFF_IMPEDANCE, differential, model, layer, width_mm, gap_mm
        ))
    elif request.diff_target is None:
        items.append(PlannedItem(
            nc.name, layer, FIGURE_DIFF_IMPEDANCE,
            problem=f"Net class {nc.name} has no differential-pair gap.",
        ))
    if request.diff_target is not None:
        items.append(_item(
            nc.name, layer, FIGURE_GAP_FOR_TARGET,
            _gap_solve, model, layer, width_mm, gap_mm, request.diff_target, grid,
        ))
    return items


def _item(netclass: str, layer: str | None, figure: str, build, *args) -> PlannedItem:
    """Build one item's computation; a layer or class that cannot be mapped is a problem."""
    try:
        computation = build(*args)
    except (MappingError, KeyError) as exc:
        message = exc.args[0] if exc.args else str(exc)
        return PlannedItem(netclass, layer, figure, problem=str(message))
    return PlannedItem(netclass, layer, figure, computation=computation)
