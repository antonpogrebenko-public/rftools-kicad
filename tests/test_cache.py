"""The result cache: exact keys, seven-day lifetime, cleared on a plugin version change."""
from __future__ import annotations

import json

from rftools_kicad.cache import MAX_AGE_SECONDS, ResultCache, cache_key

PAYLOAD = {"slug": "microstrip-impedance", "inputs": {"traceWidth": 0.3, "substrateHeight": 1.51}}
RESPONSE = {"slug": "microstrip-impedance", "values": {"impedance": 80.1},
            "provenance": {"version": "api@0123456789ab"}}


class Clock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_stored_result_is_returned_with_its_provenance(tmp_path):
    cache = ResultCache(tmp_path / "results.json", clock=Clock())
    cache.put(PAYLOAD, RESPONSE)
    entry = ResultCache(tmp_path / "results.json", clock=Clock()).get(PAYLOAD)
    assert entry["response"] == RESPONSE
    assert entry["request"] == PAYLOAD
    assert entry["response"]["provenance"]["version"] == "api@0123456789ab"


def test_the_key_is_the_exact_request():
    assert cache_key(PAYLOAD) == cache_key(json.loads(json.dumps(PAYLOAD)))
    reordered = {"inputs": {"substrateHeight": 1.51, "traceWidth": 0.3}, "slug": PAYLOAD["slug"]}
    assert cache_key(reordered) == cache_key(PAYLOAD)
    nudged = {**PAYLOAD, "inputs": {**PAYLOAD["inputs"], "traceWidth": 0.30000000000000004}}
    assert cache_key(nudged) != cache_key(PAYLOAD)
    solve = {**PAYLOAD, "solveFor": "traceWidth", "target": {"output": "impedance", "value": 50}}
    assert cache_key(solve) != cache_key(PAYLOAD)
    assert cache_key({**solve, "grid": 0.001}) != cache_key(solve)


def test_entries_expire_after_seven_days(tmp_path):
    clock = Clock()
    cache = ResultCache(tmp_path / "results.json", clock=clock)
    cache.put(PAYLOAD, RESPONSE)
    clock.now += MAX_AGE_SECONDS
    assert cache.get(PAYLOAD) is not None
    clock.now += 1
    assert cache.get(PAYLOAD) is None
    assert ResultCache(tmp_path / "results.json", clock=clock).get(PAYLOAD) is None


def test_a_new_plugin_version_starts_afresh(tmp_path):
    path = tmp_path / "results.json"
    ResultCache(path, clock=Clock(), plugin_version="0.1.0").put(PAYLOAD, RESPONSE)
    assert ResultCache(path, clock=Clock(), plugin_version="0.1.0").get(PAYLOAD) is not None
    newer = ResultCache(path, clock=Clock(), plugin_version="0.2.0")
    assert newer.get(PAYLOAD) is None
    newer.put({"slug": "via-calculator", "inputs": {}}, {"values": {}})
    assert json.loads(path.read_text())["pluginVersion"] == "0.2.0"
    assert len(json.loads(path.read_text())["entries"]) == 1


def test_a_damaged_file_is_an_empty_cache(tmp_path):
    path = tmp_path / "results.json"
    path.write_text("{not json")
    cache = ResultCache(path, clock=Clock())
    assert cache.get(PAYLOAD) is None
    cache.put(PAYLOAD, RESPONSE)
    assert ResultCache(path, clock=Clock()).get(PAYLOAD) is not None


def test_an_unwritable_cache_only_logs(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    cache = ResultCache(blocker / "results.json", clock=Clock())
    cache.put(PAYLOAD, RESPONSE)  # the directory cannot be created; no exception
    assert "Could not write the result cache" in caplog.text


def test_clear(tmp_path):
    cache = ResultCache(tmp_path / "results.json", clock=Clock())
    cache.put(PAYLOAD, RESPONSE)
    cache.clear()
    assert PAYLOAD not in ResultCache(tmp_path / "results.json", clock=Clock())
