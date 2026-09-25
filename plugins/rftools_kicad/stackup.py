"""The board's stackup, as KiCad reports it, turned into a layer model.

Two steps, with a plain dict between them:

1. :func:`from_kipy` converts kicad-python's ``Board.get_stackup()`` into a
   *neutral stackup dict*. It is the only function here that knows KiCad's
   types, so everything after it is testable without KiCad, and the golden
   cases (``golden/kicad-golden.json``) store their stackups in this form::

       {"boardThicknessNm": 1600000,
        "layers": [                                   # top to bottom
          {"name": "F.Mask", "type": "soldermask", "thicknessNm": 10000, "epsilonR": 3.3},
          {"name": "F.Cu", "type": "copper", "thicknessNm": 35000, "role": "signal"},
          {"name": "dielectric 1", "type": "dielectric",
           "sublayers": [{"material": "FR4", "thicknessNm": 1510000,
                          "epsilonR": 4.5, "lossTangent": 0.02}]},
          ...]}

   ``role`` is one of ``signal``, ``power``, ``mixed``, ``jumper``, or absent.
   kicad-python 0.8 does not expose a copper layer's type, so a stackup read
   from KiCad 10 carries no role and the planes are proposed by adjacency.

2. :func:`layer_model` derives, for each copper layer, the structure the plugin
   would compute (design Decision 2): microstrip over the nearest reference
   plane inward on an outer layer, stripline between the nearest plane above
   and below on an inner layer. A height is the dielectric between the trace
   layer and its plane, plus the copper of any signal layer lying between
   them (its etched pattern is filled with resin, so it adds height but is
   not weighted into εr). With an adjacent plane that is the dielectric
   alone, which is the golden generator's rule. εr is the thickness-weighted
   mean of the dielectric plies between, computed exactly as the generator
   does (integer nanometres summed, then divided once;
   Σ(thicknessNm × εr) / Σ thicknessNm accumulated top to bottom).

Which copper layers are reference planes is a proposal the user can change:
``layer_model(stackup, planes=[...])`` or :meth:`LayerModel.with_planes`.
A layer missing a thickness or a permittivity is named in
:attr:`LayerModel.problems`, and every copper layer that depends on it is not
modelled (spec: "A stackup the plugin cannot model").
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

NM_PER_MM = 1e6
NM_PER_UM = 1e3

ROLES = ("signal", "power", "mixed", "jumper")
#: Copper layer roles that make a layer a reference plane.
PLANE_ROLES = ("power", "mixed")

#: How the planes were chosen.
PLANES_FROM_ROLES = "roles"
PLANES_FROM_ADJACENCY = "adjacency"
PLANES_FROM_USER = "override"

#: What a :class:`Problem` says is missing.
MISSING_THICKNESS = "thickness"
MISSING_EPSILON_R = "epsilonR"
MISSING_PLANE = "reference plane"

_PROPERTY_TEXT = {
    MISSING_THICKNESS: "thickness",
    MISSING_EPSILON_R: "relative permittivity (εr)",
}


class StackupError(ValueError):
    """The input is not a stackup at all (as opposed to one with gaps)."""


# ── The model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sublayer:
    """One physical dielectric ply. ``None`` means the stackup does not state it."""

    dielectric: str
    material: str
    thickness_nm: int | None
    epsilon_r: float | None
    loss_tangent: float | None

    @property
    def usable(self) -> bool:
        return self.thickness_nm is not None and self.epsilon_r is not None

    def describe(self) -> str:
        material = self.material or self.dielectric
        thickness = _mm_text(self.thickness_nm) if self.thickness_nm is not None else "? mm"
        er = f"εr {_num_text(self.epsilon_r)}" if self.epsilon_r is not None else "εr ?"
        return f"{thickness} {material} ({er})"


@dataclass(frozen=True)
class Copper:
    name: str
    position: int  # 0 is the top copper layer
    thickness_nm: int | None
    role: str | None
    outer: bool
    kicad_id: str | None = None  # KiCad's own layer name ("F.Cu"), when read from KiCad

    @property
    def thickness_um(self) -> float | None:
        return None if self.thickness_nm is None else self.thickness_nm / NM_PER_UM


@dataclass(frozen=True)
class Soldermask:
    name: str
    side: str  # "top" or "bottom"
    thickness_nm: int | None
    epsilon_r: float | None


@dataclass(frozen=True)
class Problem:
    """A stackup layer that lacks a property, or a copper layer with no plane.

    ``layer`` is the layer at fault (a dielectric, or the copper layer itself),
    ``missing`` what it lacks, and ``skipped`` the copper layers that are not
    computed because of it.
    """

    layer: str
    missing: str
    skipped: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Structure:
    """What the plugin computes on one copper layer.

    ``kind`` is ``"microstrip"`` (an outer layer over one plane) or
    ``"stripline"`` (an inner layer between two). Heights are in nanometres,
    from the trace layer to each plane: the dielectric between, plus the copper
    of any signal layer between (none when the plane is adjacent); the side with no plane
    is ``None``. ``sublayers`` are every ply between the trace and its
    plane(s), top to bottom, and ``epsilon_r`` their thickness-weighted mean.
    """

    layer: str
    kind: str
    plane_above: str | None
    plane_below: str | None
    height_above_nm: int | None
    height_below_nm: int | None
    epsilon_r: float
    sublayers: tuple[Sublayer, ...]
    copper_thickness_nm: int
    outer: bool

    @property
    def combined(self) -> bool:
        """More than one ply lies between the trace and its plane(s)."""
        return len(self.sublayers) > 1

    @property
    def reference(self) -> str | None:
        """A microstrip's plane."""
        return self.plane_below if self.plane_below is not None else self.plane_above

    @property
    def substrate_height_nm(self) -> int | None:
        """A microstrip's height: the dielectric to its one plane."""
        if self.kind != "microstrip":
            return None
        return self.height_below_nm if self.height_below_nm is not None else self.height_above_nm

    @property
    def near_height_nm(self) -> int | None:
        if self.kind != "stripline":
            return None
        return min(self.height_above_nm, self.height_below_nm)

    @property
    def far_height_nm(self) -> int | None:
        if self.kind != "stripline":
            return None
        return max(self.height_above_nm, self.height_below_nm)

    @property
    def centred(self) -> bool:
        return self.kind == "stripline" and self.height_above_nm == self.height_below_nm

    def describe(self) -> str:
        """One sentence for the dialog, stating when plies were combined."""
        if self.kind == "microstrip":
            head = (
                f"{self.layer}: microstrip over {self.reference}, "
                f"{_mm_text(self.substrate_height_nm)} of dielectric"
            )
        else:
            head = (
                f"{self.layer}: stripline between {self.plane_above} "
                f"({_mm_text(self.height_above_nm)} above) and {self.plane_below} "
                f"({_mm_text(self.height_below_nm)} below)"
            )
        er = f"εr {_num_text(self.epsilon_r)}"
        if self.combined:
            plies = "; ".join(s.describe() for s in self.sublayers)
            return (
                f"{head}, {er}. Combined {len(self.sublayers)} dielectric plies: the height "
                f"is their total and εr their thickness-weighted mean ({plies})."
            )
        return f"{head}, {er}."


