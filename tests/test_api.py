"""The API wrapper: cache, refusals mapped by the client's error types, what is sent."""
from __future__ import annotations

import json
import numbers

import pytest

from rftools_kicad import api as A
from rftools_kicad import client as C
from rftools_kicad.cache import ResultCache
from rftools_kicad.mapping import single_ended, trace_current
from rftools_kicad.settings import KEY_URL, public_id
from rftools_kicad.solve import target_request
from rftools_kicad.stackup import layer_model
from tests.support import (
    KEY,
    QUOTA_BODY,
    USAGE,
    FakeClient,
    answer_error,
    api_error,
    calc_response,
    two_layer,
)


@pytest.fixture
def model():
    return layer_model(two_layer())


@pytest.fixture
def cache(tmp_path):
    return ResultCache(tmp_path / "results.json")


def computations(model, n):
    return [single_ended(model, "F.Cu", 0.1 + i / 100) for i in range(n)]


def make_api(client, cache=None, **kw):
    return A.Api(client, cache, key_id=public_id(KEY), sleep=kw.pop("sleep", _no_sleep), **kw)


def _no_sleep(seconds):
    raise AssertionError(f"unexpected sleep {seconds}")


# ── Results and the cache ────────────────────────────────────────────────────


def test_a_result_is_stored_with_its_provenance_and_a_repeat_makes_no_request(model, cache):
    client = FakeClient()
    api = make_api(client, cache)
    [c] = computations(model, 1)
    first = api.run(c)
    assert first.ok and not first.cached
    assert first.response["values"] == {"impedance": 50.0}
    assert first.response["provenance"]["version"] == "api@0123456789ab"
    assert len(client.metered) == 1

    again = make_api(client, ResultCache(cache.path)).run(c)  # a later run, same file
    assert again.cached and again.response == first.response
    assert len(client.metered) == 1  # spec: "Repeating a run" makes no API call


def test_usage_is_read_without_a_metered_call(model):
    client = FakeClient()
    usage, refusal = make_api(client).usage()
    assert refusal is None
    assert usage == {"tier": "free", "period": "2026-09", "resetAt": "2026-10-01T00:00:00Z",
                     "allowance": 50, "used": 10, "remaining": 40}
    assert client.metered == []


def test_a_solve_goes_through_client_solve(model, cache):
    client = FakeClient()
    solve = target_request(single_ended(model, "F.Cu", 0.3), 50)
    outcome = make_api(client, cache).run(solve)
    assert outcome.ok and outcome.response["value"] == 0.321
    assert outcome.response["result"]["values"] == {"impedance": 50}
    [(method, body)] = client.metered
    assert method == "solve"
    assert body == {
        "slug": "microstrip-impedance", "inputs": solve.input_dict, "solveFor": "traceWidth",
        "target": {"output": "impedance", "value": 50.0}, "grid": 0.001,
    }


# ── Refusals, by type ────────────────────────────────────────────────────────


def test_quota_stops_the_run_and_keeps_what_was_computed(model, cache):
    # Spec scenario "Allowance spent mid-run".
    client = FakeClient()
    api = make_api(client, cache)
    a, b, c = computations(model, 3)
    first = api.run(a)
    client.errors.append(api_error("quota", overage_url="https://rftools.io/dashboard/#overage"))
    second = api.run(b)
    third = api.run(c)
    assert first.ok  # computed before the refusal: still there
    refusal = second.refusal
    assert refusal.kind == A.QUOTA and refusal.stops_run
    assert refusal.reset_at == "2026-10-01T00:00:00Z"
    assert refusal.upgrade_url == "https://rftools.io/pricing"
    assert refusal.overage_url == "https://rftools.io/dashboard/#overage"
    assert (refusal.limit, refusal.used, refusal.status) == (5, 5, 402)
    assert "reset at 2026-10-01T00:00:00Z" in refusal.message
    assert third.refusal is refusal
    assert len(client.metered) == 2  # nothing sent after the refusal
    assert api.run(a).cached  # a cached result is still answered


