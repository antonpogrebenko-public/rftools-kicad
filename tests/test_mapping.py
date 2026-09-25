"""Layer model + net class -> calculator inputs (design Decision 2), exactly."""
from __future__ import annotations

import pytest

from rftools_kicad import mapping as M
from rftools_kicad.stackup import layer_model
from tests.support import four_layer, kipy_netclass, missing_er, six_layer, two_layer


def er(*plies):
    num = 0.0
    den = 0
    for t, e in plies:
        num += t * e
        den += t
    return num / den


# ── Single-ended ─────────────────────────────────────────────────────────────


def test_outer_layer_is_bare_microstrip():
    c = M.single_ended(layer_model(two_layer()), "F.Cu", 0.3)
    assert c.slug == "microstrip-impedance" and c.output == "impedance"
    assert c.inputs == (
        ("traceWidth", 0.3), ("substrateHeight", 1.51),
        ("dielectricConstant", 4.5), ("copperThickness", 35.0),
    )


def test_outer_layer_under_solder_mask_uses_controlled_impedance_embedded():
    options = M.Options(solder_mask_cover=True)
    c = M.single_ended(layer_model(four_layer()), "B.Cu", 0.35, options)
    assert c.slug == "controlled-impedance"
    assert c.input_dict == {
        "traceType": 1, "traceWidth": 0.35, "substrateHeight": 0.2, "dielectricConst": 4.4,
        "copperThickness": 35.0, "coverHeight": 0.01, "coverDielectric": 3.3,
    }


def test_solder_mask_without_permittivity_is_named():
    stackup = two_layer()
    stackup["layers"][0]["epsilonR"] = None
    with pytest.raises(M.MappingError, match="F.Mask has no relative permittivity"):
        M.single_ended(layer_model(stackup), "F.Cu", 0.3, M.Options(solder_mask_cover=True))


def test_inner_layer_is_asymmetric_stripline_near_and_far():
    model = layer_model(four_layer(roles=False))
    c = M.single_ended(model, "In1.Cu", 0.2)
    assert c.slug == "asymmetric-stripline"
    assert c.input_dict == {
        "traceWidth": 0.2, "heightToNearPlane": 0.2, "heightToFarPlane": 1.04,
        "copperThickness": 35.0,
        "dielectricConst": er((200000, 4.4), (1040000, 4.6)),
    }


def test_six_layer_stripline_combines_prepreg_and_core():
    c = M.single_ended(layer_model(six_layer()), "In2.Cu", 0.12)
    assert c.input_dict == {
        "traceWidth": 0.12,
        "heightToNearPlane": 0.415,
        "heightToFarPlane": 0.6625,  # 230 µm prepreg + In3.Cu's 17.5 µm + 415 µm core
        "copperThickness": 17.5,
        "dielectricConst": er((415000, 4.6), (230000, 4.4), (415000, 4.6)),
    }


def test_a_layer_that_cannot_be_modelled_says_why():
    with pytest.raises(M.MappingError, match="dielectric 2 has no relative permittivity"):
        M.single_ended(layer_model(missing_er()), "In1.Cu", 0.2)
    with pytest.raises(M.MappingError, match="reference plane"):
        M.single_ended(layer_model(four_layer()), "In1.Cu", 0.2)
    with pytest.raises(KeyError):
        M.single_ended(layer_model(two_layer()), "In9.Cu", 0.2)


# ── Coplanar waveguide ───────────────────────────────────────────────────────


def test_coplanar_waveguide_on_an_outer_layer():
    c = M.coplanar(layer_model(two_layer()), "F.Cu", 1.0, 0.25)
    assert c.slug == "coplanar-waveguide"
    assert c.input_dict == {
        "structure": 1, "traceWidth": 1.0, "gapWidth": 0.25, "substrateHeight": 1.51,
        "dielectricConst": 4.5, "copperThickness": 35.0,
    }
    assert M.coplanar(layer_model(two_layer()), "F.Cu", 1.0, 0.25, grounded=False).input_dict[
        "structure"
    ] == 0
    with pytest.raises(M.MappingError, match="inner layer"):
        M.coplanar(layer_model(six_layer()), "In2.Cu", 0.2, 0.2)


# ── Pairs ────────────────────────────────────────────────────────────────────


