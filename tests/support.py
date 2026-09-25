"""Fixtures shared by the tests: stackups, kicad-python messages, a fake SDK client."""
from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "golden" / "kicad-golden.json"

#: Keys used in the suite. None may appear in a log record beyond its first 12
#: characters (conftest.py checks every record of every test).
KEY = "rfc_TESTkey0" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
OTHER_KEY = "rfc_OTHERkey" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0"
TEST_KEYS = (KEY, OTHER_KEY)


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def golden_case(case_id: str) -> dict:
    return next(c for c in golden()["cases"] if c["id"] == case_id)


# ── Stackups (neutral dicts), top to bottom ──────────────────────────────────


def _mask(name, thickness=10000, er=3.3):
    return {"name": name, "type": "soldermask", "thicknessNm": thickness, "epsilonR": er}


def _cu(name, thickness=35000, role=None):
    layer = {"name": name, "type": "copper", "thicknessNm": thickness}
    if role is not None:
        layer["role"] = role
    return layer


def _diel(name, *plies):
    return {
        "name": name,
        "type": "dielectric",
        "sublayers": [
            {"material": m, "thicknessNm": t, "epsilonR": er, "lossTangent": 0.02}
            for m, t, er in plies
        ],
    }


def two_layer() -> dict:
    """KiCad 10's default board: 1.51 mm FR4 at εr 4.5, 35 µm copper, 10 µm mask."""
    return {
        "boardThicknessNm": 1600000,
        "layers": [
            _mask("F.Mask"),
            _cu("F.Cu"),
            _diel("dielectric 1", ("FR4", 1510000, 4.5)),
            _cu("B.Cu"),
            _mask("B.Mask"),
        ],
    }


def four_layer(roles: bool = True) -> dict:
    """Signal / GND / PWR / signal, 1.6 mm. With ``roles`` the planes are typed power."""
    plane = "power" if roles else None
    signal = "signal" if roles else None
    return {
        "boardThicknessNm": 1600000,
        "layers": [
            _mask("F.Mask"),
            _cu("F.Cu", role=signal),
            _diel("dielectric 1", ("7628 prepreg", 200000, 4.4)),
            _cu("In1.Cu", role=plane),
            _diel("dielectric 2", ("FR4 core", 1040000, 4.6)),
            _cu("In2.Cu", role=plane),
            _diel("dielectric 3", ("7628 prepreg", 200000, 4.4)),
            _cu("B.Cu", role=signal),
            _mask("B.Mask"),
        ],
    }


def six_layer() -> dict:
    """Signal / GND / signal / signal / PWR / signal (dual stripline), 1.6 mm.

    F.Cu sits on a two-ply prepreg (1080 + 2116) over In1.Cu; In2.Cu is a
    stripline between In1.Cu (a core above) and In4.Cu (a prepreg and a core
    below, with In3.Cu's copper between them).
    """
    return {
        "boardThicknessNm": 1600000,
        "layers": [
            _mask("F.Mask"),
            _cu("F.Cu", role="signal"),
            _diel("dielectric 1", ("1080 prepreg", 75000, 4.0), ("2116 prepreg", 115000, 4.2)),
            _cu("In1.Cu", 17500, role="power"),
            _diel("dielectric 2", ("FR4 core", 415000, 4.6)),
            _cu("In2.Cu", 17500, role="signal"),
            _diel("dielectric 3", ("7628 prepreg", 230000, 4.4)),
            _cu("In3.Cu", 17500, role="signal"),
            _diel("dielectric 4", ("FR4 core", 415000, 4.6)),
            _cu("In4.Cu", 17500, role="power"),
            _diel("dielectric 5", ("2116 prepreg", 115000, 4.2), ("1080 prepreg", 75000, 4.0)),
            _cu("B.Cu", role="signal"),
            _mask("B.Mask"),
        ],
    }


def missing_er() -> dict:
    """The four-layer board without roles, its core's εr not stated."""
    stackup = four_layer(roles=False)
    core = stackup["layers"][4]
    assert core["name"] == "dielectric 2"
    core["sublayers"][0]["epsilonR"] = None
    return stackup


def with_role(stackup: dict, name: str, role: str) -> dict:
    stackup = copy.deepcopy(stackup)
    for layer in stackup["layers"]:
        if layer.get("name") == name:
            layer["role"] = role
    return stackup


# ── kicad-python messages ────────────────────────────────────────────────────