@dataclass(frozen=True)
class LayerModel:
    stackup: Mapping[str, Any]
    board_thickness_nm: int | None
    copper: tuple[Copper, ...]
    soldermasks: tuple[Soldermask, ...]
    planes: tuple[str, ...]
    plane_source: str
    structures: tuple[Structure, ...]
    problems: tuple[Problem, ...]
    board_epsilon_r: float | None

    def structure(self, layer: str) -> Structure | None:
        for s in self.structures:
            if s.layer == layer:
                return s
        return None

    def copper_layer(self, name: str) -> Copper:
        for c in self.copper:
            if c.name == name:
                return c
        raise KeyError(f"{name} is not a copper layer of this stackup")

    def soldermask_over(self, layer: str) -> Soldermask | None:
        """The solder mask on an outer layer's side of the board."""
        copper = self.copper_layer(layer)
        if not copper.outer:
            return None
        side = "top" if copper.position == 0 else "bottom"
        for mask in self.soldermasks:
            if mask.side == side:
                return mask
        return None

    def problems_for(self, layer: str) -> list[Problem]:
        return [p for p in self.problems if layer in p.skipped or p.layer == layer]

    @property
    def copper_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.copper)

    def with_planes(self, planes: Iterable[str]) -> LayerModel:
        """The same stackup with the user's choice of reference planes."""
        return layer_model(self.stackup, planes=planes)


