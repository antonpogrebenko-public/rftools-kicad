"""The standard-library client, over real HTTP to a fake rftools.io on 127.0.0.1."""
from __future__ import annotations

import json
import socket
import ssl
import sys
import urllib.error

import pytest

from rftools_kicad import __version__
from rftools_kicad import client as C
from tests.support import KEY, PROVENANCE, QUOTA_BODY, USAGE, calc_response, solve_response

MICROSTRIP = {"traceWidth": 0.3, "substrateHeight": 1.51, "dielectricConstant": 4.5,
              "copperThickness": 35.0}
USAGE_HEADERS = {"X-Usage-Allowance": "50", "X-Usage-Used": "11",
                 "X-Usage-Reset": "2026-10-01T00:00:00Z"}
HTML_404 = (b"<!DOCTYPE html><html><head><title>404: This page could not be found"
            b"</title></head><body><h1>404</h1></body></html>")


def no_key_in(*texts):
    for text in texts:
        assert KEY not in text and KEY[:13] not in text, text


# ── What is sent ─────────────────────────────────────────────────────────────


def test_calculate_posts_the_slug_and_the_inputs_and_nothing_else(service):
    service.answer(200, calc_response("microstrip-impedance", MICROSTRIP, {"impedance": 80.9}),
                   USAGE_HEADERS)
    client = service.client()
    result = client.calculate("microstrip-impedance", MICROSTRIP)
    [request] = service.requests
    assert (request.method, request.path) == ("POST", "/api/py/v1/calculate")
    assert request.json == {"slug": "microstrip-impedance", "inputs": MICROSTRIP}
    assert request.headers["x-api-key"] == KEY  # the key travels only as the credential
    assert request.headers["user-agent"] == f"rftools-kicad/{__version__}"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert result == C.CalculateResult(
        slug="microstrip-impedance", values={"impedance": 80.9}, warnings=[], errors=[],
        provenance=dict(PROVENANCE, inputs=MICROSTRIP),
        usage=C.Usage(allowance=50, used=11, remaining=39, reset_at="2026-10-01T00:00:00Z"),
    )
    assert client.last_usage == result.usage


def test_solve_posts_only_the_fields_named(service):
    inputs = {k: v for k, v in MICROSTRIP.items() if k != "traceWidth"}
    for _ in range(2):
        service.answer(200, solve_response("microstrip-impedance", inputs, "traceWidth",
                                           "impedance", 50, value=2.784, unrounded=2.7840107))
    client = service.client()
    result = client.solve("microstrip-impedance", inputs, "traceWidth", "impedance", 50.0,
                          grid=0.001, range=(0.01, 10))
    bare = client.solve("microstrip-impedance", inputs, "traceWidth", "impedance", 50.0)
    first, second = (r.json for r in service.requests)
    assert service.requests[0].path == "/api/py/v1/calculate/solve"
    assert first == {"slug": "microstrip-impedance", "inputs": inputs, "solveFor": "traceWidth",
                     "target": {"output": "impedance", "value": 50.0}, "grid": 0.001,
                     "range": [0.01, 10]}
    assert second == {"slug": "microstrip-impedance", "inputs": inputs, "solveFor": "traceWidth",
                      "target": {"output": "impedance", "value": 50.0}}
    assert (result.value, result.unrounded, result.reached) == (2.784, 2.7840107, True)
    assert (result.solve_for, result.grid, result.evaluations) == ("traceWidth", 0.001, 23)
    assert result.target == {"output": "impedance", "value": 50}
    assert result.result.values == {"impedance": 50}
    assert result.result.provenance["version"] == "api@0123456789ab"
    assert bare.usage is None  # no X-Usage-* headers on that answer


def test_usage_is_a_get_with_no_body(service):
    service.answer(200, dict(USAGE, overage={"calls": 0}, keys=[], rateLimitPerMinute=30))
    usage = service.client().usage()
    [request] = service.requests
    assert (request.method, request.path, request.body) == ("GET", "/api/py/v1/usage", b"")
    assert "content-type" not in request.headers
    assert usage == C.Usage(allowance=50, used=10, remaining=40,
                            reset_at="2026-10-01T00:00:00Z", tier="free", period="2026-09")