def kipy_stackup(stackup: dict, *, with_soldermask_details: bool = True):
    """The ``board_pb2.BoardStackup`` KiCad 10.0.6 would send for a neutral stackup.

    Silkscreen and paste entries with no thickness are added at both ends, as
    KiCad reports them; copper layers carry no role (KiCad exposes none).
    """
    from kipy.proto.board import board_pb2
    from kipy.proto.board.board_types_pb2 import BoardLayer

    message = board_pb2.BoardStackup()

    def add(kind, layer_name=None, user_name=""):
        layer = message.layers.add()
        layer.type = kind
        layer.layer = BoardLayer.Value(layer_name) if layer_name else BoardLayer.BL_UNDEFINED
        layer.user_name = user_name
        return layer

    add(board_pb2.BSLT_SILKSCREEN, "BL_F_SilkS", "F.Silkscreen")
    add(board_pb2.BSLT_SOLDERPASTE, "BL_F_Paste", "F.Paste")
    for entry in stackup["layers"]:
        if entry["type"] == "soldermask":
            side = "F" if entry["name"].startswith("F") else "B"
            layer = add(board_pb2.BSLT_SOLDERMASK, f"BL_{side}_Mask", entry["name"])
            layer.thickness.value_nm = entry["thicknessNm"] or 0
            if with_soldermask_details:
                layer.soldermask.epsilon_r = entry["epsilonR"] or 0
                layer.soldermask.thickness.value_nm = entry["thicknessNm"] or 0
        elif entry["type"] == "copper":
            layer = add(
                board_pb2.BSLT_COPPER, "BL_" + entry["name"].replace(".", "_"), entry["name"]
            )
            layer.enabled = True
            layer.material_name = "copper"
            layer.thickness.value_nm = entry["thicknessNm"] or 0
        else:
            layer = add(board_pb2.BSLT_DIELECTRIC)
            layer.dielectric.type = board_pb2.BSDT_PREPREG
            total = 0
            for ply in entry["sublayers"]:
                p = layer.dielectric.layer.add()
                p.material_name = ply["material"]
                p.thickness.value_nm = ply["thicknessNm"] or 0
                p.epsilon_r = ply["epsilonR"] or 0
                p.loss_tangent = ply["lossTangent"]
                total += ply["thicknessNm"] or 0
            layer.thickness.value_nm = total
    add(board_pb2.BSLT_SOLDERPASTE, "BL_B_Paste", "B.Paste")
    add(board_pb2.BSLT_SILKSCREEN, "BL_B_SilkS", "B.Silkscreen")
    return message


def kipy_netclass(
    name: str,
    *,
    track: int | None = 200000,
    diff_width: int | None = None,
    diff_gap: int | None = None,
    clearance: int | None = 200000,
    via_diameter: int | None = 600000,
    via_drill: int | None = 300000,
):
    """A ``kipy.project_types.NetClass`` as KiCad 10.0 returns it (NCT_IMPLICIT)."""
    from kipy.project_types import NetClass
    from kipy.proto.common.types import project_settings_pb2

    message = project_settings_pb2.NetClass()
    message.name = name
    message.type = project_settings_pb2.NetClassType.NCT_IMPLICIT
    message.constituents.append(name)
    board = message.board
    if track is not None:
        board.track_width.value_nm = track
    if diff_width is not None:
        board.diff_pair_track_width.value_nm = diff_width
    if diff_gap is not None:
        board.diff_pair_gap.value_nm = diff_gap
    if clearance is not None:
        board.clearance.value_nm = clearance
    if via_diameter is not None:
        board.via_stack.copper_layers.add().size.x_nm = via_diameter
    if via_drill is not None:
        board.via_stack.drill.diameter.x_nm = via_drill
    return NetClass(message)


# ── A fake SDK client ────────────────────────────────────────────────────────

PROVENANCE = {
    "method": "calculator",
    "version": "api@0123456789ab",
    "formulaRef": "Hammerstad & Jensen (1980)",
    "assumptions": [],
    "validRange": {"status": "inside", "bounds": {}, "outside": []},
    "computedAt": "2026-09-25T12:00:00.000Z",
    "inputs": {},
    "seed": None,
    "elapsedSeconds": 0.001,
}

USAGE = {
    "tier": "free",
    "period": "2026-09",
    "resetAt": "2026-10-01T00:00:00Z",
    "allowance": 50,
    "used": 10,
    "remaining": 40,
}


def calc_response(slug: str, inputs: dict, values: dict) -> dict:
    provenance = dict(PROVENANCE, inputs=dict(inputs))
    return {"slug": slug, "values": values, "warnings": [], "errors": [], "provenance": provenance}


