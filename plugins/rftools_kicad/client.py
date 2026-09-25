"""A small client for the rftools.io API, on the standard library alone.

KiCad runs the plugin on its own Python: 3.9 on macOS, 3.11 on Windows, the
system ``python3`` on Linux (design Decision 7a). The ``rftools-io`` SDK needs
3.12, so the plugin makes its three calls itself, with ``urllib``:

    Client(key).calculate(slug, inputs)            POST /calculate
    Client(key).solve(slug, inputs, solve_for,     POST /calculate/solve
                      target_output, target_value,
                      grid=None, range=None)
    Client(key).usage()                            GET  /usage (never metered)

Each request carries the key in ``X-API-Key`` (never sent on to a redirect)
and ``User-Agent: rftools-kicad/<version>``. A body holds only the fields the
caller named: ``grid`` and ``range`` are sent only when given. Results are
plain dataclasses (:class:`CalculateResult`, :class:`SolveResult`,
:class:`Usage`); a metered result carries the account's usage from the
response's ``X-Usage-*`` headers.

**TLS.** KiCad's bundled Python on macOS has no CA certificates of its own, so
:func:`ssl_context` adds certifi's bundle (a declared requirement) to what the
default context trusts (the system's certificates on Windows and Linux, and
``SSL_CERT_FILE``). Without certifi it is the default context alone.

**Refusals** are typed by HTTP status alone, never by the text of a message
(design Decision 4):

    401, 403     AuthError       key_url (the body's keyUrl, when it names one)
    402          QuotaError      reason, limit, used, reset_at, upgrade_url,
                                 overage_url, retry_after (Retry-After)
    404          NotFound
    400, 422     RequestError    param, failures ({param, reason} per field named)
    429          RateLimitError  retry_after, limit, window_seconds, upgrade_url
    5xx, other   ServiceError    500/502/503/504 after ``max_retries`` retries
    no answer    TransportError  certificate=True when TLS verification failed,
                                 timed_out=True when the time ran out

Every error has ``status_code`` (None when there was no response) and
``error_kind`` (the body's ``errorKind``). A body that is not JSON — CloudFront
answers some 404s with an HTML page — reads as an empty one. The key never
appears in an error, a log record or a repr: only its public identifier.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rftools_kicad import __version__
from rftools_kicad.settings import public_id, redact

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://rftools.io/api/py/v1"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = f"rftools-kicad/{__version__}"

#: Answers retried (with a 1 s, then 2 s pause) before they become a ServiceError.
RETRY_STATUSES = frozenset({500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 2

#: Hosts a key may be sent to over plain HTTP: a test server on this machine.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Usage:
    """The account's usage: from ``GET /usage``, or a metered response's headers.

    ``tier`` and ``period`` come only from ``GET /usage``.
    """

    allowance: int | None = None
    used: int | None = None
    remaining: int | None = None
    reset_at: str | None = None
    tier: str | None = None
    period: str | None = None


@dataclass(frozen=True)
class CalculateResult:
    """``POST /calculate``: what the calculator returned, and the provenance envelope."""

    slug: str | None
    values: dict
    warnings: list
    errors: list
    provenance: dict | None
    usage: Usage | None = None


@dataclass(frozen=True)
class SolveResult:
    """``POST /calculate/solve`` (design Decision 10).

    ``value`` is on the grid when one was sent, ``unrounded`` the search's own
    solution; ``result`` is the forward call at exactly ``value``.
    """

    slug: str | None
    solve_for: str | None
    target: dict | None
    grid: float | None
    value: float
    unrounded: float | None
    reached: bool
    evaluations: int | None
    warnings: list
    result: CalculateResult | None
    usage: Usage | None = None


# ── Errors ───────────────────────────────────────────────────────────────────


class ApiError(Exception):
    """A call rftools.io did not answer with a result."""

    def __init__(
        self, message: str, *, status_code: int | None = None, error_kind: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_kind = error_kind


class AuthError(ApiError):
    """401: no key, or an unknown or revoked one; 403: the account may not do this."""

    def __init__(self, message: str, *, key_url: str | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.key_url = key_url


class QuotaError(ApiError):
    """402: the account's monthly allowance (or its overage cap) is spent."""

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        limit: int | None = None,
        used: int | None = None,
        reset_at: str | None = None,
        upgrade_url: str | None = None,
        overage_url: str | None = None,
        retry_after: int | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.reason = reason
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        self.upgrade_url = upgrade_url
        self.overage_url = overage_url
        self.retry_after = retry_after


