"""The window chooses its web view or falls back to the native one (site-design 6.1).

wx may be absent where the suite runs, so these tests import webdialog over a
minimal fake wx: what the module needs at import, and the web view's backends.
"""
from __future__ import annotations

import importlib
import logging
import sys
import types

import pytest

from rftools_kicad.dialog_logic import DialogState
from rftools_kicad.stackup import layer_model
from tests.support import FakeServices, two_layer


def webdialog(monkeypatch, available=()):
    wx = types.ModuleType("wx")
    html2 = types.ModuleType("wx.html2")

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

    class WebView:
        @staticmethod
        def IsBackendAvailable(backend):
            return backend in available

    wx.Dialog, wx.html2 = Dialog, html2
    html2.WebView = WebView
    html2.WebViewBackendWebKit = b"wxWebViewWebKit"
    html2.WebViewBackendEdge = b"wxWebViewEdge"
    html2.WebViewBackendIE = b"wxWebViewIE"
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "wx.html2", html2)
    sys.modules.pop("rftools_kicad.webdialog", None)
    return importlib.import_module("rftools_kicad.webdialog"), html2


@pytest.fixture(autouse=True)
def forget_webdialog():
    """The module was imported over the fake wx: nothing after this test may see it."""
    yield
    sys.modules.pop("rftools_kicad.webdialog", None)
    import rftools_kicad

    if hasattr(rftools_kicad, "webdialog"):
        delattr(rftools_kicad, "webdialog")


def a_state(tmp_path) -> DialogState:
    return DialogState(layer_model(two_layer()), [], FakeServices(tmp_path))


def test_ie_only_falls_back_to_native(monkeypatch, tmp_path, caplog):
    W, html2 = webdialog(monkeypatch, available=(b"wxWebViewIE",))
    backend, reason = W.web_view_backend()
    assert backend is None and "Internet Explorer" in reason
    state, shown = a_state(tmp_path), []
    with caplog.at_level(logging.WARNING):
        assert W.show(state, native=shown.append) == "native"
    assert shown == [state]
    assert "web view unavailable" in caplog.text and "Internet Explorer" in caplog.text


def test_edge_or_webkit_is_chosen(monkeypatch):
    W, html2 = webdialog(monkeypatch, available=(b"wxWebViewIE", b"wxWebViewEdge"))
    assert W.web_view_backend()[0] == b"wxWebViewEdge"
    W, html2 = webdialog(monkeypatch, available=(b"wxWebViewWebKit",))
    assert W.web_view_backend()[0] == b"wxWebViewWebKit"


class FakeWindow:
    made: list = []

    def __init__(self, parent, state, backend, ready=False):
        self.state, self.backend, self.ready = state, backend, ready
        FakeWindow.made.append(self)

    def ShowModal(self):
        return 0

    def Destroy(self):
        pass


def test_no_ready_within_timeout_falls_back_with_same_state(monkeypatch, tmp_path, caplog):
    W, _ = webdialog(monkeypatch, available=(b"wxWebViewWebKit",))
    monkeypatch.setattr(W, "WebDialog", FakeWindow)
    state, shown = a_state(tmp_path), []
    with caplog.at_level(logging.WARNING):
        assert W.show(state, native=shown.append) == "native"
    assert shown == [state] and FakeWindow.made[-1].state is state
    assert f"within {W.READY_TIMEOUT_S:g} s" in caplog.text


def test_a_web_view_that_cannot_be_created_falls_back(monkeypatch, tmp_path):
    W, _ = webdialog(monkeypatch, available=(b"wxWebViewEdge",))

    def broken(*args, **kwargs):
        raise RuntimeError("WebView2 failed to start")

    monkeypatch.setattr(W, "WebDialog", broken)
    shown = []
    assert W.show(a_state(tmp_path), native=shown.append) == "native" and len(shown) == 1


def test_a_ready_page_stays_in_the_web_view(monkeypatch, tmp_path):
    W, _ = webdialog(monkeypatch, available=(b"wxWebViewWebKit",))
    monkeypatch.setattr(W, "WebDialog", lambda *a: FakeWindow(*a, ready=True))
    shown = []
    assert W.show(a_state(tmp_path), native=shown.append) == "web" and shown == []


def test_presenter_order(monkeypatch):
    from rftools_kicad import main as M

    real = importlib.import_module
    tried = []

    def import_module(name, *args):
        if name.startswith("rftools_kicad.") and name.split(".")[1] in (
                "webdialog", "dialog", "report"):
            tried.append(name)
            raise ImportError(name)
        return real(name, *args)

    monkeypatch.setattr(importlib, "import_module", import_module)
    assert M.presenter() is M.present
    assert tried == ["rftools_kicad.webdialog", "rftools_kicad.dialog", "rftools_kicad.report"]
