"""The run budget: calls stated, with the allowance left, before any calculation request."""
from __future__ import annotations

from rftools_kicad import api as A
from rftools_kicad.budget import (
    FIGURE_CURRENT,
    FIGURE_DIFF_IMPEDANCE,
    FIGURE_GAP_FOR_TARGET,
    FIGURE_IMPEDANCE,
    FIGURE_VIA,
    FIGURE_WIDTH_FOR_TARGET,
    ClassRequest,
    estimate,
    plan_run,
)
from rftools_kicad.cache import ResultCache
from rftools_kicad.mapping import NetClassValues
from rftools_kicad.stackup import layer_model
from tests.support import USAGE, FakeClient, four_layer, missing_er

SE50 = NetClassValues(
    "SE50", track_width_nm=300000, clearance_nm=200000,
    via_diameter_nm=600000, via_drill_nm=300000,
)
USB = NetClassValues(
    "USB", track_width_nm=200000, diff_pair_width_nm=180000, diff_pair_gap_nm=150000,
    clearance_nm=150000, via_diameter_nm=450000, via_drill_nm=200000,
)


def reference_board():
    """Four layers with planes typed, and a thicker prepreg under B.Cu than over F.Cu,
    so the two outer layers are different microstrips (a symmetric board's outer
    layers are the same request, which is made once; see below)."""
    stackup = four_layer()
    stackup["layers"][4]["sublayers"][0]["thicknessNm"] = 1030000
    stackup["layers"][6]["sublayers"][0]["thicknessNm"] = 210000
    return layer_model(stackup)


def reference_requests():
    """A 50 Ω class and a 90 Ω pair on two layers each, with current and via checks."""
    return [
        ClassRequest(SE50, layers=("F.Cu", "B.Cu"), impedance_target=50,
                     current_a=2, via=True),
        ClassRequest(USB, layers=("F.Cu", "B.Cu"), single_ended=False, diff_target=90,
                     current_a=0.5, via=True),
    ]


def test_the_four_layer_reference_board_states_twelve_calls_and_the_remaining_allowance(tmp_path):
    model = reference_board()
    client = FakeClient()
    api = A.Api(client, ResultCache(tmp_path / "results.json"))

    plan = plan_run(model, reference_requests())
    budget = estimate(plan, api)

    figures = [(i.netclass, i.layer, i.figure) for i in plan.items]
    assert figures == [
        ("SE50", "F.Cu", FIGURE_IMPEDANCE), ("SE50", "F.Cu", FIGURE_WIDTH_FOR_TARGET),
        ("SE50", "B.Cu", FIGURE_IMPEDANCE), ("SE50", "B.Cu", FIGURE_WIDTH_FOR_TARGET),
        ("SE50", "F.Cu", FIGURE_CURRENT), ("SE50", None, FIGURE_VIA),
        ("USB", "F.Cu", FIGURE_DIFF_IMPEDANCE), ("USB", "F.Cu", FIGURE_GAP_FOR_TARGET),
        ("USB", "B.Cu", FIGURE_DIFF_IMPEDANCE), ("USB", "B.Cu", FIGURE_GAP_FOR_TARGET),
        ("USB", "F.Cu", FIGURE_CURRENT), ("USB", None, FIGURE_VIA),
    ]
    assert plan.problems == ()
    kinds = sorted(c.kind for c in plan.computations)
    assert kinds.count("solve") == 4 and kinds.count("calculate") == 8
    assert (budget.calls, budget.cached, budget.total) == (12, 0, 12)
    assert (budget.remaining, budget.allowance) == (40, 50)
    assert budget.fits is True
    assert budget.text() == (
        "This run will use at most 12 API calls; 40 of 50 remain this month, "
        "resetting 2026-10-01T00:00:00Z."
    )
    # Before any calculation request: only the free usage read was made.
    assert client.calls == [("usage", {})]


def test_cached_results_are_not_counted(tmp_path):
    model = reference_board()
    client = FakeClient()
    cache = ResultCache(tmp_path / "results.json")
    plan = plan_run(model, reference_requests())
    api = A.Api(client, cache)
    for computation in plan.computations[:5]:
        assert api.run(computation).ok
    budget = estimate(plan, A.Api(client, ResultCache(cache.path)))
    assert (budget.calls, budget.cached, budget.total) == (7, 5, 12)
    assert "(5 more answered from the cache)" in budget.text()


