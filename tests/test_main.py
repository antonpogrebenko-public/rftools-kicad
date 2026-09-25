"""The entrypoint: Python version check first, then KiCad, then the hand-off."""
from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys

import pytest

from rftools_kicad import main as M
from rftools_kicad.mapping import NetClassValues
from rftools_kicad.stackup import LayerModel
from tests.support import ROOT, kipy_netclass, kipy_stackup, two_layer

MAIN = ROOT / "plugins" / "rftools_kicad" / "main.py"


@pytest.fixture(autouse=True)
def drop_plugin_log_handlers():
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_rftools", False)]


# ── Python 3.9 can run the check ─────────────────────────────────────────────


def test_main_parses_with_a_python_3_9_grammar():
    source = MAIN.read_text(encoding="utf-8")
    ast.parse(source, filename=str(MAIN), feature_version=(3, 9))


def test_main_imports_only_the_standard_library_at_module_level():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {"__future__", "html", "os", "sys", "tempfile", "typing"}, names


def test_the_message_for_an_old_python_says_what_to_do():
    message = M.python_too_old_message((3, 9, 13, "final", 0), "/KiCad/python3.9")
    assert "needs Python 3.12 or later" in message
    assert "Python 3.9.13 (/KiCad/python3.9)" in message
    assert "Preferences › Plugins" in message and "Python Interpreter" in message
    assert "restart KiCad" in message
    # KiCad 10.0.6 keeps the old environment after the interpreter changes (spike).
    assert "does not rebuild the plugin's environment on its own" in message
    assert ("right-click the plugin in Preferences › Plugins › Action Plugins and choose "
            "Recreate Plugin Environment") in message
    assert "io.rftools.kicad" in message and "python-environments" in message
    assert "macOS (3.9) and Windows (3.11)" in message and "python.org" in message
    assert "On Linux, KiCad uses the system python3" in message
    assert "python3-venv" in message and "python3-wxgtk4.0" in message
    assert M.python_too_old_message((3, 12, 0)) is None
    assert M.python_too_old_message((3, 13, 5)) is None


def test_mains_copies_of_shared_wording_match_the_package():
    # main.py repeats these because it imports nothing of the plugin's before the check.
    import rftools_kicad
    from rftools_kicad import api as A

    assert M.PLUGIN_ID == rftools_kicad.IDENTIFIER
    assert M.RECREATE_ENVIRONMENT == A.RECREATE_ENVIRONMENT


def test_an_old_python_is_told_before_anything_is_imported(monkeypatch):
    shown = []
    monkeypatch.setattr(M, "notify", lambda text, **kw: shown.append((text, kw)) or "wx")
    monkeypatch.setattr(M, "run", lambda *a, **k: pytest.fail("run() on an old Python"))
    assert M.main(version_info=(3, 9, 13)) == 2
    [(text, kw)] = shown
    assert "3.9.13" in text and kw == {"browser_fallback": True}


def test_without_wx_the_message_opens_as_a_page(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "wx", None)  # import wx fails
    monkeypatch.setattr(M.tempfile, "gettempdir", lambda: str(tmp_path))
    opened = []
    text = M.python_too_old_message((3, 9, 13), "/K/<py>")
    how = M.notify(text, browser_fallback=True, opener=opened.append)
    assert how == "browser"
    [uri] = opened
    assert uri.startswith("file://") and uri.endswith(".html")
    page = next(tmp_path.glob("rftools-kicad-*.html")).read_text(encoding="utf-8")
    assert "needs Python 3.12 or later" in page and "/K/&lt;py&gt;" in page
    assert "needs Python 3.12 or later" in capsys.readouterr().err


def test_a_stream_that_cannot_encode_the_text_gets_replacements():
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    M.say("εr 4.5 › done", stream)
    stream.flush()
    assert stream.buffer.getvalue().decode("cp1252") == "?r 4.5 › done\n"


def test_without_wx_and_without_the_page_it_is_the_console(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "wx", None)
    assert M.notify("plain") == "console"
    assert "plain" in capsys.readouterr().err


# ── KiCad ────────────────────────────────────────────────────────────────────


class FakeProject:
    def __init__(self, netclasses):
        self._netclasses = netclasses

    def get_net_classes(self):
        return self._netclasses


class FakeBoard:
    def __init__(self, stackup, netclasses):
        from kipy.board import BoardStackup

        self._stackup = BoardStackup(stackup)
        self._project = FakeProject(netclasses)

    def get_stackup(self):
        return self._stackup

    def get_project(self):
        return self._project


class FakeKiCad:
    def __init__(self, board):
        self._board = board

    def get_board(self):
        return self._board


