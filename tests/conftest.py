"""Suite-wide guard: no log record may carry more of an API key than its public id.

Every test runs with logging captured at DEBUG for every logger (the plugin's,
its HTTP client's, kicad-python's), and fails at teardown if any record's message or
traceback holds ``rfc_`` followed by more than the 8 characters of a key's
public identifier (spec: "A failing call ... the log contains no part of the
key beyond its public identifier").
"""
from __future__ import annotations

import logging
import re

import pytest

from tests.support import TEST_KEYS

#: More than the 12-character public identifier ("rfc_" + 8).
LEAK = re.compile(r"rfc_[A-Za-z0-9_\-]{9,}")


def leaks(text: str) -> list:
    found = LEAK.findall(text or "")
    found += [k for k in TEST_KEYS if k[:13] in (text or "")]
    return found


@pytest.fixture(autouse=True)
def no_key_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    yield
    for record in caplog.records:
        text = record.getMessage()
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            text += record.exc_text
        assert not leaks(text), f"a log record carries an API key: {record.name}: {text!r}"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Point every user directory at the test's own tmp_path; no key in the environment."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("RFTOOLS_API_KEY", raising=False)
    yield


@pytest.fixture
def service():
    """rftools.io on 127.0.0.1 for the real client (tests.support.FakeService)."""
    from tests.support import FakeService

    fake = FakeService()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def plugin_log_in_tmp(tmp_path, monkeypatch):
    """configure_logging keeps <cache>/rftools-kicad/plugin.log; never the real one here."""
    monkeypatch.setenv("RFTOOLS_KICAD_LOG_DIR", str(tmp_path / "plugin-log"))