def test_a_free_allowance_that_does_not_cover_the_run_is_stated():
    model = reference_board()
    client = FakeClient(usage=dict(USAGE, allowance=5, used=0, remaining=5))
    budget = estimate(plan_run(model, reference_requests()), A.Api(client))
    assert budget.calls == 12 and budget.remaining == 5 and budget.fits is False


def test_an_unreadable_allowance_is_stated_not_guessed():
    client = FakeClient()
    client.usage_error = ConnectionError("offline")
    budget = estimate(plan_run(reference_board(), reference_requests()), A.Api(client))
    assert budget.remaining is None and budget.fits is None
    assert budget.usage_refusal.kind == A.OFFLINE
    assert "could not be read" in budget.text()


def test_a_symmetric_board_asks_once_for_identical_outer_layers():
    # F.Cu and B.Cu are the same microstrip here, so each width, gap and
    # impedance request is made once and the second layer is a cache hit.
    plan = plan_run(layer_model(four_layer()), reference_requests())
    assert len(plan.items) == 12 and len(plan.computations) == 8


def test_identical_requests_are_counted_once():
    model = layer_model(four_layer())
    twin = NetClassValues("SE50b", **{k: getattr(SE50, k) for k in (
        "track_width_nm", "clearance_nm", "via_diameter_nm", "via_drill_nm")})
    plan = plan_run(model, [
        ClassRequest(SE50, layers=("F.Cu",), via=True),
        ClassRequest(twin, layers=("F.Cu",), via=True),
    ])
    assert len(plan.items) == 4 and len(plan.computations) == 2


def test_what_cannot_be_planned_is_a_problem_not_a_call():
    model = layer_model(missing_er())
    bare = NetClassValues("Bare")
    plan = plan_run(model, [
        ClassRequest(SE50, layers=("In1.Cu",), impedance_target=50),
        ClassRequest(bare, layers=("F.Cu",), diff_target=100, current_a=1, via=True),
    ])
    problems = {(p.netclass, p.figure): p.problem for p in plan.problems}
    assert "dielectric 2 has no relative permittivity" in problems[("SE50", FIGURE_IMPEDANCE)]
    assert "no differential-pair width" in problems[("Bare", FIGURE_DIFF_IMPEDANCE)]
    assert "Bare has no via diameter or via drill" in problems[("Bare", FIGURE_VIA)]
    # Bare has no track width: its single-ended figure is named as missing, but
    # its current width needs none.
    assert "no track width" in problems[("Bare", FIGURE_IMPEDANCE)]
    assert [c.slug for c in plan.computations] == ["trace-width-current"]


def test_a_target_without_a_class_width_starts_from_the_calculators_default():
    model = layer_model(four_layer())
    plan = plan_run(model, [ClassRequest(NetClassValues("NoWidth"), layers=("F.Cu",),
                                         impedance_target=50)])
    [solve] = plan.computations
    assert solve.kind == "solve" and solve.input_dict["traceWidth"] == 1.2


def test_a_coplanar_gap_makes_outer_layers_grounded_cpw_and_leaves_inner_layers_stripline():
    from rftools_kicad.mapping import COPLANAR, CPW_GROUNDED, STRIPLINE
    from tests.support import six_layer

    model = layer_model(six_layer())
    request = ClassRequest(SE50, layers=("F.Cu", "In2.Cu"), impedance_target=50, cpw_gap_mm=0.2)
    plan = plan_run(model, [request])
    by = {(i.layer, i.figure): i.computation for i in plan.items}
    outer = by[("F.Cu", FIGURE_IMPEDANCE)]
    assert outer.slug == COPLANAR
    assert outer.input_dict["structure"] == CPW_GROUNDED
    assert outer.input_dict["gapWidth"] == 0.2 and outer.input_dict["traceWidth"] == 0.3
    solve = by[("F.Cu", FIGURE_WIDTH_FOR_TARGET)]
    assert solve.slug == COPLANAR and solve.solve_for == "traceWidth"
    assert solve.input_dict["gapWidth"] == 0.2
    assert solve.search_range == (0.02, 10.0)  # coplanar-waveguide states no maximum width
    assert by[("In2.Cu", FIGURE_IMPEDANCE)].slug == STRIPLINE
    assert by[("In2.Cu", FIGURE_WIDTH_FOR_TARGET)].slug == STRIPLINE
    assert not plan.problems