# ── Stackup dict -> layer model ──────────────────────────────────────────────


def layer_model(stackup: Mapping[str, Any], planes: Iterable[str] | None = None) -> LayerModel:
    """Derive the per-layer structures from a neutral stackup dict.

    ``planes`` overrides the proposal: the copper layer names to treat as
    reference planes. Without it, copper layers whose role is ``power`` or
    ``mixed`` are planes; when no layer has such a role (every stackup read
    from KiCad 10, which exposes none), every copper layer is a plane to its
    neighbours, which is the adjacency rule of design Decision 2.
    """
    layers = _layers(stackup)
    copper_rows: list[tuple[int, Mapping[str, Any]]] = [
        (i, layer) for i, layer in enumerate(layers) if layer["type"] == "copper"
    ]
    if not copper_rows:
        raise StackupError("the stackup has no copper layers")

    last = len(copper_rows) - 1
    copper: list[Copper] = []
    for position, (_, layer) in enumerate(copper_rows):
        role = layer.get("role")
        copper.append(Copper(
            name=str(layer["name"]),
            position=position,
            thickness_nm=_thickness(layer.get("thicknessNm")),
            role=role if role in ROLES else None,
            outer=position in (0, last),
            kicad_id=layer.get("id"),
        ))
    names = [c.name for c in copper]
    if len(set(names)) != len(names):
        raise StackupError("two copper layers share a name")

    # Dielectric plies, keyed by their index in `layers`.
    plies: dict[int, tuple[Sublayer, ...]] = {}
    dielectric_number = 0
    for i, layer in enumerate(layers):
        if layer["type"] != "dielectric":
            continue
        dielectric_number += 1
        name = str(layer.get("name") or f"dielectric {dielectric_number}")
        plies[i] = tuple(
            Sublayer(
                dielectric=name,
                material=str(sub.get("material") or ""),
                thickness_nm=_thickness(sub.get("thicknessNm")),
                epsilon_r=_epsilon(sub.get("epsilonR")),
                loss_tangent=_finite(sub.get("lossTangent")),
            )
            for sub in (layer.get("sublayers") or [])
        ) or (Sublayer(name, "", None, None, None),)  # a dielectric that states nothing

    soldermasks: list[Soldermask] = []
    first_copper, last_copper = copper_rows[0][0], copper_rows[-1][0]
    for i, layer in enumerate(layers):
        if layer["type"] != "soldermask":
            continue
        side = "top" if i < first_copper else "bottom" if i > last_copper else None
        if side is None:
            continue  # a mask between copper layers is not a stackup KiCad makes
        soldermasks.append(Soldermask(
            name=str(layer.get("name") or f"{side} mask"),
            side=side,
            thickness_nm=_thickness(layer.get("thicknessNm")),
            epsilon_r=_epsilon(layer.get("epsilonR")),
        ))

    plane_set, plane_source = _planes(copper, planes)
    problems = _Problems()

    for c in copper:
        if c.thickness_nm is None:
            problems.add(c.name, MISSING_THICKNESS, c.name)

    structures: list[Structure] = []
    for c in copper:
        if not _trace_layer(c, plane_set, plane_source):
            continue  # a plane carries no traces to compute
        index = copper_rows[c.position][0]
        above = _nearest_plane(copper, plane_set, c.position, step=-1)
        below = _nearest_plane(copper, plane_set, c.position, step=+1)
        if c.outer:
            # Microstrip over the nearest plane inward; the other side is air.
            if c.position == 0:
                above = None
            else:
                below = None
            if len(copper) == 1 or (above is None and below is None):
                problems.add(c.name, MISSING_PLANE, c.name, message=(
                    f"{c.name} has no reference plane inward, so it is not computed. "
                    "Mark a copper layer as a plane."
                ))
                continue
            kind = "microstrip"
        else:
            if above is None or below is None:
                side = "above" if above is None else "below"
                problems.add(c.name, MISSING_PLANE, c.name, message=(
                    f"{c.name} has no reference plane {side}, so it is not computed as "
                    "stripline. Mark a copper layer on each side as a plane."
                ))
                continue
            kind = "stripline"

        sub_above = _plies_between(plies, copper_rows[above.position][0], index) if above else ()
        sub_below = _plies_between(plies, index, copper_rows[below.position][0]) if below else ()
        cu_above = _copper_between(copper, above.position, c.position) if above else ()
        cu_below = _copper_between(copper, c.position, below.position) if below else ()
        if any(x.thickness_nm is None for x in cu_above + cu_below):
            continue  # that copper layer is already reported as missing its thickness
        bad = [s for s in sub_above + sub_below if not s.usable]
        for s in bad:
            if s.thickness_nm is None:
                problems.add(s.dielectric, MISSING_THICKNESS, c.name)
            if s.epsilon_r is None:
                problems.add(s.dielectric, MISSING_EPSILON_R, c.name)
        if bad or c.thickness_nm is None:
            continue

        sublayers = sub_above + sub_below  # top to bottom
        structures.append(Structure(
            layer=c.name,
            kind=kind,
            plane_above=above.name if above else None,
            plane_below=below.name if below else None,
            height_above_nm=_total_nm(sub_above) + _copper_nm(cu_above) if above else None,
            height_below_nm=_total_nm(sub_below) + _copper_nm(cu_below) if below else None,
            epsilon_r=weighted_epsilon(sublayers),
            sublayers=sublayers,
            copper_thickness_nm=c.thickness_nm,
            outer=c.outer,
        ))

    every_ply = [s for i in sorted(plies) for s in plies[i]]
    board_er = weighted_epsilon(every_ply) if all(s.usable for s in every_ply) else None
    board_thickness = _thickness(stackup.get("boardThicknessNm"))
    if board_thickness is None:
        board_thickness = _sum_thickness(plies, soldermasks, copper)

    return LayerModel(
        stackup=stackup,
        board_thickness_nm=board_thickness,
        copper=tuple(copper),
        soldermasks=tuple(soldermasks),
        planes=tuple(c.name for c in copper if c.name in plane_set),
        plane_source=plane_source,
        structures=tuple(structures),
        problems=problems.result(),
        board_epsilon_r=board_er,
    )