def test_run_reads_the_board_and_hands_plain_data_to_the_presenter(tmp_path):
    netclasses = [
        kipy_netclass("Default"),
        kipy_netclass("USB", track=180000, diff_width=180000, diff_gap=150000),
    ]
    board = FakeBoard(kipy_stackup(two_layer()), netclasses)
    received = {}

    def presenter(model, classes, services):
        received.update(model=model, classes=classes, services=services)
        return 7

    code = M.run(kicad_factory=lambda: FakeKiCad(board), present_fn=presenter)
    assert code == 7
    model = received["model"]
    assert isinstance(model, LayerModel)
    assert [s.layer for s in model.structures] == ["F.Cu", "B.Cu"]
    assert received["classes"][1] == NetClassValues(
        "USB", 180000, 180000, 150000, 200000, 600000, 300000,
    )
    services = received["services"]
    assert services.board is board and services.version == "0.1.0"
    assert services.key_url.endswith("client=kicad-plugin")
    assert services.cache.path.name == "results.json"
    assert not services.has_key
    with pytest.raises(M.PluginStartError, match="Get a free key"):
        services.api()


def test_the_stub_presenter_prints_the_model_and_calls_nothing(capsys):
    board = FakeBoard(kipy_stackup(two_layer()), [kipy_netclass("Default")])
    assert M.run(kicad_factory=lambda: FakeKiCad(board), present_fn=M.present) == 0
    out = capsys.readouterr().out
    assert "F.Cu: microstrip over B.Cu, 1.51 mm of dielectric, εr 4.5." in out
    assert "Net class Default: track 0.2 mm, via 0.6 / 0.3 mm, clearance 0.2 mm" in out
    assert "no API call was made" in out


def test_the_presenter_is_the_dialog_else_the_report_else_the_stub(monkeypatch):
    import importlib

    real = importlib.import_module
    loadable = {"rftools_kicad.dialog", "rftools_kicad.report"}

    def import_module(name, *args):
        if name in ("rftools_kicad.dialog", "rftools_kicad.report") and name not in loadable:
            raise ImportError(f"no {name}")
        if name == "rftools_kicad.dialog":
            class Dialog:  # wx may be absent where the suite runs
                @staticmethod
                def present(*args):
                    return 0
            return Dialog
        return real(name, *args)

    monkeypatch.setattr(importlib, "import_module", import_module)
    assert M.presenter().__qualname__.endswith("Dialog.present")
    loadable.discard("rftools_kicad.dialog")  # import wx failed
    from rftools_kicad import report

    assert M.presenter() is report.present
    loadable.clear()
    assert M.presenter() is M.present


def test_kicad_without_the_api_enabled_is_a_readable_message(monkeypatch, capsys):
    from kipy.errors import ConnectionError as KiCadConnectionError

    monkeypatch.setitem(sys.modules, "wx", None)

    def refuse():
        raise KiCadConnectionError("Failed to connect to KiCad: Connection refused")

    assert M.run(kicad_factory=refuse) == 1
    assert "Enable the KiCad API in Preferences › Plugins" in capsys.readouterr().err


def test_no_open_board_is_a_readable_message(monkeypatch, capsys):
    from kipy.errors import ApiError

    monkeypatch.setitem(sys.modules, "wx", None)

    class NoBoard:
        def get_board(self):
            raise ApiError("Expected to be able to retrieve at least one board")

    assert M.run(kicad_factory=NoBoard) == 1
    assert "Open the board in the PCB editor" in capsys.readouterr().err


def test_a_board_kicad_cannot_describe_is_a_readable_message(monkeypatch, capsys):
    from kipy.errors import ApiError

    monkeypatch.setitem(sys.modules, "wx", None)

    class Unreadable(FakeBoard):
        def get_stackup(self):
            raise ApiError("stackup unavailable")

    board = Unreadable(kipy_stackup(two_layer()), [])
    assert M.run(kicad_factory=lambda: FakeKiCad(board)) == 1
    assert "did not return the board's stackup" in capsys.readouterr().err


def test_the_grid_comes_from_the_settings(tmp_path):
    from rftools_kicad.settings import Settings

    services = M.Services(Settings(options={"grid": 0.005}), None, None)
    assert services.grid == 0.005
    assert M.Services(Settings(options={"grid": -1}), None, None).grid == 0.001


def test_the_entrypoint_as_kicad_runs_it_fails_gracefully_without_kicad(tmp_path):
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "KICAD_API_SOCKET": f"ipc://{tmp_path}/no-kicad.sock",
    })
    env.pop("RFTOOLS_API_KEY", None)
    # As on a Linux KiCad without python3-wxgtk4.0: wx does not import, so the
    # message goes to stderr instead of a modal box nobody would close.
    no_wx = tmp_path / "no-wx"
    (no_wx / "wx").mkdir(parents=True)
    (no_wx / "wx" / "__init__.py").write_text("raise ImportError('no wx here')\n")
    env["PYTHONPATH"] = str(no_wx)
    result = subprocess.run(
        [sys.executable, str(MAIN)], cwd=str(MAIN.parent.parent), env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert "Enable the KiCad API" in result.stderr
    assert "Traceback" not in result.stderr