class RateLimitError(ApiError):
    """429: more requests in the window than the key may make."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        limit: int | None = None,
        window_seconds: int | None = None,
        upgrade_url: str | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after
        self.limit = limit
        self.window_seconds = window_seconds
        self.upgrade_url = upgrade_url


class NotFound(ApiError):
    """404: no such calculator (or no such route)."""


class RequestError(ApiError):
    """400/422: the service refused the request; ``failures`` name the fields."""

    def __init__(
        self, message: str, *, param: str | None = None, failures: list | None = None, **kw: Any
    ) -> None:
        super().__init__(message, **kw)
        self.param = param
        self.failures = list(failures or [])


class ServiceError(ApiError):
    """A 5xx (after the retries), another unexpected status, or an unreadable result."""


class TransportError(ApiError):
    """No answer: no connection, a timeout, or a TLS failure."""

    def __init__(
        self, message: str, *, certificate: bool = False, timed_out: bool = False, **kw: Any
    ) -> None:
        super().__init__(message, **kw)
        self.certificate = certificate
        self.timed_out = timed_out


# ── TLS ──────────────────────────────────────────────────────────────────────


def ssl_context() -> ssl.SSLContext:
    """A verifying context that trusts certifi's bundle besides the default locations."""
    context = ssl.create_default_context()
    try:
        import certifi

        cafile = certifi.where()
    except Exception:  # not installed, or broken: the default context is all there is
        log.debug("certifi is not available; TLS uses the system's certificates only.")
        return context
    try:
        context.load_verify_locations(cafile=cafile)
    except (OSError, ssl.SSLError) as exc:
        log.warning(
            "certifi's CA bundle could not be loaded (%s); TLS uses the system's certificates "
            "only.", type(exc).__name__,
        )
    return context


# ── The client ───────────────────────────────────────────────────────────────