def test_the_default_service_is_rftools_io_over_https():
    client = C.Client(KEY)
    assert client.base_url == C.DEFAULT_BASE_URL == "https://rftools.io/api/py/v1"
    assert C.USER_AGENT == "rftools-kicad/0.1.0"


def test_the_key_is_never_sent_over_plain_http_to_another_host():
    with pytest.raises(ValueError, match="https"):
        C.Client(KEY, base_url="http://rftools.io/api/py/v1")
    with pytest.raises(ValueError, match="https"):
        C.Client(KEY, base_url="ftp://rftools.io/")
    assert C.Client(KEY, base_url="http://127.0.0.1:9/api/").base_url == "http://127.0.0.1:9/api"


def test_a_redirect_does_not_carry_the_key(service):
    service.answer(307, b"", {"Location": service.base_url + "/elsewhere"})
    service.answer(200, dict(USAGE))
    service.client().usage()
    first, redirected = service.requests
    assert first.headers["x-api-key"] == KEY
    assert redirected.path == "/api/py/v1/elsewhere" and "x-api-key" not in redirected.headers


# ── Refusals, by status ──────────────────────────────────────────────────────


def test_401_is_an_auth_error_with_the_key_link_when_named(service):
    service.answer(401, {"detail": "An API key is required. Get a free key at "
                                   "https://rftools.io/dashboard/?newKey=1.",
                         "errorKind": "invalid_request",
                         "keyUrl": "https://rftools.io/dashboard/?newKey=1"})
    service.answer(401, {"detail": "This API key is not valid. It may have been revoked.",
                         "errorKind": "invalid_request"})
    client = service.client()
    with pytest.raises(C.AuthError) as named:
        client.usage()
    with pytest.raises(C.AuthError) as unnamed:
        client.usage()
    assert named.value.key_url == "https://rftools.io/dashboard/?newKey=1"
    assert named.value.status_code == 401 and named.value.error_kind == "invalid_request"
    assert unnamed.value.key_url is None
    assert "It may have been revoked" in str(unnamed.value)


def test_403_is_an_auth_error_too(service):
    service.answer(403, {"detail": "Forbidden", "errorKind": "invalid_request"})
    with pytest.raises(C.AuthError) as info:
        service.client().calculate("microstrip-impedance", MICROSTRIP)
    assert info.value.status_code == 403


def test_402_carries_every_field_of_the_refusal_and_retry_after(service):
    body = dict(QUOTA_BODY, overageUrl="https://rftools.io/dashboard/#overage")
    service.answer(402, body, {"Retry-After": "86400", **USAGE_HEADERS})
    client = service.client()
    with pytest.raises(C.QuotaError) as info:
        client.calculate("microstrip-impedance", MICROSTRIP)
    error = info.value
    assert (error.status_code, error.error_kind, error.reason) == (402, "rate_limited", "allowance")
    assert (error.limit, error.used, error.retry_after) == (5, 5, 86400)
    assert error.reset_at == "2026-10-01T00:00:00Z"
    assert error.upgrade_url == "https://rftools.io/pricing"
    assert error.overage_url == "https://rftools.io/dashboard/#overage"
    assert client.last_usage.remaining == 39  # a refusal's headers are read too


def test_429_carries_retry_after(service):
    service.answer(429, {"detail": "Too many requests.", "errorKind": "rate_limited",
                         "limit": 30, "windowSeconds": 60}, {"Retry-After": "7"})
    with pytest.raises(C.RateLimitError) as info:
        service.client().calculate("microstrip-impedance", MICROSTRIP)
    assert (info.value.retry_after, info.value.limit, info.value.window_seconds) == (7, 30, 60)