def test_outer_pair_is_differential_pair_with_dielectric_constant():
    c = M.differential(layer_model(four_layer()), "F.Cu", 0.15, 0.2)
    assert c.slug == "differential-pair" and c.output == "zdiff"
    assert c.input_dict == {
        "traceWidth": 0.15, "traceSpacing": 0.2, "substrateHeight": 0.2,
        "dielectricConstant": 4.4, "copperThickness": 35.0,
    }


def test_inner_pair_off_centre_is_the_asymmetric_calculator():
    c = M.differential(layer_model(six_layer()), "In2.Cu", 0.1, 0.15)
    assert c.slug == "edge-coupled-internal-asymmetric" and c.output == "diffImpedance"
    assert c.input_dict == {
        "traceWidth": 0.1, "traceSpacing": 0.15, "heightBelow": 0.6625, "heightAbove": 0.415,
        "copperThickness": 17.5,
        "dielectricConst": er((415000, 4.6), (230000, 4.4), (415000, 4.6)),
    }


def test_inner_pair_centred_is_the_symmetric_calculator_plane_to_plane():
    stackup = four_layer(roles=False)
    stackup["layers"][4]["sublayers"][0]["thicknessNm"] = 200000  # In1.Cu centred
    c = M.differential(layer_model(stackup), "In1.Cu", 0.12, 0.18)
    assert c.slug == "edge-coupled-internal-symmetric"
    # planeSpacing is plane to plane: both dielectrics and the trace's copper.
    assert c.input_dict == {
        "traceWidth": 0.12, "traceSpacing": 0.18, "planeSpacing": (200000 + 200000 + 35000) / 1e6,
        "copperThickness": 35.0, "dielectricConst": er((200000, 4.4), (200000, 4.6)),
    }


# ── Current ──────────────────────────────────────────────────────────────────


def test_current_on_an_outer_layer():
    c = M.trace_current(layer_model(two_layer()), "F.Cu", 2)
    assert c.slug == "trace-width-current" and c.output == "width2152mm"
    assert c.input_dict == {
        "current": 2, "copperWeight": 1.0, "tempRise": 10.0, "traceLength": 100, "isExternal": 1,
    }
    assert c.out_of_range == ()


def test_current_on_an_inner_half_ounce_layer_with_options():
    options = M.Options(temp_rise_c=20, trace_length_mm=50)
    c = M.trace_current(layer_model(six_layer()), "In3.Cu", 1.5, options)
    assert c.input_dict == {
        "current": 1.5, "copperWeight": 0.5, "tempRise": 20, "traceLength": 50, "isExternal": 0,
    }


def test_copper_weight_outside_half_to_four_ounces_is_flagged():
    stackup = two_layer()
    stackup["layers"][1]["thicknessNm"] = 12000  # 12 µm = 0.34 oz
    c = M.trace_current(layer_model(stackup), "F.Cu", 1)
    assert c.input_dict["copperWeight"] == 12000 / 1e3 / 35
    [flag] = c.out_of_range
    assert flag.key == "copperWeight" and (flag.min, flag.max) == (0.5, 4)
    assert "outside the calculator's stated range (0.5–4 oz)" in flag.text()


def test_current_needs_no_plane_but_needs_copper_thickness():
    model = layer_model(four_layer())
    assert M.trace_current(model, "In1.Cu", 1).input_dict["isExternal"] == 0
    stackup = two_layer()
    stackup["layers"][1]["thicknessNm"] = None
    with pytest.raises(M.MappingError, match="no copper thickness"):
        M.trace_current(layer_model(stackup), "F.Cu", 1)


# ── Vias ─────────────────────────────────────────────────────────────────────


def test_via_from_a_net_class_drill_is_via_diameter_and_pad_is_pad_diameter():
    nc = M.NetClassValues.from_kipy(kipy_netclass("Default"))
    c = M.via_for_class(layer_model(four_layer()), nc)
    assert c.slug == "via-calculator" and c.output == "impedance"
    assert c.input_dict == {
        "viaDiameter": 0.3,  # the calculator's "Via Drill Diameter"
        "padDiameter": 0.6,  # KiCad's net-class "via diameter"
        "antipadDiameter": 1.0,  # pad + 2 x clearance
        "boardThickness": 1.6,
        "dielectricConstant": er((200000, 4.4), (1040000, 4.6), (200000, 4.4)),
        "copperThickness": 25,  # plating: the calculator's default
        "signalLayer": 0,
    }