def solve_response(
    slug, inputs, solve_for, output, target, value, unrounded=None, reached=True,
    achieved=None, grid=0.001,
) -> dict:
    forward_inputs = dict(inputs, **{solve_for: value})
    return {
        "slug": slug,
        "solveFor": solve_for,
        "target": {"output": output, "value": target},
        "grid": grid,
        "value": value,
        "unrounded": value if unrounded is None else unrounded,
        "reached": reached,
        "evaluations": 23,
        "warnings": [] if reached else ["The target was not reached."],
        "result": calc_response(
            slug, forward_inputs, {output: target if achieved is None else achieved}
        ),
    }


class FakeClient:
    """Records what it is asked, answers from callables, raises what it is told to.

    ``calls`` holds ``(method, body)`` with the body as the service would
    receive it, so a test can assert exactly what would be sent.
    """

    def __init__(self, *, calculate=None, solve=None, usage=None, has_solve=True):
        self.calls: list = []
        self.errors: list = []  # raised, in order, by the next metered calls
        self.usage_error = None
        self._calculate = calculate or self._default_calculate
        self._solve = solve or self._default_solve
        self._usage = dict(USAGE) if usage is None else usage
        if not has_solve:
            self.solve = None  # an SDK without Client.solve (rftools-io 0.3)

    @property
    def metered(self) -> list:
        return [c for c in self.calls if c[0] != "usage"]

    def usage(self):
        self.calls.append(("usage", {}))
        if self.usage_error is not None:
            raise self.usage_error
        return self._usage

    def calculate(self, slug, inputs):
        self.calls.append(("calculate", {"slug": slug, "inputs": dict(inputs)}))
        if self.errors:
            raise self.errors.pop(0)
        return self._calculate(slug, inputs)

    def solve(self, slug, inputs, solve_for, target_output, target_value, grid=None, range=None):  # noqa: A002
        body = {
            "slug": slug,
            "inputs": dict(inputs),
            "solveFor": solve_for,
            "target": {"output": target_output, "value": target_value},
        }
        if grid is not None:
            body["grid"] = grid
        if range is not None:
            body["range"] = list(range)
        self.calls.append(("solve", body))
        if self.errors:
            raise self.errors.pop(0)
        return self._solve(slug, inputs, solve_for, target_output, target_value, grid, range)

    @staticmethod
    def _default_calculate(slug, inputs):
        from rftools_kicad.mapping import PRIMARY_OUTPUT

        return calc_response(slug, inputs, {PRIMARY_OUTPUT[slug]: 50.0})

    @staticmethod
    def _default_solve(slug, inputs, solve_for, output, target, grid, range_):
        return solve_response(slug, inputs, solve_for, output, target, value=0.321, grid=grid)


# ── SDK exceptions ───────────────────────────────────────────────────────────


def sdk_available() -> bool:
    try:
        import rftools.exceptions  # noqa: F401
    except Exception:
        return False
    return True


def sdk_error(kind: str, **fields):
    """The exception the SDK raises for *kind*, built as the SDK builds it."""
    from rftools import exceptions as e

    if kind == "quota":
        error = e.QuotaError(
            "Quota exceeded.", retry_after=fields.get("retry_after", 3600),
            reason="allowance", limit=5, used=5,
            reset_at=fields.get("reset_at", "2026-10-01T00:00:00Z"),
            upgrade_url=fields.get("upgrade_url", "https://rftools.io/pricing"),
            overage_url=fields.get("overage_url"),
        )
        error.status_code = 402
    elif kind == "auth":
        error = e.AuthError("Authentication failed: invalid", key_url=fields.get("key_url"))
        error.status_code = fields.get("status", 401)
    elif kind == "rate":
        error = e.RateLimitError(
            "Rate limit exceeded", retry_after=fields.get("retry_after", 2), limit=30,
            window_seconds=60,
        )
        error.status_code = 429
    elif kind == "offline":
        error = e.APIError("Network error: [Errno 8] nodename nor servname provided", 0)
    elif kind == "server":
        error = e.APIError("Server error after 3 retries: 503", 503)
    elif kind == "invalid":
        error = e.ValidationError(
            "Input validation failed",
            detail=[{"param": "traceWidth", "value": -1, "reason": "must be positive"}],
        )
        error.status_code = 400
    elif kind == "not_found":
        error = e.NotFound("Calculator not found")
        error.status_code = 404
    else:
        raise ValueError(kind)
    return error


# ── A fake KiCad project for net-class writes ────────────────────────────────


def netclass_proto(name: str, **kwargs):
    """The ``project_settings_pb2.NetClass`` KiCad 10.0 reports (NCT_IMPLICIT)."""
    return kipy_netclass(name, **kwargs).proto