def test_a_retry_after_that_is_not_seconds_is_none(service):
    service.answer(429, {"detail": "slow down"}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(C.RateLimitError) as info:
        service.client().usage()
    assert info.value.retry_after is None


def test_404_with_json_and_404_with_an_html_page(service):
    service.answer(404, {"detail": "Calculator 'nope' not found", "errorKind": "invalid_request"})
    service.answer(404, HTML_404)  # CloudFront's page for a path it has no route for
    client = service.client()
    with pytest.raises(C.NotFound) as json_404:
        client.calculate("nope", {})
    with pytest.raises(C.NotFound) as html_404:
        client.calculate("microstrip-impedance", MICROSTRIP)
    assert "Calculator 'nope' not found" in str(json_404.value)
    assert html_404.value.status_code == 404 and html_404.value.error_kind is None
    assert "<html" not in str(html_404.value)


def test_400_names_the_field_the_service_names(service):
    service.answer(400, {"detail": "'solveFor' is not an input of 'microstrip-impedance'.",
                         "errorKind": "invalid_request", "param": "solveFor",
                         "allowed": ["traceWidth"]})
    service.answer(400, {"detail": "Input 'traceWidth' is not a finite number.",
                         "errorKind": "invalid_request", "inputs": ["traceWidth"]})
    client = service.client()
    with pytest.raises(C.RequestError) as solve:
        client.solve("microstrip-impedance", MICROSTRIP, "width", "impedance", 50)
    with pytest.raises(C.RequestError) as forward:
        client.calculate("microstrip-impedance", dict(MICROSTRIP, traceWidth=float("nan")))
    assert solve.value.param == "solveFor"
    assert [f["param"] for f in solve.value.failures] == ["solveFor"]
    assert [f["param"] for f in forward.value.failures] == ["traceWidth"]
    assert b"NaN" in service.requests[1].body  # sent as given; the service refuses it


def test_422_from_the_framework_names_each_field(service):
    service.answer(422, {"detail": [
        {"loc": ["body", "inputs", "traceWidth"], "msg": "Input should be a valid number",
         "type": "float_parsing"},
        {"loc": ["body", "target", "value"], "msg": "Field required", "type": "missing"},
    ]})
    with pytest.raises(C.RequestError) as info:
        service.client().solve("microstrip-impedance", MICROSTRIP, "traceWidth", "impedance", 50)
    assert info.value.failures == [
        {"param": "inputs.traceWidth", "reason": "Input should be a valid number"},
        {"param": "target.value", "reason": "Field required"},
    ]
    assert "target.value: Field required" in str(info.value)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 405])
def test_a_server_failure_is_a_service_error(service, status):
    service.answer(status, {"detail": "Could not check the API key.", "errorKind": "transient"})
    with pytest.raises(C.ServiceError) as info:
        service.client().calculate("microstrip-impedance", MICROSTRIP)
    assert info.value.status_code == status and len(service.requests) == 1


def test_5xx_is_retried_before_it_is_a_service_error(service):
    waits = []
    service.answer(503, {"detail": "busy", "errorKind": "transient"})
    service.answer(502, HTML_404)
    service.answer(200, calc_response("microstrip-impedance", MICROSTRIP, {"impedance": 50.0}))
    client = service.client(max_retries=2, sleep=waits.append)
    assert client.calculate("microstrip-impedance", MICROSTRIP).values == {"impedance": 50.0}
    assert waits == [1.0, 2.0] and len(service.requests) == 3
    for _ in range(3):
        service.answer(500, {"detail": "fault", "errorKind": "fault"})
    with pytest.raises(C.ServiceError):
        client.calculate("microstrip-impedance", MICROSTRIP)
    assert waits == [1.0, 2.0, 1.0, 2.0]


def test_a_2xx_that_is_not_a_result_is_a_service_error(service):
    service.answer(200, b"<html>maintenance</html>")
    service.answer(200, {"slug": "microstrip-impedance"})
    service.answer(200, {"slug": "microstrip-impedance", "solveFor": "traceWidth"})
    client = service.client()
    with pytest.raises(C.ServiceError):
        client.calculate("microstrip-impedance", MICROSTRIP)
    with pytest.raises(C.ServiceError):
        client.calculate("microstrip-impedance", MICROSTRIP)
    with pytest.raises(C.ServiceError):
        client.solve("microstrip-impedance", MICROSTRIP, "traceWidth", "impedance", 50)


