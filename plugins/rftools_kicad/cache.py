"""Results the API has computed, kept on disk so a repeated run costs nothing.

``<user cache dir>/rftools-kicad/results.json``::

    {"pluginVersion": "0.1.0",
     "entries": {"<key>": {"storedAt": 1790000000.0,
                           "request": {...the exact request body...},
                           "response": {...the full result, provenance included...}}}}

The key is a SHA-256 of the request body as the service receives it (the
calculator slug, the exact inputs and, for a solve, the solved input, target,
grid and range; see ``mapping.Computation.payload``), serialised with sorted
keys and Python's round-trip float repr, so equal inputs share an entry and
any change of any digit misses. Entries expire after seven days, and the
whole file is discarded when the plugin version changes (a new version may
map a layer differently, and the engine version that computed a result is
shown beside it anyway).

A damaged file is treated as empty: the cache can only save calls, never cause
a failure.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rftools_kicad import __version__

log = logging.getLogger(__name__)

FILE_NAME = "results.json"
MAX_AGE_SECONDS = 7 * 24 * 3600


def cache_key(payload: dict) -> str:
    """The cache key of a request body (a Computation's ``payload()``)."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ResultCache:
    """A JSON file of results keyed by request.

    ``clock`` returns seconds since the epoch (injectable for tests);
    ``plugin_version`` is the version the file must carry to be read.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_age: float = MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
        plugin_version: str = __version__,
    ) -> None:
        self.path = Path(path)
        self.max_age = max_age
        self.clock = clock
        self.plugin_version = plugin_version
        self._entries: dict | None = None

    # ── Reading ──────────────────────────────────────────────────────────────

    def get(self, payload: dict) -> dict | None:
        """The stored entry for *payload* (``storedAt``, ``request``, ``response``), if fresh."""
        entry = self._load().get(cache_key(payload))
        if entry is None or not self._fresh(entry):
            return None
        return entry

    def __contains__(self, payload: dict) -> bool:
        return self.get(payload) is not None

    # ── Writing ──────────────────────────────────────────────────────────────

    def put(self, payload: dict, response: dict) -> dict:
        """Store *response* for *payload* and write the file; returns the entry."""
        entries = self._load()
        entry = {"storedAt": self.clock(), "request": payload, "response": response}
        entries[cache_key(payload)] = entry
        self._save()
        return entry

    def clear(self) -> None:
        self._entries = {}
        self._save()

    # ── Internals ────────────────────────────────────────────────────────────

    def _fresh(self, entry: dict) -> bool:
        stored = entry.get("storedAt")
        if not isinstance(stored, (int, float)):
            return False
        age = self.clock() - stored
        return 0 <= age <= self.max_age

    def _load(self) -> dict:
        if self._entries is not None:
            return self._entries
        self._entries = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._entries
        except (OSError, ValueError) as exc:
            log.warning("Ignoring an unreadable result cache at %s: %s", self.path, exc)
            return self._entries
        if not isinstance(data, dict) or data.get("pluginVersion") != self.plugin_version:
            log.info("Result cache is from another plugin version; starting afresh.")
            return self._entries
        entries = data.get("entries")
        if isinstance(entries, dict):
            self._entries = {
                k: v for k, v in entries.items()
                if isinstance(v, dict) and self._fresh(v)
            }
        return self._entries

    def _save(self) -> None:
        data = {"pluginVersion": self.plugin_version, "entries": self._entries or {}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".results-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=1, sort_keys=True, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                _unlink(tmp)
                raise
        except (OSError, ValueError) as exc:
            # A cache that cannot be written only costs calls next time.
            log.warning("Could not write the result cache at %s: %s", self.path, exc)


def _unlink(path: Any) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