class Client:
    """The rftools.io API for one key. ``opener`` and ``sleep`` are injectable for tests."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        opener: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self.base_url = _checked_base_url(base_url)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep
        if opener is None:
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context()))
        self._opener = opener
        #: The account's usage as the latest metered response reported it.
        self.last_usage: Usage | None = None

    @property
    def key_id(self) -> str:
        return public_id(self._api_key)

    def __repr__(self) -> str:  # never the key
        return f"Client(key={self.key_id!r}, base_url={self.base_url!r})"

    # ── The calls ────────────────────────────────────────────────────────────

    def calculate(self, slug: str, inputs: Mapping[str, float]) -> CalculateResult:
        """One forward computation (one metered call)."""
        data, usage = self._request("POST", "/calculate", {"slug": slug, "inputs": dict(inputs)})
        return _calculate_result(data, usage)

    def solve(
        self,
        slug: str,
        inputs: Mapping[str, float],
        solve_for: str,
        target_output: str,
        target_value: float,
        grid: float | None = None,
        range: Sequence[float] | None = None,  # noqa: A002 - the API's field name
    ) -> SolveResult:
        """*solve_for*'s value at which *target_output* is *target_value* (one metered call)."""
        body: dict = {
            "slug": slug,
            "inputs": dict(inputs),
            "solveFor": solve_for,
            "target": {"output": target_output, "value": target_value},
        }
        if grid is not None:
            body["grid"] = grid
        if range is not None:
            body["range"] = list(range)
        data, usage = self._request("POST", "/calculate/solve", body)
        return _solve_result(data, usage)

    def usage(self) -> Usage:
        """The account's allowance, used and remaining this month (never metered)."""
        data, _ = self._request("GET", "/usage", None)
        return Usage(
            allowance=_int(data.get("allowance")),
            used=_int(data.get("used")),
            remaining=_int(data.get("remaining")),
            reset_at=_str(data.get("resetAt")),
            tier=_str(data.get("tier")),
            period=_str(data.get("period")),
        )

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict | None) -> tuple:
        """``(JSON object, usage from the headers)`` for a 2xx, else the typed error."""
        attempt = 0
        while True:
            status, headers, raw = self._exchange(method, path, body)
            usage = usage_from_headers(headers)
            if usage is not None:
                self.last_usage = usage
            if 200 <= status < 300:
                data = _json_object(raw)
                if data is None:
                    raise ServiceError(
                        f"rftools.io answered {method} {path} with HTTP {status} but no result."
                    )
                return data, usage
            if status in RETRY_STATUSES and attempt < self.max_retries:
                wait = 2.0 ** attempt
                attempt += 1
                log.info("rftools.io answered HTTP %s; retrying in %.0f s.", status, wait)
                self._sleep(wait)
                continue
            raise error_for_response(status, headers, raw, key=self._api_key)

    def _exchange(self, method: str, path: str, body: dict | None) -> tuple:
        """``(status, headers, body bytes)``; TransportError when nothing came back."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self._api_key:
            # Unredirected: a redirect to another host never receives the key.
            request.add_unredirected_header("X-API-Key", self._api_key)
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as refused:  # a non-2xx answer is still an answer
                status, headers, raw = refused.code, refused.headers, _read_quietly(refused)
            else:
                try:
                    status, headers, raw = response.getcode(), response.headers, response.read()
                finally:
                    response.close()
        except (OSError, http.client.HTTPException) as exc:  # URLError is an OSError
            error = transport_error(exc, self.timeout)
            log.debug("rftools.io %s %s: no answer (%s)", method, path, type(exc).__name__)
            raise error from exc
        log.debug("rftools.io %s %s -> HTTP %s", method, path, status)
        return int(status), headers, raw


def _checked_base_url(base_url: str) -> str:
    """*base_url* without a trailing slash; HTTPS unless it is this machine."""
    url = (base_url or "").rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return url
    raise ValueError(f"the API base URL must be https:// (got {url!r})")


def _read_quietly(response: Any) -> bytes:
    try:
        return response.read() or b""
    except Exception:  # a refusal whose body cannot be read is still that refusal
        return b""
    finally:
        try:
            response.close()
        except Exception:
            pass


# ── Reading answers ──────────────────────────────────────────────────────────


def error_for_response(
    status: int, headers: Any, raw: bytes | None, *, key: str | None = None
) -> ApiError:
    """The typed error for a non-2xx answer, by *status* alone (never the message text)."""
    body = _json_object(raw) or {}
    detail = body.get("detail")
    text = detail.strip() if isinstance(detail, str) else ""
    common = {"status_code": status, "error_kind": _str(body.get("errorKind"))}
    retry_after = _retry_after(headers)

    def message(default: str) -> str:
        said = f"rftools.io answered HTTP {status}: {text or default}"
        return redact(said, (key,) if key else ())

    if status in (401, 403):
        default = "the API key was not accepted" if status == 401 else "the request is not allowed"
        return AuthError(message(default), key_url=_str(body.get("keyUrl")), **common)
    if status == 402:
        return QuotaError(
            message("this account's API calls for the month are used"),
            reason=_str(body.get("reason")),
            limit=_int(body.get("limit")),
            used=_int(body.get("used")),
            reset_at=_str(body.get("resetAt")),
            upgrade_url=_str(body.get("upgradeUrl")),
            overage_url=_str(body.get("overageUrl")),
            retry_after=retry_after,
            **common,
        )
    if status == 429:
        return RateLimitError(
            message("too many requests"),
            retry_after=retry_after,
            limit=_int(body.get("limit")),
            window_seconds=_int(body.get("windowSeconds")),
            upgrade_url=_str(body.get("upgradeUrl")),
            **common,
        )
    if status == 404:
        return NotFound(message("not found"), **common)
    if status in (400, 422):
        failures = _failures(body)
        if not text and failures:
            text = "; ".join(
                f"{f['param']}: {f['reason']}" if f.get("reason") else str(f["param"])
                for f in failures
            )
        return RequestError(
            message("the request was refused"), param=_str(body.get("param")),
            failures=failures, **common,
        )
    return ServiceError(message("the service could not answer"), **common)


def transport_error(exc: BaseException, timeout: float | None = None) -> TransportError:
    """The TransportError for an exception raised before any answer arrived."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        why = getattr(reason, "verify_message", None) or "certificate verify failed"
        return TransportError(
            f"The TLS certificate of rftools.io could not be verified ({why}), so nothing was "
            "sent. The plugin checks it against certifi's certificates, where installed, and "
            "the ones this Python trusts.",
            certificate=True,
        )
    if isinstance(reason, (socket.timeout, TimeoutError)):
        within = f" within {timeout:g} s" if timeout else ""
        return TransportError(f"rftools.io did not answer{within}.", timed_out=True)
    if isinstance(reason, ssl.SSLError):
        return TransportError(f"The TLS connection to rftools.io failed ({type(reason).__name__}).")
    return TransportError(f"Could not reach rftools.io ({type(reason).__name__}: {reason}).")