# ── No answer ────────────────────────────────────────────────────────────────


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_no_connection_is_a_transport_error():
    client = C.Client(KEY, base_url=f"http://127.0.0.1:{_closed_port()}/api/py/v1")
    with pytest.raises(C.TransportError) as info:
        client.usage()
    error = info.value
    assert error.status_code is None and not error.certificate and not error.timed_out
    assert "Could not reach rftools.io" in str(error)


def test_a_slow_answer_is_a_transport_error_that_says_it_timed_out(service):
    service.delay = 1.0
    service.answer(200, dict(USAGE))
    with pytest.raises(C.TransportError) as info:
        service.client(timeout=0.2).usage()
    assert info.value.timed_out and "did not answer within 0.2 s" in str(info.value)


def test_a_certificate_that_cannot_be_verified_says_so_plainly():
    failure = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
           "issuer certificate (_ssl.c:1129)")
    failure.verify_message = "unable to get local issuer certificate"

    class Refusing:
        def open(self, request, timeout=None):
            raise urllib.error.URLError(failure)

    client = C.Client(KEY, opener=Refusing())
    with pytest.raises(C.TransportError) as info:
        client.calculate("microstrip-impedance", MICROSTRIP)
    error = info.value
    assert error.certificate and not error.timed_out
    assert str(error).startswith("The TLS certificate of rftools.io could not be verified "
                                 "(unable to get local issuer certificate)")
    assert isinstance(error.__cause__, urllib.error.URLError)


def test_another_tls_failure_is_not_called_a_certificate_problem():
    error = C.transport_error(urllib.error.URLError(ssl.SSLError(1, "handshake failure")))
    assert not error.certificate and "TLS connection to rftools.io failed" in str(error)


# ── TLS: certifi's bundle ────────────────────────────────────────────────────


def test_the_context_verifies_and_trusts_certifis_bundle(monkeypatch):
    import certifi

    loaded = []
    real = ssl.SSLContext.load_verify_locations

    def record(self, cafile=None, capath=None, cadata=None):
        loaded.append(cafile)
        return real(self, cafile, capath, cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", record)
    context = C.ssl_context()
    assert certifi.where() in loaded
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    # Every certificate in certifi's bundle is trusted, whatever the interpreter
    # has of its own (KiCad's bundled Python on macOS has none).
    with open(certifi.where(), encoding="ascii") as fh:
        bundled = fh.read().count("-----BEGIN CERTIFICATE-----")
    assert context.cert_store_stats()["x509_ca"] >= bundled > 100


def test_the_client_uses_that_context_for_https(monkeypatch):
    made = []
    real = C.ssl_context
    monkeypatch.setattr(C, "ssl_context", lambda: made.append(real()) or made[-1])
    client = C.Client(KEY)
    [context] = made
    [handler] = [h for h in client._opener.handlers if isinstance(h, C.urllib.request.HTTPSHandler)]
    assert handler._context is context


def test_without_certifi_it_is_the_default_context(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", None)  # import certifi fails
    context = C.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert C.Client(KEY).base_url == C.DEFAULT_BASE_URL  # and the client still builds


def test_a_broken_certifi_falls_back_with_a_warning(monkeypatch, caplog, tmp_path):
    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "missing.pem"))
    context = C.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert "could not be loaded" in caplog.text


# ── The key stays out of everything ──────────────────────────────────────────


def test_the_key_is_in_no_repr_error_or_log(service, caplog):
    client = service.client()
    no_key_in(repr(client))
    assert "rfc_TESTkey0" in repr(client) and client.key_id == KEY[:12]
    echoed = f"Bad header X-API-Key: {KEY}"  # a proxy that echoes the request
    for status in (400, 401, 402, 403, 404, 422, 429, 500):
        service.answer(status, {"detail": echoed, "errorKind": "invalid_request"})
        with pytest.raises(C.ApiError) as info:
            client.calculate("microstrip-impedance", MICROSTRIP)
        no_key_in(str(info.value), repr(info.value))
    service.answer(200, calc_response("microstrip-impedance", MICROSTRIP, {"impedance": 1.0}))
    result = client.calculate("microstrip-impedance", MICROSTRIP)
    no_key_in(repr(result), json.dumps(result.provenance))
    assert caplog.records  # the client logs each exchange; conftest checks every record
    no_key_in(caplog.text)