def test_invalid_key_gives_the_key_link_and_only_the_public_id(model):
    client = FakeClient()
    client.errors.append(api_error("auth"))
    refusal = make_api(client).run(computations(model, 1)[0]).refusal
    assert refusal.kind == A.AUTH and refusal.stops_run
    assert refusal.key_url == KEY_URL
    assert KEY_URL.endswith("?newKey=1&client=kicad-plugin")
    assert public_id(KEY) in refusal.message and KEY not in refusal.message


def test_the_services_key_link_wins_when_it_names_one(model):
    client = FakeClient()
    client.errors.append(api_error("auth", key_url="https://rftools.io/dashboard/?newKey=1"))
    refusal = make_api(client).run(computations(model, 1)[0]).refusal
    assert refusal.key_url == "https://rftools.io/dashboard/?newKey=1"


def test_rate_limit_is_waited_out_once_then_stops(model):
    client = FakeClient()
    waits = []
    api = make_api(client, sleep=waits.append)
    a, b, c = computations(model, 3)
    client.errors.append(api_error("rate", retry_after=7))
    assert api.run(a).ok  # waited, retried, answered
    assert waits == [7.0]
    client.errors.extend([api_error("rate", retry_after=7)])
    refusal = api.run(b).refusal
    assert refusal.kind == A.RATE_LIMITED and refusal.retry_after == 7
    assert "Wait 7 s" in refusal.message
    assert waits == [7.0]  # only once
    assert api.run(c).refusal is refusal
    assert len(client.metered) == 3


def test_a_rate_limit_longer_than_the_cap_is_not_waited(model):
    client = FakeClient()
    client.errors.append(api_error("rate", retry_after=3600))
    refusal = make_api(client).run(computations(model, 1)[0]).refusal
    assert refusal.kind == A.RATE_LIMITED and refusal.retry_after == 3600


def test_offline_shows_cached_results(model, cache):
    # Spec scenario "Offline".
    client = FakeClient()
    api = make_api(client, cache)
    a, b = computations(model, 2)
    assert api.run(a).ok
    client.errors.append(api_error("offline"))
    refusal = api.run(b).refusal
    assert refusal.kind == A.OFFLINE
    assert "Could not reach rftools.io" in refusal.message
    assert api.run(a).cached
    api.reset()  # the dialog's "retry"
    assert api.run(b).ok


def test_a_server_failure_stops_and_an_invalid_input_does_not(model):
    client = FakeClient()
    api = make_api(client)
    a, b, c = computations(model, 3)
    client.errors.append(api_error("invalid"))
    invalid = api.run(a).refusal
    assert invalid.kind == A.INVALID and not invalid.stops_run
    assert "(traceWidth)" in invalid.message
    client.errors.append(api_error("server"))
    server = api.run(b).refusal
    assert server.kind == A.SERVICE and server.status == 503 and server.stops_run
    assert api.run(c).refusal is server


def test_classification_is_by_type_never_by_message_text():
    quota = C.QuotaError("401 Unauthorized: invalid API key", status_code=402)
    auth = C.AuthError("Quota exceeded, 429 rate limited", status_code=401)
    assert A.classify(quota).kind == A.QUOTA
    assert A.classify(auth).kind == A.AUTH
    assert A.classify(C.NotFound("rate limit")).kind == A.NOT_FOUND
    assert A.classify(C.TransportError("HTTP 402 quota")).kind == A.OFFLINE
    assert A.classify(C.ServiceError("could not reach")).kind == A.SERVICE
    # and an answer is typed by its status whatever its body says
    assert A.classify(answer_error(402, {"detail": "invalid API key"})).kind == A.QUOTA
    assert A.classify(answer_error(401, {"detail": "quota exceeded"})).kind == A.AUTH


def test_every_status_the_client_types_maps_to_its_refusal():
    kinds = {s: A.classify(answer_error(s, b"<html>")).kind
             for s in (400, 401, 402, 403, 404, 405, 422, 429, 500, 502, 503, 504)}
    assert kinds == {
        400: A.INVALID, 401: A.AUTH, 402: A.QUOTA, 403: A.AUTH, 404: A.NOT_FOUND,
        405: A.SERVICE, 422: A.INVALID, 429: A.RATE_LIMITED, 500: A.SERVICE, 502: A.SERVICE,
        503: A.SERVICE, 504: A.SERVICE,
    }
    assert A.classify(api_error("offline")).kind == A.OFFLINE


