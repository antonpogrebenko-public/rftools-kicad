"""The API key and the user's options, and where the plugin keeps its files.

``<user config dir>/rftools-kicad/settings.json`` holds the key (design
Decision 6), created readable by its owner only (mode 0600 from the moment it
exists, in a 0700 directory, on macOS and Linux; the user profile's ACL on
Windows). ``RFTOOLS_API_KEY`` in the environment overrides it, as it does for
the SDK. A pasted key is checked with the usage endpoint, which never spends a
call, before it is saved.

The key is never logged or shown whole: :func:`public_id` gives the part the
rftools.io dashboard lists (``rfc_`` and the next 8 characters, the service's
``key_id``), and :class:`RedactingFilter` rewrites any key-shaped text in a log
record down to that.

Directories, without a dependency:

    config  macOS ~/Library/Application Support   Windows %APPDATA%
            Linux $XDG_CONFIG_HOME, else ~/.config
    cache   macOS ~/Library/Caches                Windows %LOCALAPPDATA%
            Linux $XDG_CACHE_HOME, else ~/.cache
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rftools_kicad import CLIENT_NAME

log = logging.getLogger(__name__)

APP_DIR = "rftools-kicad"
SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"
RESULTS_FILE = "results.json"

ENV_VAR = "RFTOOLS_API_KEY"
#: api-metering Decision 3: the dashboard creates a key labelled for this client.
KEY_URL = f"https://rftools.io/dashboard/?newKey=1&client={CLIENT_NAME}"

#: Keys are ``rfc_`` + a URL-safe token; the service's key_id, the public
#: identifier the dashboard lists, is the first 12 characters
#: (backend/src/api/routes/api_keys.py: ``key_id = key[:12]``).
KEY_PREFIX = "rfc_"
PUBLIC_ID_LENGTH = 12
_KEY_TEXT = re.compile(r"rfc_[A-Za-z0-9_\-]+")

SOURCE_ENVIRONMENT = "environment"
SOURCE_SETTINGS = "settings"


# ── Directories ──────────────────────────────────────────────────────────────


def config_dir(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "darwin":
        root = home / "Library" / "Application Support"
    elif platform.startswith("win"):
        root = Path(environ["APPDATA"]) if environ.get("APPDATA") else home / "AppData" / "Roaming"
    else:
        xdg = environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config"
    return root / APP_DIR


def cache_dir(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "darwin":
        root = home / "Library" / "Caches"
    elif platform.startswith("win"):
        local = environ.get("LOCALAPPDATA")
        root = Path(local) if local else home / "AppData" / "Local"
    else:
        xdg = environ.get("XDG_CACHE_HOME")
        root = Path(xdg) if xdg and os.path.isabs(xdg) else home / ".cache"
    return root / APP_DIR


def settings_path() -> Path:
    return config_dir() / SETTINGS_FILE


def history_path() -> Path:
    """The net-class write history (design Decision 5), beside the settings."""
    return config_dir() / HISTORY_FILE


def results_path() -> Path:
    return cache_dir() / RESULTS_FILE


# ── The key's public identifier ──────────────────────────────────────────────


def public_id(key: str | None) -> str:
    """What may be shown of *key*: ``rfc_`` and the next 8 characters, never more."""
    key = (key or "").strip()
    if key.startswith(KEY_PREFIX) and len(key) >= PUBLIC_ID_LENGTH:
        return key[:PUBLIC_ID_LENGTH]
    return "(no key)" if not key else "(unrecognised key)"


def redact(text: str, keys: tuple = ()) -> str:
    """*text* with every key in it cut down to its public identifier."""

    def cut(match: re.Match) -> str:
        found = match.group(0)
        if len(found) <= PUBLIC_ID_LENGTH:
            return found
        return found[:PUBLIC_ID_LENGTH] + "…"

    text = _KEY_TEXT.sub(cut, text)
    for key in keys:
        if key and key in text:
            text = text.replace(key, public_id(key) + "…")
    return text


class RedactingFilter(logging.Filter):
    """Rewrites a record so no key survives in its message or traceback.

    Attach it to a *handler* (a logger's own filters do not see records
    propagated from other loggers, such as the SDK's or httpx's).
    """

    def __init__(self, keys: tuple = ()) -> None:
        super().__init__()
        self.keys = tuple(k for k in keys if k)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message, self.keys)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.keys)
        record.exc_info = None
        return True


# ── Settings ─────────────────────────────────────────────────────────────────


class KeyError_(Exception):
    """Base for a key that could not be saved; the message is shown to the user."""


class KeyRejected(KeyError_):
    """The service did not accept the key (HTTP 401)."""


class KeyNotChecked(KeyError_):
    """The key could not be checked (offline, or the service failed); it was not saved."""


@dataclass
class Settings:
    """What the plugin reads at start: the key, where it came from, and the options."""

    api_key: str | None = None
    key_source: str | None = None
    options: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def key_id(self) -> str:
        return public_id(self.api_key)

    def __repr__(self) -> str:  # never the key
        return (
            f"Settings(key={self.key_id!r}, key_source={self.key_source!r}, "
            f"options={self.options!r}, path={str(self.path)!r})"
        )


def load_settings(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> Settings:
    """The settings file, with ``RFTOOLS_API_KEY`` taking the key's place when set."""
    path = settings_path() if path is None else Path(path)
    environ = os.environ if environ is None else environ
    data = _read(path)
    settings = Settings(options=dict(data.get("options") or {}), path=path)
    env_key = (environ.get(ENV_VAR) or "").strip()
    saved = data.get("apiKey")
    if env_key:
        settings.api_key, settings.key_source = env_key, SOURCE_ENVIRONMENT
    elif isinstance(saved, str) and saved.strip():
        settings.api_key, settings.key_source = saved.strip(), SOURCE_SETTINGS
    return settings


