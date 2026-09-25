"""The track width, or a pair's gap, that reaches a target impedance (design Decision 3).

One API call per target: the layer's forward inputs, the input to solve for
(``traceWidth``, or ``traceSpacing`` for a pair's gap), the target output and
value, and the manufacturing grid (0.001 mm unless the user sets another) go to
``POST /v1/calculate/solve`` through the plugin's client. The service returns
the grid value and the forward result at exactly that value, so the impedance
the dialog shows beside a suggested width is a forward computation by the same
calculator the web page runs. There is no search in the plugin.

A target no value in the calculator's range reaches comes back ``reached:
false`` with the value that came nearest, and is shown as unreachable with
that nearest impedance (spec: "An unreachable target").

The search range is the input's stated bounds. Several calculators state only a
minimum for a width or gap (``asymmetric-stripline``, the internal pairs,
``coplanar-waveguide``, ``controlled-impedance``), and the service needs both
ends, so the plugin supplies ``[stated minimum, SEARCH_MAX_MM]`` for those.
"""
from __future__ import annotations

from dataclasses import dataclass

from rftools_kicad.api import Api, Outcome, Refusal, range_notes
from rftools_kicad.mapping import (
    PAIR_INNER_ASYMMETRIC,
    PAIR_INNER_SYMMETRIC,
    PAIR_OUTER,
    Computation,
    MappingError,
    input_spec,
)

DEFAULT_GRID_MM = 0.001

#: The upper end of the search when the calculator states no maximum, mm.
SEARCH_MAX_MM = 10.0

PAIR_SLUGS = (PAIR_OUTER, PAIR_INNER_SYMMETRIC, PAIR_INNER_ASYMMETRIC)


@dataclass(frozen=True)
class TargetResult:
    """What a target search came to, as plain data for the dialog or a report."""

    computation: Computation
    #: The value to use (on the grid), or the nearest one when not reached.
    value: float | None = None
    #: The service's own solution before grid rounding.
    unrounded: float | None = None
    #: The output the API computed at exactly ``value``.
    achieved: float | None = None
    reached: bool = False
    evaluations: int | None = None
    #: The solve's own warnings (other crossings, an unreachable target) and the
    #: forward result's.
    warnings: tuple = ()
    values: dict | None = None
    provenance: dict | None = None
    cached: bool = False
    refusal: Refusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and self.value is not None

    @property
    def nearest(self) -> float | None:
        """The closest value the service found when the target is unreachable."""
        return None if self.reached else self.value

    @property
    def engine_version(self) -> str | None:
        return (self.provenance or {}).get("version")

    @property
    def formula_ref(self) -> str | None:
        return (self.provenance or {}).get("formulaRef")

    @property
    def outside(self) -> tuple:
        """Why the returned value lies outside the calculator's range, if it does."""
        return range_notes(self.provenance)


def solve_for_key(forward: Computation) -> str:
    """The input a target search varies: a pair's gap, else the track width."""
    return "traceSpacing" if forward.slug in PAIR_SLUGS else "traceWidth"


def search_range(slug: str, key: str) -> tuple | None:
    """None when the calculator states both bounds; else ``[min, SEARCH_MAX_MM]``."""
    spec = input_spec(slug, key)
    low, high = spec.get("min"), spec.get("max")
    if low is not None and high is not None:
        return None
    if low is None:
        raise MappingError(f"{slug}.{key} states no minimum to search from")
    return (low, SEARCH_MAX_MM)


def target_request(
    forward: Computation,
    target_value: float,
    grid: float | None = DEFAULT_GRID_MM,
    solve_for: str | None = None,
) -> Computation:
    """The solve computation for *forward* reaching *target_value* on its output.

    *forward* carries the class's current width or gap, which the service
    uses as the start value: of several crossings it returns the nearest.
    """
    key = solve_for or solve_for_key(forward)
    return forward.as_solve(
        key, target_value, grid=grid, search_range=search_range(forward.slug, key)
    )


def run_target(api: Api, computation: Computation) -> TargetResult:
    """Spend at most one call (none when cached) and read the result."""
    return from_outcome(api.run(computation))


def from_outcome(outcome: Outcome) -> TargetResult:
    if not outcome.ok:
        return TargetResult(outcome.computation, refusal=outcome.refusal, cached=outcome.cached)
    return from_response(outcome.computation, outcome.response, cached=outcome.cached)


def from_response(computation: Computation, response: dict, cached: bool = False) -> TargetResult:
    """Read a solve response (design Decision 10) as a :class:`TargetResult`."""
    forward = response.get("result") or {}
    values = forward.get("values") or {}
    warnings = tuple(response.get("warnings") or ()) + tuple(forward.get("warnings") or ())
    return TargetResult(
        computation=computation,
        value=response.get("value"),
        unrounded=response.get("unrounded"),
        achieved=values.get(computation.output),
        reached=bool(response.get("reached")),
        evaluations=response.get("evaluations"),
        warnings=warnings,
        values=dict(values),
        provenance=forward.get("provenance"),
        cached=cached,
    )