def test_via_options():
    nc = M.NetClassValues(
        "HS", via_diameter_nm=450000, via_drill_nm=200000, clearance_nm=150000,
    )
    options = M.Options(via_plating_um=20, antipad_clearance_mm=0.3)
    c = M.via_for_class(layer_model(two_layer()), nc, options)
    assert c.input_dict["antipadDiameter"] == 0.45 + 2 * 0.3
    assert c.input_dict["copperThickness"] == 20
    assert c.input_dict["dielectricConstant"] == 4.5


def test_via_needs_its_dimensions_and_the_board():
    nc = M.NetClassValues("NoVia", clearance_nm=200000)
    with pytest.raises(M.MappingError, match="NoVia has no via diameter or via drill"):
        M.via_for_class(layer_model(two_layer()), nc)
    full = M.NetClassValues.from_kipy(kipy_netclass("Default"))
    with pytest.raises(M.MappingError, match="no thickness or εr"):
        M.via_for_class(layer_model(missing_er()), full)


# ── Net classes and computations ─────────────────────────────────────────────


def test_net_class_values_from_kicad():
    nc = M.NetClassValues.from_kipy(
        kipy_netclass("USB", track=250000, diff_width=180000, diff_gap=150000, clearance=127000)
    )
    assert nc == M.NetClassValues(
        name="USB", track_width_nm=250000, diff_pair_width_nm=180000, diff_pair_gap_nm=150000,
        clearance_nm=127000, via_diameter_nm=600000, via_drill_nm=300000,
    )
    assert nc.mm("track_width_nm") == 0.25
    empty = M.NetClassValues.from_kipy(
        kipy_netclass("Bare", track=None, clearance=None, via_diameter=None, via_drill=None)
    )
    assert empty.track_width_nm is None and empty.via_diameter_nm is None


def test_every_mapping_uses_only_the_calculators_declared_keys_in_order():
    model = layer_model(four_layer(roles=False))
    nc = M.NetClassValues.from_kipy(kipy_netclass("Default"))
    computations = [
        M.single_ended(model, "F.Cu", 0.3),
        M.single_ended(model, "F.Cu", 0.3, M.Options(solder_mask_cover=True)),
        M.single_ended(model, "In1.Cu", 0.3),
        M.coplanar(model, "F.Cu", 0.3, 0.2),
        M.differential(model, "F.Cu", 0.2, 0.2),
        M.differential(model, "In1.Cu", 0.2, 0.2),
        M.trace_current(model, "In2.Cu", 1),
        M.via_for_class(model, nc),
    ]
    for c in computations:
        declared = [spec["key"] for spec in M.CALCULATORS[c.slug]["inputs"]]
        assert [k for k, _ in c.inputs] == declared, c.slug
        assert all(isinstance(v, (int, float)) for _, v in c.inputs)
        assert c.output in M.CALCULATORS[c.slug]["outputs"]


def test_payload_is_a_slug_and_numbers():
    c = M.single_ended(layer_model(two_layer()), "F.Cu", 0.3)
    assert c.payload() == {"slug": "microstrip-impedance", "inputs": c.input_dict}
    solve = c.as_solve("traceWidth", 50, grid=0.001)
    assert solve.payload() == {
        "slug": "microstrip-impedance", "inputs": c.input_dict, "solveFor": "traceWidth",
        "target": {"output": "impedance", "value": 50.0}, "grid": 0.001,
    }
    ranged = M.single_ended(layer_model(six_layer()), "In2.Cu", 0.1).as_solve(
        "traceWidth", 50, grid=0.001, search_range=(0.02, 10.0)
    )
    assert ranged.payload()["range"] == [0.02, 10.0]


def test_with_input_and_as_solve_refuse_unknown_keys():
    c = M.single_ended(layer_model(two_layer()), "F.Cu", 0.3)
    assert c.with_input("traceWidth", 2.784).input_dict["traceWidth"] == 2.784
    with pytest.raises(M.MappingError):
        c.with_input("gapWidth", 1)
    with pytest.raises(M.MappingError):
        c.as_solve("impedance", 50)


def test_complete_refuses_a_key_the_calculator_does_not_declare():
    # dielectricConst is controlled-impedance's spelling, not microstrip-impedance's.
    with pytest.raises(M.MappingError, match="has no input dielectricConst"):
        M._complete("microstrip-impedance", {
            "traceWidth": 1, "substrateHeight": 1, "dielectricConst": 4, "copperThickness": 35,
        })
    with pytest.raises(M.MappingError, match="not a finite number"):
        M._complete("microstrip-impedance", {
            "traceWidth": float("nan"), "substrateHeight": 1, "dielectricConstant": 4,
            "copperThickness": 35,
        })
