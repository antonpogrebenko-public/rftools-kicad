"""Opening a link from KiCad's plugin process (browser.py)."""
from __future__ import annotations

from types import SimpleNamespace

from rftools_kicad import browser

URL = "https://rftools.io/dashboard/?newKey=1&client=kicad-plugin"


class FakeWx:
    def __init__(self, result=True, error=None):
        self.result, self.error, self.calls = result, error, []

    def LaunchDefaultBrowser(self, url):  # noqa: N802 — wx's name
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.result


def never(*_a, **_k):
    raise AssertionError("not reached")


def test_wx_first_and_nothing_else_when_it_opens():
    wx = FakeWx(True)
    assert browser.open_url(URL, wx_module=wx, platform="darwin", run=never, fallback=never)
    assert wx.calls == [URL]


def test_macos_falls_back_to_open_when_wx_cannot():
    ran = []

    def run(cmd, **_k):
        ran.append(cmd)
        return SimpleNamespace(returncode=0)

    assert browser.open_url(URL, wx_module=FakeWx(False), platform="darwin", run=run,
                            fallback=never)
    assert ran == [["open", URL]]


def test_linux_uses_xdg_open_and_windows_startfile():
    ran = []
    def run(cmd, **_k):
        ran.append(cmd)
        return SimpleNamespace(returncode=0)

    assert browser.open_url(URL, platform="linux", run=run, fallback=never)
    assert ran == [["xdg-open", URL]]
    started = []
    assert browser.open_url(URL, platform="win32", startfile=started.append, run=never,
                            fallback=never)
    assert started == [URL]


def test_webbrowser_is_last_and_false_when_everything_fails():
    def failing_run(cmd, **_k):
        return SimpleNamespace(returncode=1)

    tried = []
    opened = browser.open_url(URL, wx_module=FakeWx(error=RuntimeError("no display")),
                              platform="darwin", run=failing_run,
                              fallback=lambda u: tried.append(u) or False)
    assert opened is False
    assert tried == [URL]


def test_an_opener_that_raises_is_not_fatal():
    def boom(*_a, **_k):
        raise OSError("no such command")

    assert browser.open_url(URL, platform="linux", run=boom, fallback=lambda u: True)


def test_the_plugin_log_file_is_written_and_redacted(tmp_path, monkeypatch):
    import logging
    from types import SimpleNamespace as NS

    from rftools_kicad import main as M

    key = "rfc_AbCdEfGh_" + "s" * 32
    monkeypatch.setenv("RFTOOLS_KICAD_LOG_DIR", str(tmp_path))
    M.configure_logging(NS(api_key=key))
    logging.getLogger("rftools_kicad.test").warning("calling with %s", key)
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "plugin.log").read_text(encoding="utf-8")
    assert "calling with rfc_AbCdEfGh" in text and key not in text
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_rftools", False)]
