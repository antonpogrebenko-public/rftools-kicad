"""The key and settings: where they live, file mode, override, validation, redaction."""
from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from rftools_kicad import api as A
from rftools_kicad import settings as S
from tests.support import KEY, OTHER_KEY, USAGE, FakeClient, api_error

posix_only = pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")


# ── Directories ──────────────────────────────────────────────────────────────


def test_config_and_cache_dirs_per_platform(tmp_path):
    home = tmp_path / "u"
    env = {}
    assert S.config_dir("darwin", env, home) == home / "Library/Application Support/rftools-kicad"
    assert S.cache_dir("darwin", env, home) == home / "Library/Caches/rftools-kicad"
    assert S.config_dir("linux", env, home) == home / ".config/rftools-kicad"
    assert S.cache_dir("linux", env, home) == home / ".cache/rftools-kicad"
    xdg = {"XDG_CONFIG_HOME": "/xdg/config", "XDG_CACHE_HOME": "/xdg/cache"}
    assert S.config_dir("linux", xdg, home) == Path("/xdg/config/rftools-kicad")
    assert S.cache_dir("linux", xdg, home) == Path("/xdg/cache/rftools-kicad")
    relative = {"XDG_CONFIG_HOME": "relative/dir"}  # the XDG spec says ignore it
    assert S.config_dir("linux", relative, home) == home / ".config/rftools-kicad"
    win = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    assert S.config_dir("win32", win, home) == Path(win["APPDATA"]) / "rftools-kicad"
    assert S.cache_dir("win32", win, home) == Path(win["LOCALAPPDATA"]) / "rftools-kicad"
    assert S.config_dir("win32", {}, home) == home / "AppData/Roaming/rftools-kicad"


def test_the_plugin_files_are_under_those_dirs():
    assert S.settings_path().name == "settings.json"
    assert S.history_path().parent == S.settings_path().parent
    assert S.results_path().parent == S.cache_dir()
    assert S.results_path().parent.name == "rftools-kicad"


# ── The public identifier ────────────────────────────────────────────────────


def test_public_id_is_rfc_and_eight_characters():
    assert S.public_id(KEY) == KEY[:12] == "rfc_TESTkey0"
    assert len(S.public_id(KEY)) == S.PUBLIC_ID_LENGTH == 12
    assert S.public_id("  " + KEY + "\n") == KEY[:12]
    assert S.public_id(None) == "(no key)"
    assert S.public_id("sk_live_abcdefghijklmnop") == "(unrecognised key)"
    assert S.public_id("rfc_short") == "(unrecognised key)"


def test_redact_cuts_every_key_to_its_public_id():
    text = f"key {KEY} and {OTHER_KEY} and rfc_abc"
    assert S.redact(text) == f"key {KEY[:12]}… and {OTHER_KEY[:12]}… and rfc_abc"
    odd = "not-an-rfc-key-0123456789"
    assert odd not in S.redact(f"odd {odd}", keys=(odd,))