def usage_from_headers(headers: Any) -> Usage | None:
    """The usage a metered response reports in ``X-Usage-*``, or None without them."""
    fields = _lower(headers)
    allowance = _int_text(fields.get("x-usage-allowance"))
    used = _int_text(fields.get("x-usage-used"))
    reset_at = (fields.get("x-usage-reset") or "").strip()
    if allowance is None or used is None or not reset_at:
        return None
    return Usage(allowance=allowance, used=used, remaining=max(0, allowance - used),
                 reset_at=reset_at)


def _calculate_result(data: Mapping[str, Any], usage: Usage | None) -> CalculateResult:
    values = data.get("values")
    if not isinstance(values, dict):
        raise ServiceError("rftools.io answered without the calculator's values.")
    provenance = data.get("provenance")
    return CalculateResult(
        slug=_str(data.get("slug")),
        values=dict(values),
        warnings=_list(data.get("warnings")),
        errors=_list(data.get("errors")),
        provenance=dict(provenance) if isinstance(provenance, dict) else None,
        usage=usage,
    )


def _solve_result(data: Mapping[str, Any], usage: Usage | None) -> SolveResult:
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServiceError("rftools.io answered the solve without a value.")
    target = data.get("target")
    inner = data.get("result")
    return SolveResult(
        slug=_str(data.get("slug")),
        solve_for=_str(data.get("solveFor")),
        target=dict(target) if isinstance(target, dict) else None,
        grid=_number(data.get("grid")),
        value=value,
        unrounded=_number(data.get("unrounded")),
        reached=data.get("reached") is True,
        evaluations=_int(data.get("evaluations")),
        warnings=_list(data.get("warnings")),
        result=_calculate_result(inner, None) if isinstance(inner, dict) else None,
        usage=usage,
    )


def _failures(body: Mapping[str, Any]) -> list:
    """``{param, reason}`` for every field the refusal names, in the order it names them."""
    found: list = []
    detail = body.get("detail")
    if isinstance(detail, list):  # FastAPI's 422: [{loc, msg, type}]
        for item in detail:
            if isinstance(item, dict):
                found.append({"param": _param_of(item.get("loc")), "reason": _str(item.get("msg"))})
    reason = detail.strip() if isinstance(detail, str) else None
    param = _str(body.get("param"))  # the solve call's refusals name one field
    if param:
        found.append({"param": param, "reason": reason})
    for name in body.get("inputs") or ():  # a non-finite or over-limit input
        if isinstance(name, str) and name:
            found.append({"param": name, "reason": reason})
    unique, seen = [], set()
    for failure in found:
        if failure["param"] and failure["param"] not in seen:
            seen.add(failure["param"])
            unique.append(failure)
    return unique


def _param_of(loc: Any) -> str | None:
    if not isinstance(loc, (list, tuple)):
        return None
    parts = [str(p) for p in loc if isinstance(p, (str, int)) and not isinstance(p, bool)]
    if parts and parts[0] in ("body", "query", "header", "path"):
        parts = parts[1:]
    return ".".join(parts) or None


def _json_object(raw: bytes | None) -> dict | None:
    """The body as a JSON object, or None (empty, HTML, not an object)."""
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lower(headers: Any) -> dict:
    if headers is None:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {str(k).lower(): str(v) for k, v in items}


def _retry_after(headers: Any) -> int | None:
    """``Retry-After`` in seconds; an HTTP date or anything else reads as None."""
    seconds = _int_text(_lower(headers).get("retry-after"))
    return seconds if seconds is not None and seconds >= 0 else None


def _int_text(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value is not None else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []
