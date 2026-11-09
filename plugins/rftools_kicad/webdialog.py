"""The plugin's window in rftools.io's design: a wx.html2 web view in KiCad's window.

kicad-plugin-site-design, spec ``kicad-plugin/presentation``. The page is
``webui.page``; its messages go to ``webui.reply``, which works through the
same ``DialogState`` as the native dialog. Where no suitable web view can
start, ``dialog.show`` opens the native dialog on the same state:

- no WebKit or Edge backend (Internet Explorer's cannot render the design);
- creating the view raises;
- the page has not reported ready within :data:`READY_TIMEOUT_S`.

Importing this module fails where ``wx.html2`` is missing (Linux without
python3-wxgtk-webview4.0), and ``main.presenter`` then takes the native dialog.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

import wx
import wx.html2 as html2

from rftools_kicad import webui
from rftools_kicad.dialog_logic import DialogState

READY_TIMEOUT_S = 5.0
HANDLER = "rftools"  # window.rftools.postMessage in the page (ui/bridge.js)
FALLBACK = 9001  # the modal result when the page never reported ready

log = logging.getLogger("rftools_kicad.webdialog")


def web_view_backend() -> tuple:
    """``(backend, reason)``: WebKit, else Edge, else ``None`` with the reason."""
    for name in ("WebViewBackendWebKit", "WebViewBackendEdge"):
        backend = getattr(html2, name, None)
        if backend and html2.WebView.IsBackendAvailable(backend):
            return backend, f"{name} is available"
    return None, ("no WebKit or Edge web view backend is available "
                  "(Internet Explorer's is never used)")


def theme() -> str:
    """KiCad's light or dark appearance, as the system reports it."""
    try:
        return "dark" if wx.SystemSettings.GetAppearance().IsDark() else "light"
    except AttributeError:  # a wx without GetAppearance
        return "light"


def present(model: Any, netclasses: list, services: Any) -> int:
    """main.present's contract: show the window; returns the exit code."""
    from rftools_kicad import dialog

    try:
        app = wx.App(False)
    except (Exception, SystemExit):  # wx exits when it cannot reach a display
        from rftools_kicad import report

        return report.present(model, netclasses, services)
    state = DialogState(model, netclasses, services)
    show(state)
    dialog._save_options(state, services)
    del app
    return 0


def show(
    state: DialogState,
    *,
    native: Callable[[DialogState], Any] | None = None,
) -> str:
    """The web window on *state*, or the native dialog; returns "web" or "native"."""
    backend, reason = web_view_backend()
    if backend is None:
        return _fallback(state, reason, native)
    try:
        window = WebDialog(None, state, backend)
    except Exception as exc:
        return _fallback(state, f"creating the web view failed ({type(exc).__name__}: {exc})",
                         native)
    try:
        window.ShowModal()
        ready = window.ready
    finally:
        window.Destroy()
    if not ready:
        return _fallback(state, f"the page did not report ready within {READY_TIMEOUT_S:g} s",
                         native)
    return "web"


def _fallback(state: DialogState, reason: str, native: Callable | None) -> str:
    log.warning("web view unavailable: %s; opening the native dialog", reason)
    if native is None:
        native = importlib.import_module("rftools_kicad.dialog").show
    native(state)
    return "native"


class WxUi:
    """webui.UiPort over wx: the system browser, the clipboard, closing, progress."""

    def __init__(self, window: Any) -> None:
        self.window = window
        self._progress = None

    def open_url(self, url: str) -> bool:
        from rftools_kicad.browser import open_url

        return bool(open_url(url, wx_module=wx))

    def copy(self, text: str) -> bool:
        from rftools_kicad.dialog import copy_text

        return copy_text(text)

    def close(self) -> None:
        self.window.EndModal(wx.ID_CLOSE)

    def progress(self, done: int, count: int, label: str) -> None:
        # The run is synchronous, so its progress is the native dialog's, as before.
        if self._progress is None:
            self._progress = wx.ProgressDialog(
                webui.TITLE, "Computing…", maximum=max(1, count), parent=self.window,
                style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE,
            )
        self._progress.Update(min(done, max(1, count)), label or " ")

    def progress_done(self) -> None:
        """After the run, finished or failed: the progress dialog never outlives it."""
        if self._progress is not None:
            self._progress.Destroy()
            self._progress = None


class WebDialog(wx.Dialog):
    """KiCad's window holding the page; every message goes to webui.reply."""

    def __init__(self, parent: Any, state: DialogState, backend: Any) -> None:
        super().__init__(parent, title=webui.TITLE,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self.session = webui.start(state)
        self.ready = False
        self.ui = WxUi(self)
        self.view = html2.WebView.New(self, backend=backend)
        if hasattr(self.view, "EnableContextMenu"):  # its Reload would blank the page
            self.view.EnableContextMenu(False)
        if not self.view.AddScriptMessageHandler(HANDLER):
            raise RuntimeError("the web view refused the script message handler")
        self.view.Bind(html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, self.on_message)
        self.view.Bind(html2.EVT_WEBVIEW_NAVIGATING, self.on_navigating)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.view, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.SetSize(self.FromDIP(wx.Size(1180, 760)))
        self.CentreOnScreen()
        self.SetEscapeId(wx.ID_CLOSE)
        # RunScript waits for the result in a nested event loop, which hangs a modal
        # dialog on macOS's WebKit; RunScriptAsync (wx 3.1.6+) answers by event.
        self.run_script = getattr(self.view, "RunScriptAsync", None) or self.view.RunScript
        self.view.SetPage(webui.page(self.session, theme=theme()), "")
        self._timer = wx.CallLater(int(READY_TIMEOUT_S * 1000), self.on_timeout)

    def on_navigating(self, event: Any) -> None:
        """The page never leaves itself: web and file URLs are refused, and an https
        link opens in the system browser. SetPage itself navigates to about:blank
        (WebKit does so twice) or a data: URL, which stay allowed."""
        url = event.GetURL() or ""
        if not url.startswith(("http://", "https://", "file:")):
            return
        event.Veto()
        if url.startswith("https://"):
            self.ui.open_url(url)

    def on_message(self, event: Any) -> None:
        answer = webui.reply(self.session, event.GetString(), self.ui)
        if answer.ready:
            self.ready = True
            self._timer.Stop()
            if self.session.state.has_key:  # as the native dialog does on opening
                self.session.state.refresh_usage()
                self.run_script(webui.update_script(self.session, ("header", "footer")))
        elif answer.close:
            self.EndModal(wx.ID_CLOSE)
        elif answer.script:
            self.run_script(answer.script)

    def on_timeout(self) -> None:
        if not self.ready and self.IsModal():
            self.EndModal(FALLBACK)