def test_the_redacting_filter_cleans_messages_and_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(S.RedactingFilter(keys=(KEY,)))
    logger = logging.getLogger("rftools_kicad.test_redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise RuntimeError("Bearer " + KEY)
        except RuntimeError:
            logger.exception("request with %s failed", KEY)
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert KEY[:12] in output
    assert KEY[:13] not in output


# ── Settings file ────────────────────────────────────────────────────────────


def usage_client(key):
    return FakeClient()


def test_a_key_is_checked_with_usage_then_saved(tmp_path):
    path = tmp_path / "cfg" / "settings.json"
    seen = []

    def factory(key):
        seen.append(key)
        return FakeClient()

    usage = S.save_key("  " + KEY + " ", factory, path=path)
    assert seen == [KEY]
    assert usage["remaining"] == USAGE["remaining"]
    data = json.loads(path.read_text())
    assert data["apiKey"] == KEY and data["keyId"] == KEY[:12]
    loaded = S.load_settings(path, environ={})
    assert (loaded.api_key, loaded.key_source, loaded.key_id) == (KEY, S.SOURCE_SETTINGS, KEY[:12])
    assert KEY not in repr(loaded)


@posix_only
def test_the_settings_file_is_owner_only_from_creation(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "settings.json"
    opened = []
    real_open = os.open

    def spy(file, flags, mode=0o777, *args, **kwargs):
        opened.append((str(file), flags, mode))
        return real_open(file, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    old_umask = os.umask(0)  # even with nothing masked
    try:
        S.save_key(KEY, usage_client, path=path)
    finally:
        os.umask(old_umask)
    [(name, flags, mode)] = [o for o in opened if "settings" in o[0]]
    assert mode == 0o600 and flags & os.O_CREAT and flags & os.O_EXCL
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_an_existing_wider_file_is_replaced_by_an_owner_only_one(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"options": {"grid": 0.005}}))
    path.chmod(0o644)
    S.save_key(KEY, usage_client, path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["options"] == {"grid": 0.005}


def test_the_environment_overrides_the_saved_key(tmp_path):
    path = tmp_path / "settings.json"
    S.save_key(KEY, usage_client, path=path)
    loaded = S.load_settings(path, environ={"RFTOOLS_API_KEY": OTHER_KEY})
    assert (loaded.api_key, loaded.key_source) == (OTHER_KEY, S.SOURCE_ENVIRONMENT)
    assert S.load_settings(path, environ={"RFTOOLS_API_KEY": "  "}).api_key == KEY
    assert S.load_settings(tmp_path / "none.json", environ={}).api_key is None


def test_a_rejected_key_is_not_saved(tmp_path):
    path = tmp_path / "settings.json"
    client = FakeClient()
    client.usage_error = api_error("auth")
    with pytest.raises(S.KeyRejected) as info:
        S.save_key(KEY, lambda key: client, path=path)
    assert S.KEY_URL in str(info.value) and KEY[:13] not in str(info.value)
    assert not path.exists()


def test_a_key_that_cannot_be_checked_is_not_saved(tmp_path):
    path = tmp_path / "settings.json"
    client = FakeClient()
    client.usage_error = ConnectionError("offline")
    with pytest.raises(S.KeyNotChecked, match="Could not reach rftools.io"):
        S.save_key(KEY, lambda key: client, path=path)
    assert not path.exists()


def test_a_key_is_checked_through_the_plugins_client(tmp_path, service):
    path = tmp_path / "settings.json"
    service.answer(401, {"detail": "This API key is not valid. It may have been revoked.",
                         "errorKind": "invalid_request"})
    service.answer(200, dict(USAGE))
    with pytest.raises(S.KeyRejected, match="did not accept the API key rfc_OTHERkey"):
        S.save_key(OTHER_KEY, lambda key: service.client(key), path=path)
    assert not path.exists()
    usage = S.save_key(KEY, lambda key: service.client(key), path=path)
    assert usage["remaining"] == 40 and usage["tier"] == "free"
    assert [(r.method, r.path) for r in service.requests] == [("GET", "/api/py/v1/usage")] * 2
    assert [r.headers["x-api-key"] for r in service.requests] == [OTHER_KEY, KEY]
    assert json.loads(path.read_text())["apiKey"] == KEY


def test_something_that_is_not_a_key_is_refused_without_a_request(tmp_path):
    def factory(key):
        raise AssertionError("no request for a malformed key")

    with pytest.raises(S.KeyRejected, match="start with rfc_"):
        S.save_key("sk_live_123456789", factory, path=tmp_path / "settings.json")


def test_forget_key_and_options(tmp_path):
    path = tmp_path / "settings.json"
    S.save_key(KEY, usage_client, path=path)
    S.save_options({"grid": 0.002}, path=path)
    assert S.load_settings(path, environ={}).options == {"grid": 0.002}
    S.forget_key(path)
    loaded = S.load_settings(path, environ={})
    assert loaded.api_key is None and loaded.options == {"grid": 0.002}


def test_an_unreadable_settings_file_is_empty(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[not settings")
    assert S.load_settings(path, environ={}).api_key is None


def test_no_log_record_carries_more_of_a_key_than_its_public_id(tmp_path, caplog):
    # The conftest guard checks every test; this one drives every logging path
    # that could see a key and checks the records explicitly.
    caplog.set_level(logging.DEBUG)
    S.save_key(KEY, usage_client, path=tmp_path / "settings.json")
    for kind in ("quota", "auth", "rate", "offline", "server", "invalid", "not_found"):
        client = FakeClient()
        client.errors.append(api_error(kind, retry_after=10_000))
        api = A.Api(client, key_id=S.public_id(KEY), sleep=lambda s: None)
        from rftools_kicad.mapping import single_ended
        from rftools_kicad.stackup import layer_model
        from tests.support import two_layer

        api.run(single_ended(layer_model(two_layer()), "F.Cu", 0.3))
    assert caplog.records, "the paths above log"
    assert any(KEY[:12] in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert KEY[:13] not in record.getMessage()


def test_settings_repr_never_shows_the_key():
    settings = S.Settings(api_key=KEY, key_source=S.SOURCE_SETTINGS)
    assert KEY[:13] not in repr(settings) and KEY[:12] in repr(settings)
    assert sys.modules["rftools_kicad.settings"] is S