#: KiCad 10.0.6, whose API corrupts a net class it is sent (spike, 2026-09-25).
BROKEN_KICAD = (10, 0, 6)
#: The first release after it, which the tests take to carry commit d622c37a.
FIXED_KICAD = (10, 0, 7)

#: Nets per class in the fakes (the spike's 100ohm class had 26).
NETS_PER_CLASS = 26


class FakeKiCad:
    """KiCad for the net-class writer: its version, and ``SetNetClasses`` as it answers it.

    It is both the ``KiCad`` object (``get_version()``) and the project's
    command channel (``send()``). ``version`` picks the behaviour, from
    KiCad's source (``NETCLASS::Serialize``/``Deserialize`` and
    ``handleSetNetClasses``, before and after commit d622c37a):

    * 10.0.6 and earlier: a class reads back NCT_IMPLICIT while it has
      constituents and NCT_EXPLICIT without. An implicit class sent is dropped
      and the command still answers success; an explicit one is taken whole
      and left with **no** constituents, so its nets are lost (spike).
    * later: a class with at most one constituent reads back NCT_EXPLICIT. An
      implicit class sent is refused (AS_BAD_REQUEST) after the classes before
      it were taken; an explicit one is taken whole and keeps itself as its one
      constituent.

    ``MMM_MERGE`` replaces a named class whole. ``nets`` is ``{class: number
    of nets}`` for :class:`FakeBoard` (default :data:`NETS_PER_CLASS` each).
    ``ignore`` drops every class (a KiCad that answers success and applies
    nothing); ``refuse`` is raised instead of answering; ``version=None``
    makes ``get_version()`` fail.
    """

    def __init__(self, classes, *, version=FIXED_KICAD, nets=None, ignore=False, refuse=None,
                 on_send=None):
        from kipy.proto.common.types import project_settings_pb2

        self.version = version
        self.classes = {}
        for proto in classes:
            copy = project_settings_pb2.NetClass()
            copy.CopyFrom(proto)
            copy.type = self._reported_type(copy)
            self.classes[copy.name] = copy
        self.nets = dict(nets) if nets is not None else {n: NETS_PER_CLASS for n in self.classes}
        self.sent = []  # every command received, as sent
        self.ignore = ignore
        self.refuse = refuse
        self.on_send = on_send

    @property
    def broken(self) -> bool:
        return self.version is not None and tuple(self.version) <= BROKEN_KICAD

    def get_version(self):
        from kipy.errors import ApiError
        from kipy.kicad import KiCadVersion

        if self.version is None:
            raise ApiError("GetVersion is not answered")
        major, minor, patch = self.version
        return KiCadVersion(major, minor, patch, f"{major}.{minor}.{patch}")

    def _reported_type(self, proto):
        from kipy.proto.common.types import project_settings_pb2

        types = project_settings_pb2.NetClassType
        if self.broken:
            return types.NCT_IMPLICIT if proto.constituents else types.NCT_EXPLICIT
        return types.NCT_EXPLICIT if len(proto.constituents) <= 1 else types.NCT_IMPLICIT

    def send(self, command, response_type):
        from kipy.errors import ApiError
        from kipy.proto.common.commands import project_commands_pb2
        from kipy.proto.common.types import MapMergeMode, project_settings_pb2

        copy = type(command)()
        copy.CopyFrom(command)
        self.sent.append(copy)
        if self.on_send is not None:
            self.on_send(copy)
        if self.refuse is not None:
            raise self.refuse
        assert isinstance(command, project_commands_pb2.SetNetClasses)
        assert command.merge_mode == MapMergeMode.MMM_MERGE
        for sent in command.net_classes:
            if self.ignore:
                continue
            if sent.type == project_settings_pb2.NetClassType.NCT_IMPLICIT:
                if self.broken:
                    continue  # dropped, and the command answers success
                raise ApiError(f"could not unpack netclass '{sent.name}'")
            stored = project_settings_pb2.NetClass()
            stored.CopyFrom(sent)
            del stored.constituents[:]
            if not self.broken:
                stored.constituents.append(sent.name)
            stored.type = self._reported_type(stored)
            self.classes[sent.name] = stored
        return response_type()

    def serialized(self) -> dict:
        return {n: p.SerializeToString(deterministic=True) for n, p in self.classes.items()}