def test_another_exception_with_a_status_code_classifies_by_it():
    class Refused(Exception):
        def __init__(self, status):
            super().__init__("anything at all")
            self.status_code = status

    kinds = {s: A.classify(Refused(s)).kind for s in (0, 400, 401, 402, 403, 404, 429, 502)}
    assert kinds == {
        0: A.OFFLINE, 400: A.INVALID, 401: A.AUTH, 402: A.QUOTA, 403: A.AUTH,
        404: A.NOT_FOUND, 429: A.RATE_LIMITED, 502: A.SERVICE,
    }
    assert A.classify(ConnectionRefusedError()).kind == A.OFFLINE
    assert A.classify(TimeoutError()).kind == A.OFFLINE


def test_a_403_names_the_account_not_a_revoked_key():
    refusal = A.classify(api_error("auth", status=403), key_id=public_id(KEY))
    assert refusal.kind == A.AUTH and refusal.status == 403
    assert refusal.message.startswith("rftools.io refused this request for the account of key")


def test_a_certificate_failure_is_offline_and_says_what_failed():
    import ssl
    import urllib.error

    failure = ssl.SSLCertVerificationError(1, "certificate verify failed")
    failure.verify_message = "unable to get local issuer certificate"
    refusal = A.classify(C.transport_error(urllib.error.URLError(failure)))
    assert refusal.kind == A.OFFLINE and refusal.stops_run
    assert refusal.message.startswith("The TLS certificate of rftools.io could not be verified "
                                      "(unable to get local issuer certificate)")
    assert "Recreate Plugin Environment" in refusal.message
    plain = A.classify(api_error("offline"))
    assert plain.message.startswith("Could not reach rftools.io.")


def test_the_client_is_the_plugins_own():
    client = A.make_client(KEY)
    assert isinstance(client, C.Client) and client.base_url == "https://rftools.io/api/py/v1"
    assert KEY not in repr(client)


