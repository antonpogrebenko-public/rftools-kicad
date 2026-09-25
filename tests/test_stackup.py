"""The stackup -> layer model step (design Decision 2; spec: the proposed layer model)."""
from __future__ import annotations

import pytest

from rftools_kicad.stackup import (
    MISSING_EPSILON_R,
    MISSING_PLANE,
    MISSING_THICKNESS,
    PLANES_FROM_ADJACENCY,
    PLANES_FROM_ROLES,
    PLANES_FROM_USER,
    StackupError,
    from_kipy,
    kicad_layer_name,
    layer_model,
)
from tests.support import four_layer, kipy_stackup, missing_er, six_layer, two_layer, with_role


def test_two_layer_is_microstrip_both_sides_over_the_other_copper():
    model = layer_model(two_layer())
    assert model.plane_source == PLANES_FROM_ADJACENCY
    assert model.planes == ("F.Cu", "B.Cu")
    top, bottom = model.structure("F.Cu"), model.structure("B.Cu")
    assert (top.kind, top.reference, top.substrate_height_nm) == ("microstrip", "B.Cu", 1510000)
    assert (bottom.kind, bottom.reference, bottom.substrate_height_nm) == (
        "microstrip", "F.Cu", 1510000,
    )
    assert top.epsilon_r == 4.5 and not top.combined
    assert model.board_thickness_nm == 1600000
    assert model.problems == ()


def test_four_layer_with_typed_planes_proposes_microstrip_over_the_adjacent_plane():
    # Spec scenario "A four-layer board".
    model = layer_model(four_layer(roles=True))
    assert model.plane_source == PLANES_FROM_ROLES
    assert model.planes == ("In1.Cu", "In2.Cu")
    assert [s.layer for s in model.structures] == ["F.Cu", "B.Cu"]  # planes carry no traces
    top = model.structure("F.Cu")
    assert (top.kind, top.reference, top.substrate_height_nm, top.epsilon_r) == (
        "microstrip", "In1.Cu", 200000, 4.4,
    )
    bottom = model.structure("B.Cu")
    assert (bottom.reference, bottom.substrate_height_nm) == ("In2.Cu", 200000)
    assert model.problems == ()
    assert "microstrip over In1.Cu" in top.describe()


def test_a_stackup_without_roles_treats_every_adjacent_copper_layer_as_a_plane():
    # KiCad 10 exposes no copper layer type, so this is what a real board gives.
    model = layer_model(four_layer(roles=False))
    assert model.plane_source == PLANES_FROM_ADJACENCY
    assert model.planes == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    inner = model.structure("In1.Cu")
    assert (inner.kind, inner.plane_above, inner.plane_below) == ("stripline", "F.Cu", "In2.Cu")
    assert (inner.height_above_nm, inner.height_below_nm) == (200000, 1040000)
    assert inner.near_height_nm == 200000 and inner.far_height_nm == 1040000
    expected = (200000 * 4.4 + 1040000 * 4.6) / 1240000
    assert inner.epsilon_r == expected
    assert model.structure("F.Cu").reference == "In1.Cu"


def test_prepreg_and_core_are_summed_and_thickness_weighted_and_the_model_says_so():
    # Spec scenario "Prepreg and core between a trace and its plane".
    model = layer_model(six_layer())
    top = model.structure("F.Cu")
    assert top.substrate_height_nm == 75000 + 115000
    assert top.epsilon_r == (75000 * 4.0 + 115000 * 4.2) / 190000
    assert top.combined
    assert "Combined 2 dielectric plies" in top.describe()

    inner = model.structure("In2.Cu")
    assert (inner.plane_above, inner.plane_below) == ("In1.Cu", "In4.Cu")
    assert inner.height_above_nm == 415000
    # In3.Cu lies between In2.Cu and its plane below: its copper adds height, not εr.
    assert inner.height_below_nm == 230000 + 17500 + 415000
    assert [s.material for s in inner.sublayers] == ["FR4 core", "7628 prepreg", "FR4 core"]
    num = 415000 * 4.6 + 230000 * 4.4 + 415000 * 4.6
    assert inner.epsilon_r == num / 1060000
    assert inner.combined and "thickness-weighted" in inner.describe()
    assert inner.copper_thickness_nm == 17500


def test_the_user_can_change_the_planes_and_heights_follow():
    # Spec scenario "The user corrects a plane".
    model = layer_model(six_layer())
    edited = model.with_planes(["In1.Cu", "In3.Cu", "In4.Cu"])
    assert edited.plane_source == PLANES_FROM_USER
    inner = edited.structure("In2.Cu")
    assert (inner.plane_below, inner.height_below_nm) == ("In3.Cu", 230000)
    assert edited.structure("In3.Cu") is None  # now a plane


def test_a_plane_override_must_name_copper_layers():
    with pytest.raises(StackupError):
        layer_model(two_layer(), planes=["In7.Cu"])


def test_a_mixed_layer_is_a_plane_and_a_trace_layer():
    stackup = with_role(four_layer(roles=True), "In1.Cu", "mixed")
    model = layer_model(stackup)
    assert "In1.Cu" in model.planes
    # In1.Cu carries traces too, but has no plane above it (F.Cu is signal).
    assert any(p.layer == "In1.Cu" and p.missing == MISSING_PLANE for p in model.problems)