def save_key(
    key: str,
    client_factory: Callable[[str], Any],
    path: Path | None = None,
) -> dict:
    """Check *key* with a usage request, then save it; returns the usage figures.

    ``client_factory(key)`` builds an SDK-like client (``rftools.Client``).
    Raises :class:`KeyRejected` when the service refuses the key and
    :class:`KeyNotChecked` when it cannot be asked; in both cases nothing is
    written. The usage request is never metered.
    """
    from rftools_kicad.api import AUTH, Api, SdkUnavailable  # api imports this module

    key = (key or "").strip()
    if not key.startswith(KEY_PREFIX) or len(key) <= PUBLIC_ID_LENGTH:
        raise KeyRejected(
            f"That does not look like an rftools.io API key (they start with {KEY_PREFIX}). "
            f"Get a free key at {KEY_URL}"
        )
    try:
        client = client_factory(key)
    except SdkUnavailable as exc:
        raise KeyNotChecked(f"The key was not saved: {exc}") from exc
    usage, refusal = Api(client, key_id=public_id(key)).usage()
    if refusal is not None:
        if refusal.kind == AUTH:
            raise KeyRejected(refusal.message)
        raise KeyNotChecked(f"The key was not saved: {refusal.message}")
    path = settings_path() if path is None else Path(path)
    data = _read(path)
    data.update({
        "version": 1,
        "apiKey": key,
        "keyId": public_id(key),
        "savedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    })
    write_private_json(path, data)
    log.info("Saved API key %s to %s", public_id(key), path)
    return usage


def forget_key(path: Path | None = None) -> None:
    path = settings_path() if path is None else Path(path)
    data = _read(path)
    for name in ("apiKey", "keyId", "savedAt"):
        data.pop(name, None)
    write_private_json(path, data)


def save_options(options: Mapping[str, Any], path: Path | None = None) -> None:
    path = settings_path() if path is None else Path(path)
    data = _read(path)
    data["options"] = dict(options)
    write_private_json(path, data)


def write_private_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write *data* so the file is owner-only from creation, then rename it into place.

    The temporary file is created with ``os.open(..., 0o600)`` rather than
    chmod-ed afterwards, so no other user can open it in between; the rename
    replaces any earlier file, whatever its mode was.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = directory / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(data), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Ignoring an unreadable settings file at %s: %s", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}