def test_a_bug_is_not_a_refusal(model):
    client = FakeClient(calculate=lambda slug, inputs: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        make_api(client).run(computations(model, 1)[0])


def test_usage_refusal_is_returned_not_raised():
    client = FakeClient()
    client.usage_error = ConnectionError("down")
    usage, refusal = make_api(client).usage()
    assert usage is None and refusal.kind == A.OFFLINE


# ── What is sent ─────────────────────────────────────────────────────────────


def _only_numbers(inputs):
    return all(
        isinstance(v, numbers.Real) and not isinstance(v, bool) for v in inputs.values()
    )


def test_requests_hold_a_calculator_identifier_and_numbers_only(model, cache):
    # Spec: "The plugin SHALL send the service only calculator inputs".
    client = FakeClient()
    api = make_api(client, cache)
    forward = single_ended(model, "F.Cu", 0.3)
    api.run(forward)
    api.run(target_request(forward, 50))
    api.run(trace_current(model, "B.Cu", 2))
    design_words = ("F.Cu", "B.Cu", "dielectric 1", "FR4", "Default", "F.Mask")
    assert len(client.metered) == 3
    for method, body in client.metered:
        assert set(body) <= {"slug", "inputs", "solveFor", "target", "grid", "range"}
        assert isinstance(body["slug"], str) and _only_numbers(body["inputs"])
        if method == "solve":
            assert body["solveFor"] in body["inputs"]
            assert isinstance(body["target"]["value"], float)
        text = json.dumps(body)
        assert not any(word in text for word in design_words), text


def test_through_the_client_the_body_is_a_slug_and_numbers(model, service):
    service.answer(200, dict(USAGE))
    service.answer(200, {
        "slug": "microstrip-impedance", "values": {"impedance": 80.9}, "warnings": [],
        "errors": [], "provenance": {"version": "api@0123456789ab", "formulaRef": "H&J"},
    }, {"X-Usage-Allowance": "50", "X-Usage-Used": "11", "X-Usage-Reset": "2026-10-01T00:00:00Z"})
    api = make_api(service.client())
    usage, _ = api.usage()
    outcome = api.run(single_ended(model, "F.Cu", 0.3))
    assert usage["remaining"] == 40
    assert outcome.response["values"] == {"impedance": 80.9}
    assert outcome.response["provenance"]["version"] == "api@0123456789ab"
    assert api.last_usage["remaining"] == 39
    request = service.requests[-1]
    assert request.json == {"slug": "microstrip-impedance", "inputs": single_ended(
        model, "F.Cu", 0.3).input_dict}
    assert _only_numbers(request.json["inputs"])
    assert request.headers["x-api-key"] == KEY  # the key travels only as the credential
    assert KEY not in request.body.decode()


def test_through_the_client_a_402_body_reaches_the_dialog(model, service):
    service.answer(402, QUOTA_BODY, {"Retry-After": "86400"})
    refusal = make_api(service.client()).run(single_ended(model, "F.Cu", 0.3)).refusal
    assert refusal.kind == A.QUOTA
    assert (refusal.reset_at, refusal.retry_after) == ("2026-10-01T00:00:00Z", 86400)
    assert refusal.upgrade_url == "https://rftools.io/pricing" and refusal.overage_url is None
    assert (refusal.limit, refusal.used, refusal.reason) == (5, 5, "allowance")


def test_through_the_client_a_429_is_waited_out_once(model, service):
    service.answer(429, {"detail": "Too many requests.", "errorKind": "rate_limited",
                         "limit": 30, "windowSeconds": 60}, {"Retry-After": "3"})
    service.answer(200, calc_response("microstrip-impedance", {}, {"impedance": 50.0}))
    waits = []
    outcome = make_api(service.client(), sleep=waits.append).run(single_ended(model, "F.Cu", 0.3))
    assert outcome.ok and waits == [3.0] and len(service.requests) == 2


def test_through_the_client_a_404_page_is_not_found(model, service):
    service.answer(404, b"<!DOCTYPE html><html><body>404</body></html>")
    refusal = make_api(service.client()).run(single_ended(model, "F.Cu", 0.3)).refusal
    assert refusal.kind == A.NOT_FOUND and not refusal.stops_run
    assert refusal.message == "rftools.io has no calculator microstrip-impedance."


def test_through_the_client_a_refused_input_is_named(model, service):
    service.answer(400, {"detail": "Input 'traceWidth' is not a finite number.",
                         "errorKind": "invalid_request", "inputs": ["traceWidth"]})
    refusal = make_api(service.client()).run(single_ended(model, "F.Cu", 0.3)).refusal
    assert refusal.kind == A.INVALID and not refusal.stops_run
    assert refusal.message == "rftools.io refused the inputs for microstrip-impedance (traceWidth)."


def test_through_the_client_no_connection_is_offline(model):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = C.Client(KEY, base_url=f"http://127.0.0.1:{port}/api/py/v1")
    refusal = make_api(client).run(single_ended(model, "F.Cu", 0.3)).refusal
    assert refusal.kind == A.OFFLINE and refusal.stops_run


def test_through_the_client_a_solve_body(model, service):
    forward = single_ended(model, "F.Cu", 3.0)
    solve = target_request(forward, 50)
    service.answer(200, {
        "slug": "microstrip-impedance", "solveFor": "traceWidth",
        "target": {"output": "impedance", "value": 50}, "grid": 0.001, "value": 2.784,
        "unrounded": 2.784010722886311, "reached": True, "evaluations": 31, "warnings": [],
        "result": {"slug": "microstrip-impedance", "values": {"impedance": 50.0001127254694},
                   "warnings": [], "errors": [],
                   "provenance": {"version": "api@0123456789ab"}},
    })
    outcome = make_api(service.client()).run(solve)
    [request] = service.requests
    assert request.path == "/api/py/v1/calculate/solve"
    assert request.json == solve.payload()
    assert outcome.response["value"] == 2.784
    assert outcome.response["unrounded"] == 2.784010722886311
    assert outcome.response["solveFor"] == "traceWidth" and outcome.response["reached"] is True
    assert outcome.response["result"]["values"]["impedance"] == 50.0001127254694