def weighted_epsilon(sublayers: Sequence[Sublayer]) -> float:
    """Σ(thicknessNm × εr) / Σ thicknessNm, accumulated in the order given.

    Kept in nanometres, as the golden generator computes it: the same mean
    taken on millimetres is a different double in the last place.
    """
    num = 0.0
    den = 0
    for s in sublayers:
        num += s.thickness_nm * s.epsilon_r
        den += s.thickness_nm
    return num / den


def nm_to_mm(nm: int) -> float:
    return nm / NM_PER_MM


def nm_to_um(nm: int) -> float:
    return nm / NM_PER_UM


# ── kicad-python -> stackup dict ─────────────────────────────────────────────


def from_kipy(board_stackup: Any) -> dict[str, Any]:
    """Convert ``Board.get_stackup()`` (kicad-python 0.8) to a neutral stackup dict.

    Accepts the ``kipy.board.BoardStackup`` wrapper or its ``board_pb2.BoardStackup``
    message. Silkscreen and solder paste entries are dropped. Thicknesses are in
    nanometres, as KiCad reports them; a value KiCad leaves unset (0) is written
    as ``None`` so the model names it as missing rather than computing with it.
    """
    from kipy.board import BoardStackup
    from kipy.proto.board import board_pb2

    if isinstance(board_stackup, board_pb2.BoardStackup):
        board_stackup = BoardStackup(board_stackup)

    layers: list[dict[str, Any]] = []
    dielectric_number = 0
    for layer in board_stackup.layers:
        kind = layer.type
        if kind == board_pb2.BSLT_COPPER:
            kicad_id = kicad_layer_name(layer.layer)
            layers.append({
                "name": layer.user_name or kicad_id,
                "id": kicad_id,
                "type": "copper",
                "thicknessNm": _nm_or_none(layer.thickness),
            })
        elif kind == board_pb2.BSLT_DIELECTRIC:
            dielectric_number += 1
            layers.append(_dielectric_from_kipy(layer, dielectric_number, board_pb2))
        elif kind == board_pb2.BSLT_SOLDERMASK:
            layers.append(_soldermask_from_kipy(layer))
        # BSLT_SILKSCREEN, BSLT_SOLDERPASTE and anything newer carry no
        # electrical property the calculators take.

    total = 0
    for entry in layers:
        parts = entry["sublayers"] if entry["type"] == "dielectric" else [entry]
        for part in parts:
            if part.get("thicknessNm") is None:
                total = None
                break
            total += part["thicknessNm"]
        if total is None:
            break
    _unique_copper_names(layers)
    return {"boardThicknessNm": total, "layers": layers}