class FakeBoard:
    """kicad-python's ``Board.get_nets(netclass_filter=...)`` over a :class:`FakeKiCad`.

    As KiCad's ``GetNets`` does, a net is in a class when its class lists that
    class among its constituents: a class left with none holds no nets.
    ``refuse`` is raised instead of answering.
    """

    def __init__(self, kicad: FakeKiCad, refuse=None):
        self.kicad = kicad
        self.refuse = refuse
        self.calls = []

    def get_nets(self, netclass_filter=None):
        self.calls.append(netclass_filter)
        if self.refuse is not None:
            raise self.refuse
        proto = self.kicad.classes.get(netclass_filter)
        if proto is None or netclass_filter not in proto.constituents:
            return []
        return [object() for _ in range(self.kicad.nets.get(netclass_filter, 0))]


class FakeProject:
    """``kipy.project.Project`` over a :class:`FakeKiCad` (kicad-python 0.8: no set_net_classes)."""

    def __init__(self, kicad: FakeKiCad, path: str = "/work/rf-board", name: str = "rf-board"):
        self._kicad = kicad
        self.path = path
        self.name = name

    def get_net_classes(self):
        from kipy.project_types import NetClass
        from kipy.proto.common.types import project_settings_pb2

        out = []
        for proto in self._kicad.classes.values():
            copy = project_settings_pb2.NetClass()
            copy.CopyFrom(proto)
            out.append(NetClass(copy))
        return out


class FakeProject09(FakeProject):
    """kicad-python 0.9's ``Project.set_net_classes`` over the same channel."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_calls = []

    def set_net_classes(self, net_classes, merge_mode=None):
        from google.protobuf.empty_pb2 import Empty
        from kipy.proto.common.commands import project_commands_pb2

        self.set_calls.append((list(net_classes), merge_mode))
        command = project_commands_pb2.SetNetClasses()
        command.merge_mode = merge_mode
        command.net_classes.extend([nc.proto for nc in net_classes])
        self._kicad.send(command, Empty)


def sample_classes():
    """Four classes as a real project reports them, with fields the plugin must not touch."""
    default = netclass_proto(
        "Default", track=150000, diff_width=178000, diff_gap=250000, clearance=130000,
        via_diameter=450000, via_drill=300000,
    )
    default.priority = 2147483647
    default.board.diff_pair_via_gap.value_nm = 250000
    default.schematic.wire_width.value_nm = 1524
    default.schematic.bus_width.value_nm = 3048
    high = netclass_proto(
        "HighSpeed", track=200000, diff_width=130000, diff_gap=250000, clearance=200000,
    )
    high.priority = 0
    high.board.color.r = 1.0
    high.board.color.b = 1.0
    high.board.color.a = 1.0
    high.schematic.wire_width.value_nm = 1524
    usb = netclass_proto(
        "USB", track=147000, diff_width=140000, diff_gap=154000, clearance=200000,
    )
    usb.priority = 1
    power = netclass_proto("Power", track=500000, clearance=250000)
    power.priority = 2
    return [default, high, usb, power]


# ── The services main.py hands the presentation layer ────────────────────────


class FakeServices:
    """``main.Services`` over a :class:`FakeClient`, with every file under *tmp_path*."""

    def __init__(self, tmp_path, *, client=None, key=KEY, project=None, grid=0.001,
                 options=None, kicad=None, board=None):
        from rftools_kicad.cache import ResultCache
        from rftools_kicad.mapping import Options
        from rftools_kicad.settings import KEY_URL, SOURCE_SETTINGS, Settings

        self.version = "0.1.0"
        self.settings = Settings(
            api_key=key, key_source=SOURCE_SETTINGS if key else None, options={},
            path=tmp_path / "config" / "rftools-kicad" / "settings.json",
        )
        self.options = options or Options()
        self.grid = grid
        self.cache = ResultCache(tmp_path / "cache" / "results.json")
        self.key_url = KEY_URL
        self.history_path = tmp_path / "config" / "rftools-kicad" / "history.json"
        self.client = client or FakeClient()
        self.project = project
        # A FakeProject's FakeKiCad is also the KiCad object and backs the board.
        channel = getattr(project, "_kicad", None)
        self.kicad = kicad if kicad is not None else channel
        if board is None and isinstance(channel, FakeKiCad):
            board = FakeBoard(channel)
        self.board = board

    @property
    def has_key(self):
        return bool(self.settings.api_key)

    def api(self):
        from rftools_kicad.api import Api

        if not self.settings.api_key:
            raise RuntimeError("No API key is set. Get a free key at " + self.key_url)
        return Api(self.client, self.cache, key_id=self.settings.key_id, sleep=lambda s: None)

    def save_key(self, key):
        from rftools_kicad.settings import load_settings, save_key

        usage = save_key(key, lambda k: self.client, path=self.settings.path)
        self.settings = load_settings(self.settings.path, environ={})
        return usage