def test_missing_permittivity_names_the_layer_and_skips_what_depends_on_it():
    # Spec scenario "A stackup the plugin cannot model".
    model = layer_model(missing_er())
    problems = [p for p in model.problems if p.missing == MISSING_EPSILON_R]
    assert len(problems) == 1
    problem = problems[0]
    assert problem.layer == "dielectric 2"
    assert problem.skipped == ("In1.Cu", "In2.Cu")
    assert "dielectric 2" in problem.message and "εr" in problem.message
    assert "In1.Cu, In2.Cu are not computed" in problem.message
    assert model.structure("In1.Cu") is None and model.structure("In2.Cu") is None
    assert model.structure("F.Cu") is not None and model.structure("B.Cu") is not None
    assert model.board_epsilon_r is None
    assert model.problems_for("In1.Cu") == [problem]


def test_missing_copper_thickness_names_the_copper_layer():
    stackup = two_layer()
    stackup["layers"][1]["thicknessNm"] = None
    model = layer_model(stackup)
    assert [(p.layer, p.missing, p.skipped) for p in model.problems] == [
        ("F.Cu", MISSING_THICKNESS, ("F.Cu",)),
    ]
    assert model.structure("F.Cu") is None and model.structure("B.Cu") is not None


def test_a_dielectric_with_no_plies_is_missing_both_properties():
    stackup = two_layer()
    stackup["layers"][2]["sublayers"] = []
    model = layer_model(stackup)
    assert {(p.layer, p.missing) for p in model.problems} == {
        ("dielectric 1", MISSING_THICKNESS), ("dielectric 1", MISSING_EPSILON_R),
    }
    assert model.structures == ()


def test_board_thickness_is_summed_when_not_stated():
    stackup = two_layer()
    del stackup["boardThicknessNm"]
    assert layer_model(stackup).board_thickness_nm == 10000 + 35000 + 1510000 + 35000 + 10000


def test_not_a_stackup():
    with pytest.raises(StackupError):
        layer_model({"layers": "nope"})
    with pytest.raises(StackupError):
        layer_model({"layers": [{"type": "dielectric", "sublayers": []}]})


def test_the_weighted_mean_matches_the_golden_generator_to_the_bit():
    # 0.115 + 0.075 in mm is not the double 0.19; nm summed then divided is.
    model = layer_model(six_layer())
    assert model.structure("F.Cu").substrate_height_nm / 1e6 == 0.19


# ── kicad-python ─────────────────────────────────────────────────────────────


def test_from_kipy_reads_the_stackup_kicad_sends():
    neutral = from_kipy(kipy_stackup(two_layer()))
    assert neutral["boardThicknessNm"] == 1600000
    assert [(layer["type"], layer["name"]) for layer in neutral["layers"]] == [
        ("soldermask", "F.Mask"), ("copper", "F.Cu"), ("dielectric", "dielectric 1"),
        ("copper", "B.Cu"), ("soldermask", "B.Mask"),
    ]
    copper = neutral["layers"][1]
    assert copper == {"name": "F.Cu", "id": "F.Cu", "type": "copper", "thicknessNm": 35000}
    assert "role" not in copper  # kicad-python 0.8 exposes none
    assert neutral["layers"][2]["sublayers"] == [
        {"material": "FR4", "thicknessNm": 1510000, "epsilonR": 4.5, "lossTangent": 0.02},
    ]
    assert neutral["layers"][2]["kind"] == "prepreg"
    assert neutral["layers"][0] == {
        "name": "F.Mask", "id": "F.Mask", "type": "soldermask",
        "thicknessNm": 10000, "epsilonR": 3.3,
    }


def test_from_kipy_accepts_the_wrapper_and_the_message():
    from kipy.board import BoardStackup

    message = kipy_stackup(six_layer())
    assert from_kipy(message) == from_kipy(BoardStackup(message))


def test_a_board_read_from_kicad_models_like_its_neutral_stackup_without_roles():
    for stackup in (two_layer(), four_layer(roles=False), six_layer()):
        from_kicad = layer_model(from_kipy(kipy_stackup(stackup)))
        neutral = layer_model(_without_roles(stackup))
        assert from_kicad.structures == neutral.structures
        assert from_kicad.board_epsilon_r == neutral.board_epsilon_r
        assert from_kicad.board_thickness_nm == neutral.board_thickness_nm


def test_from_kipy_reports_an_unset_permittivity_as_missing():
    model = layer_model(from_kipy(kipy_stackup(missing_er())))
    assert [(p.layer, p.missing) for p in model.problems] == [("dielectric 2", MISSING_EPSILON_R)]


def test_a_soldermask_without_details_has_no_permittivity():
    # KiCad before 10.0.6 sends no soldermask details.
    neutral = from_kipy(kipy_stackup(two_layer(), with_soldermask_details=False))
    assert neutral["layers"][0]["epsilonR"] is None
    assert neutral["layers"][0]["thicknessNm"] == 10000


def test_copper_layers_the_user_named_alike_are_told_apart():
    stackup = four_layer(roles=False)
    message = kipy_stackup(stackup)
    for layer in message.layers:
        if layer.user_name in ("In1.Cu", "In2.Cu"):
            layer.user_name = "GND"
    names = [entry["name"] for entry in from_kipy(message)["layers"] if entry["type"] == "copper"]
    assert names == ["F.Cu", "GND (In1.Cu)", "GND (In2.Cu)", "B.Cu"]


def test_kicad_layer_names():
    from kipy.proto.board.board_types_pb2 import BoardLayer

    assert kicad_layer_name(BoardLayer.BL_F_Cu) == "F.Cu"
    assert kicad_layer_name(BoardLayer.BL_In12_Cu) == "In12.Cu"
    assert kicad_layer_name(BoardLayer.BL_B_Mask) == "B.Mask"


def _without_roles(stackup: dict) -> dict:
    return {
        **stackup,
        "layers": [{k: v for k, v in layer.items() if k != "role"} for layer in stackup["layers"]],
    }
