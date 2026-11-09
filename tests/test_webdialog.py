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


def built_webdialog(monkeypatch, *, handler_ok=True):
    """webdialog over a fake wx whose Dialog records Destroy and whose WebView.New
    gives a view that accepts (or refuses) the script message handler."""
    W, html2 = webdialog(monkeypatch, available=(b"wxWebViewWebKit",))
    wx = sys.modules["wx"]
    wx.DEFAULT_DIALOG_STYLE = wx.RESIZE_BORDER = wx.MAXIMIZE_BOX = 0
    destroyed = []

    class View:
        def EnableContextMenu(self, on):
            pass

        def AddScriptMessageHandler(self, name):
            return handler_ok

    def new(parent, backend=None):
        return View()

    html2.WebView.New = staticmethod(new)
    monkeypatch.setattr(wx.Dialog, "Destroy", lambda self: destroyed.append(self), raising=False)
    return W, destroyed


def test_a_half_built_window_is_destroyed_before_the_fallback(monkeypatch, tmp_path):
    W, destroyed = built_webdialog(monkeypatch, handler_ok=False)
    with pytest.raises(RuntimeError, match="script message handler"):
        W.WebDialog(None, a_state(tmp_path), b"wxWebViewWebKit")
    assert len(destroyed) == 1


class Navigation:
    def __init__(self, url):
        self.url, self.vetoed = url, False

    def GetURL(self):
        return self.url

    def Veto(self):
        self.vetoed = True


def bare_window(W, ready):
    window = W.WebDialog.__new__(W.WebDialog)
    window.ready, opened = ready, []
    window.ui = types.SimpleNamespace(open_url=opened.append)
    return window, opened


def test_once_ready_the_page_never_navigates(monkeypatch):
    """A reload (F5) or Back would otherwise replace the page with a blank one."""
    W, _ = webdialog(monkeypatch, available=(b"wxWebViewEdge",))
    window, opened = bare_window(W, ready=True)
    for url in ("about:blank", "data:text/html,", "https://rftools.io/pricing/", ""):
        event = Navigation(url)
        window.on_navigating(event)
        assert event.vetoed, url
    assert opened == ["https://rftools.io/pricing/"]


def test_before_ready_set_page_may_navigate_to_about_blank(monkeypatch):
    W, _ = webdialog(monkeypatch, available=(b"wxWebViewWebKit",))
    window, opened = bare_window(W, ready=False)
    for url in ("about:blank", "data:text/html,"):
        event = Navigation(url)
        window.on_navigating(event)
        assert not event.vetoed, url
    event = Navigation("file:///etc/passwd")
    window.on_navigating(event)
    assert event.vetoed and opened == []


def appearance(monkeypatch, W, *, dark):
    wx = sys.modules["wx"]
    look = types.SimpleNamespace(IsDark=lambda: dark)
    wx.SystemSettings = types.SimpleNamespace(GetAppearance=lambda: look)


def fake_winreg(monkeypatch, value):
    winreg = types.ModuleType("winreg")
    winreg.HKEY_CURRENT_USER = "HKCU"

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path):
        if value is None:
            raise OSError("no such key")
        assert root == "HKCU" and path.endswith(r"Themes\Personalize")
        return Key()

    winreg.OpenKey = open_key
    winreg.QueryValueEx = lambda key, name: (value, 4)
    monkeypatch.setitem(sys.modules, "winreg", winreg)


def test_windows_follows_the_windows_app_theme(monkeypatch):
    """The plugin runs in its own process, where wx's IsDark reports Win32's light
    colours even on a dark Windows; the system's setting is AppsUseLightTheme."""
    W, _ = webdialog(monkeypatch)
    appearance(monkeypatch, W, dark=False)
    monkeypatch.setattr(W.sys, "platform", "win32")
    fake_winreg(monkeypatch, 0)
    assert W.theme() == "dark"
    fake_winreg(monkeypatch, 1)
    assert W.theme() == "light"
    fake_winreg(monkeypatch, None)  # no setting (older Windows): wx decides
    assert W.theme() == "light"


def test_other_systems_follow_wx(monkeypatch):
    W, _ = webdialog(monkeypatch)
    monkeypatch.setattr(W.sys, "platform", "darwin")
    fake_winreg(monkeypatch, 0)  # never read off Windows
    appearance(monkeypatch, W, dark=True)
    assert W.theme() == "dark"
    appearance(monkeypatch, W, dark=False)
    assert W.theme() == "light"