def _unique_copper_names(layers: list[dict[str, Any]]) -> None:
    """Two copper layers the user gave one name are told apart by KiCad's own name."""
    names = [entry["name"] for entry in layers if entry["type"] == "copper"]
    for entry in layers:
        if entry["type"] == "copper" and names.count(entry["name"]) > 1:
            entry["name"] = f"{entry['name']} ({entry['id']})"


def kicad_layer_name(board_layer: int) -> str:
    """``BL_F_Cu`` -> ``F.Cu``, ``BL_In1_Cu`` -> ``In1.Cu``, ``BL_F_Mask`` -> ``F.Mask``."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    name = BoardLayer.Name(board_layer)
    if name.startswith("BL_"):
        name = name[3:]
    return name.replace("_", ".", 1)


def _dielectric_from_kipy(layer: Any, number: int, board_pb2: Any) -> dict[str, Any]:
    details = layer.dielectric
    sublayers = []
    if details is not None and details.layers:
        for ply in details.layers:
            sublayers.append({
                "material": ply.material_name,
                "thicknessNm": _nm_or_none(ply.thickness),
                "epsilonR": ply.epsilon_r or None,
                "lossTangent": ply.loss_tangent,
            })
    else:
        # No per-ply detail: the slot's own thickness, and no εr to compute with.
        sublayers.append({
            "material": layer.material_name,
            "thicknessNm": _nm_or_none(layer.thickness),
            "epsilonR": None,
            "lossTangent": None,
        })
    entry: dict[str, Any] = {
        "name": f"dielectric {number}",
        "type": "dielectric",
        "sublayers": sublayers,
    }
    if details is not None:
        kind = {board_pb2.BSDT_CORE: "core", board_pb2.BSDT_PREPREG: "prepreg"}.get(details.type)
        if kind:
            entry["kind"] = kind
    return entry


def _soldermask_from_kipy(layer: Any) -> dict[str, Any]:
    kicad_id = kicad_layer_name(layer.layer)
    mask = layer.soldermask
    epsilon_r = mask.epsilon_r if mask is not None else None
    thickness = mask.thickness if mask is not None else 0
    if not epsilon_r and layer.dielectric is not None and layer.dielectric.layers:
        # KiCad before 10.0.6 has no soldermask details; its εr, when sent,
        # came as a dielectric ply.
        epsilon_r = layer.dielectric.layers[0].epsilon_r
    return {
        "name": layer.user_name or kicad_id,
        "id": kicad_id,
        "type": "soldermask",
        "thicknessNm": _nm_or_none(thickness or layer.thickness),
        "epsilonR": epsilon_r or None,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


class _Problems:
    """Problems keyed by (layer, missing), each naming every copper layer it skips."""

    def __init__(self) -> None:
        self._order: list[tuple[str, str]] = []
        self._skipped: dict[tuple[str, str], list[str]] = {}
        self._messages: dict[tuple[str, str], str | None] = {}

    def add(self, layer: str, missing: str, skipped: str, message: str | None = None) -> None:
        key = (layer, missing)
        if key not in self._skipped:
            self._order.append(key)
            self._skipped[key] = []
            self._messages[key] = message
        if skipped not in self._skipped[key]:
            self._skipped[key].append(skipped)

    def result(self) -> tuple[Problem, ...]:
        out = []
        for layer, missing in self._order:
            skipped = tuple(self._skipped[(layer, missing)])
            message = self._messages[(layer, missing)]
            if message is None:
                verb = "is" if len(skipped) == 1 else "are"
                message = (
                    f"{layer} has no {_PROPERTY_TEXT.get(missing, missing)} in the stackup, "
                    f"so {', '.join(skipped)} {verb} not computed."
                )
            out.append(Problem(layer=layer, missing=missing, skipped=skipped, message=message))
        return tuple(out)


def _layers(stackup: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(stackup, Mapping) or not isinstance(stackup.get("layers"), list):
        raise StackupError("a stackup is a dict with a 'layers' list")
    out = []
    for layer in stackup["layers"]:
        if not isinstance(layer, Mapping) or layer.get("type") not in (
            "soldermask", "copper", "dielectric",
        ):
            continue
        if layer["type"] != "dielectric" and not layer.get("name"):
            raise StackupError(f"a {layer['type']} layer has no name")
        out.append(layer)
    return out


def _planes(
    copper: Sequence[Copper], override: Iterable[str] | None
) -> tuple[set, str]:
    names = {c.name for c in copper}
    if override is not None:
        chosen = set(override)
        unknown = chosen - names
        if unknown:
            raise StackupError(f"not copper layers of this stackup: {', '.join(sorted(unknown))}")
        return chosen, PLANES_FROM_USER
    by_role = {c.name for c in copper if c.role in PLANE_ROLES}
    if by_role:
        return by_role, PLANES_FROM_ROLES
    return set(names), PLANES_FROM_ADJACENCY


def _trace_layer(c: Copper, planes: set, source: str) -> bool:
    """Whether traces are computed on *c*.

    Under the adjacency rule every copper layer is both a plane to its
    neighbours and a trace layer. Otherwise a plane is not a trace layer,
    unless its role is ``mixed`` (planes and signals on one layer).
    """
    if source == PLANES_FROM_ADJACENCY:
        return True
    return c.name not in planes or c.role == "mixed"


def _nearest_plane(
    copper: Sequence[Copper], planes: set, position: int, step: int
) -> Copper | None:
    i = position + step
    while 0 <= i < len(copper):
        if copper[i].name in planes:
            return copper[i]
        i += step
    return None


def _plies_between(
    plies: Mapping[int, tuple[Sublayer, ...]], a: int, b: int
) -> tuple[Sublayer, ...]:
    top, bottom = (a, b) if a < b else (b, a)
    return tuple(s for i in sorted(plies) if top < i < bottom for s in plies[i])


def _total_nm(sublayers: Sequence[Sublayer]) -> int:
    return sum(s.thickness_nm for s in sublayers)


def _copper_between(copper: Sequence[Any], a: int, b: int) -> tuple[Any, ...]:
    """The copper layers strictly between copper positions *a* and *b*."""
    top, bottom = (a, b) if a < b else (b, a)
    return tuple(x for x in copper if top < x.position < bottom)


def _copper_nm(layers: Sequence[Any]) -> int:
    return sum(x.thickness_nm for x in layers)


def _sum_thickness(plies, soldermasks, copper) -> int | None:
    parts: list[int | None] = [m.thickness_nm for m in soldermasks]
    parts += [c.thickness_nm for c in copper]
    parts += [s.thickness_nm for i in sorted(plies) for s in plies[i]]
    if any(p is None for p in parts):
        return None
    return sum(parts)


def _thickness(value: Any) -> int | None:
    """A positive whole number of nanometres, else None (not stated)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0 or value != int(value):
        return None
    return int(value)


def _epsilon(value: Any) -> float | None:
    """A relative permittivity of at least 1, else None (not stated)."""
    number = _finite(value)
    return number if number is not None and number >= 1 else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _nm_or_none(value: int) -> int | None:
    return int(value) if value else None


def _mm_text(nm: int | None) -> str:
    return f"{_num_text(nm / NM_PER_MM)} mm"


def _num_text(value: float) -> str:
    return f"{value:.4g}"
