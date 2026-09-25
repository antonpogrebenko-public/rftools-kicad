"""The rftools.io API behind the result cache, with every refusal mapped by type.

:class:`Api` wraps a client — :class:`rftools_kicad.client.Client`, the
plugin's own standard-library client, or a fake in tests — and runs
:class:`~rftools_kicad.mapping.Computation` objects through it:

* a computation already in the cache is answered from it, with no request;
* ``calculate`` calls ``client.calculate(slug, inputs)``;
* ``solve`` calls ``client.solve(slug, inputs, solve_for, target_output,
  target_value, grid=, range=)``.

Only the slug, the inputs and the solve fields reach the client (spec: "The
plugin SHALL send the service only calculator inputs").

Refusals are classified by the exception's *type* — the client's
``QuotaError`` (402), ``AuthError`` (401/403), ``RateLimitError`` (429),
``RequestError`` (400/422), ``NotFound`` (404), ``ServiceError`` (5xx after the
client's retries) and ``TransportError`` (no answer) — and never by the text of
a message (design Decision 4). Another exception carrying an HTTP
``status_code`` is classified by that status.

A refusal that concerns the account or the connection stops the run: later
computations are answered from the cache where possible and are otherwise
reported with the same refusal, without contacting the service. A 429 is
waited out once (``retry_after``, capped) and retried; a second one stops.
Results computed before a refusal are kept by the caller and stay valid.
"""
from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rftools_kicad.cache import ResultCache
from rftools_kicad.client import (
    ApiError,
    AuthError,
    Client,
    NotFound,
    QuotaError,
    RateLimitError,
    RequestError,
    ServiceError,
    TransportError,
)
from rftools_kicad.mapping import CALCULATE, SOLVE, Computation
from rftools_kicad.settings import KEY_URL

log = logging.getLogger(__name__)

# Refusal kinds.
QUOTA = "quota"  # 402: the month's allowance is spent
AUTH = "auth"  # 401/403: the key is missing, unknown or revoked
RATE_LIMITED = "rate_limited"  # 429 twice
OFFLINE = "offline"  # no connection
SERVICE = "service"  # 5xx after the client's retries
INVALID = "invalid"  # 400/422: the service refused these inputs
NOT_FOUND = "not_found"  # 404

#: Refusals after which no further request is made in this run.
STOPPING = frozenset({QUOTA, AUTH, RATE_LIMITED, OFFLINE, SERVICE})

#: The longest a 429 is waited out before the run stops instead.
MAX_RATE_LIMIT_WAIT = 120.0
#: The wait when a 429 names none.
DEFAULT_RATE_LIMIT_WAIT = 60.0

UPGRADE_URL = "https://rftools.io/pricing"

#: How a user makes KiCad reinstall the plugin's dependencies (KiCad's IPC docs).
RECREATE_ENVIRONMENT = (
    "right-click the plugin in Preferences › Plugins › Action Plugins and choose "
    "Recreate Plugin Environment"
)


@dataclass(frozen=True)
class Refusal:
    """Why a computation has no result, with what the dialog needs to say so."""

    kind: str
    message: str
    status: int | None = None
    reset_at: str | None = None
    retry_after: int | None = None
    upgrade_url: str | None = None
    overage_url: str | None = None
    key_url: str | None = None
    limit: int | None = None
    used: int | None = None
    reason: str | None = None

    @property
    def stops_run(self) -> bool:
        return self.kind in STOPPING


@dataclass(frozen=True)
class Outcome:
    """What one computation came to: a response (fresh or cached) or a refusal."""

    computation: Computation
    response: dict | None = None
    cached: bool = False
    stored_at: float | None = None
    refusal: Refusal | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None


def make_client(api_key: str) -> Client:
    """The plugin's rftools.io client for *api_key*."""
    return Client(api_key)


class Api:
    """Run computations through *client*, answering repeats from *cache*.

    ``key_id`` is the key's public identifier, the only part of the key that
    may appear in a message or a log. ``sleep`` is injectable for tests.
    """

    def __init__(
        self,
        client: Any,
        cache: ResultCache | None = None,
        *,
        key_id: str = "(no key)",
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = MAX_RATE_LIMIT_WAIT,
    ) -> None:
        self._client = client
        self._cache = cache
        self._sleep = sleep
        self._max_wait = max_wait
        self.key_id = key_id
        self.stopped: Refusal | None = None
        self.requests = 0  # metered requests made (usage reads not counted)
        self.last_usage: dict | None = None
        self._waited = False

    # ── The free call ────────────────────────────────────────────────────────

    def usage(self) -> tuple[dict | None, Refusal | None]:
        """The account's allowance, used and remaining (``GET /v1/usage``, never metered)."""
        try:
            raw = self._client.usage()
        except Exception as exc:
            refusal = self._refusal(exc)
            return None, refusal
        usage = normalise_usage(raw)
        self.last_usage = usage
        return usage, None

    # ── Computations ─────────────────────────────────────────────────────────

    def cached(self, computation: Computation) -> dict | None:
        """The cache entry that would answer *computation*, if any."""
        if self._cache is None:
            return None
        return self._cache.get(computation.payload())

    def run(self, computation: Computation) -> Outcome:
        entry = self.cached(computation)
        if entry is not None:
            return Outcome(
                computation, entry["response"], cached=True, stored_at=entry.get("storedAt")
            )
        if self.stopped is not None:
            return Outcome(computation, refusal=self.stopped)
        try:
            response = self._request(computation)
        except Exception as exc:
            refusal = self._refusal(exc, computation)
            if refusal.stops_run:
                self.stopped = refusal
            return Outcome(computation, refusal=refusal)
        stored = None
        if self._cache is not None:
            stored = self._cache.put(computation.payload(), response).get("storedAt")
        return Outcome(computation, response, stored_at=stored)

    def reset(self) -> None:
        """Allow requests again after a stop (the dialog's "retry")."""
        self.stopped = None
        self._waited = False

    # ── Internals ────────────────────────────────────────────────────────────

    def _request(self, computation: Computation) -> dict:
        try:
            return self._send(computation)
        except Exception as exc:
            if _kind_of(exc) != RATE_LIMITED or self._waited:
                raise
            wait = getattr(exc, "retry_after", None)
            wait = DEFAULT_RATE_LIMIT_WAIT if wait is None else float(wait)
            if wait > self._max_wait:
                raise
            self._waited = True
            log.info("rftools.io asked to slow down; waiting %.0f s once before retrying.", wait)
            self._sleep(wait)
            return self._send(computation)

    def _send(self, computation: Computation) -> dict:
        self.requests += 1
        if computation.kind == CALCULATE:
            result = self._client.calculate(computation.slug, computation.input_dict)
            response = normalise_calculation(result)
        elif computation.kind == SOLVE:
            result = self._client.solve(
                computation.slug,
                computation.input_dict,
                computation.solve_for,
                computation.output,
                computation.target_value,
                grid=computation.grid,
                range=list(computation.search_range) if computation.search_range else None,
            )
            response = normalise_solve(result)
        else:
            raise ValueError(f"unknown computation kind {computation.kind!r}")
        usage = getattr(result, "usage", None)
        if usage is not None:
            self.last_usage = normalise_usage(usage)
        return response

    def _refusal(self, exc: BaseException, computation: Computation | None = None) -> Refusal:
        refusal = classify(exc, key_id=self.key_id, computation=computation)
        what = f"{computation.kind} {computation.slug}" if computation else "usage"
        log.warning(
            "rftools.io: %s for key %s was not answered: %s (%s)",
            what, self.key_id, refusal.kind, type(exc).__name__,
        )
        return refusal


# ── Classification ───────────────────────────────────────────────────────────


#: The client's errors, by type (most specific first).
_KINDS = (
    (QuotaError, QUOTA),
    (AuthError, AUTH),
    (RateLimitError, RATE_LIMITED),
    (RequestError, INVALID),
    (NotFound, NOT_FOUND),
    (TransportError, OFFLINE),
    (ServiceError, SERVICE),
)


def _kind_of(exc: BaseException) -> str | None:
    for error_type, kind in _KINDS:
        if isinstance(exc, error_type):
            return kind
    status = getattr(exc, "status_code", None)
    if isinstance(exc, ApiError):
        return _kind_of_status(status) or SERVICE
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
        return OFFLINE
    return _kind_of_status(status)


def _kind_of_status(status: Any) -> str | None:
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    if status == 0:
        return OFFLINE
    if status == 402:
        return QUOTA
    if status in (401, 403):
        return AUTH
    if status == 429:
        return RATE_LIMITED
    if status in (400, 422):
        return INVALID
    if status == 404:
        return NOT_FOUND
    if status >= 500:
        return SERVICE
    return None


def classify(
    exc: BaseException, *, key_id: str = "(no key)", computation: Computation | None = None
) -> Refusal:
    """The :class:`Refusal` for an exception the client raised.

    Raises *exc* again when it is not a refusal at all (a bug is not a refusal).
    """
    kind = _kind_of(exc)
    if kind is None:
        raise exc
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) and status > 0 else None
    retry_after = _int(getattr(exc, "retry_after", None))

    if kind == QUOTA:
        reset_at = getattr(exc, "reset_at", None)
        upgrade = getattr(exc, "upgrade_url", None) or UPGRADE_URL
        overage = getattr(exc, "overage_url", None)
        parts = ["This account's rftools.io API calls for this month are used up."]
        if reset_at:
            parts.append(f"They reset at {reset_at}.")
        parts.append(f"More calls: {upgrade}")
        if overage:
            parts.append(f"or turn on pay-as-you-go calls: {overage}")
        return Refusal(
            QUOTA, " ".join(parts), status=402, reset_at=reset_at, retry_after=retry_after,
            upgrade_url=upgrade, overage_url=overage, limit=_int(getattr(exc, "limit", None)),
            used=_int(getattr(exc, "used", None)), reason=getattr(exc, "reason", None),
        )
    if kind == AUTH:
        key_url = getattr(exc, "key_url", None) or KEY_URL
        if status == 403:
            message = (
                f"rftools.io refused this request for the account of key {key_id}. "
                f"Plans: {UPGRADE_URL}"
            )
        else:
            message = (
                f"rftools.io did not accept the API key {key_id}; it may have been revoked. "
                f"Get a free key at {key_url}"
            )
        return Refusal(AUTH, message, status=status or 401, key_url=key_url)
    if kind == RATE_LIMITED:
        wait = retry_after if retry_after is not None else int(DEFAULT_RATE_LIMIT_WAIT)
        return Refusal(
            RATE_LIMITED,
            f"rftools.io is limiting requests from this account. Wait {wait} s and run again.",
            status=429, retry_after=wait, upgrade_url=getattr(exc, "upgrade_url", None),
            limit=_int(getattr(exc, "limit", None)),
        )
    if kind == OFFLINE:
        if getattr(exc, "certificate", False):  # TLS verification failed: say so plainly
            return Refusal(
                OFFLINE,
                f"{exc} Results already computed or cached are shown. To reinstall the "
                f"plugin's certificates (certifi), {RECREATE_ENVIRONMENT}.",
            )
        return Refusal(
            OFFLINE,
            "Could not reach rftools.io. Results already computed or cached are shown; "
            "check the connection and try again.",
        )
    if kind == SERVICE:
        code = f" (HTTP {status})" if status else ""
        return Refusal(
            SERVICE,
            f"rftools.io could not answer{code}. Results already computed or cached are "
            "shown; try again later.",
            status=status,
        )
    slug = computation.slug if computation else "the calculator"
    if kind == NOT_FOUND:
        return Refusal(NOT_FOUND, f"rftools.io has no calculator {slug}.", status=404)
    # INVALID: the service refused these inputs. Its findings name fields and
    # values only, so they are safe to show.
    failures = getattr(exc, "failures", None) or []
    named = ", ".join(
        str(f.get("param")) for f in failures if isinstance(f, dict) and f.get("param")
    )
    detail = f" ({named})" if named else ""
    return Refusal(
        INVALID, f"rftools.io refused the inputs for {slug}{detail}.", status=status or 400
    )


# ── Normalising results to plain, cacheable dicts ────────────────────────────


def normalise_calculation(result: Any) -> dict:
    """``{slug, values, warnings, errors, provenance}`` from a CalculateResult or a dict."""
    return {
        "slug": _get(result, "slug"),
        "values": dict(_get(result, "values") or {}),
        "warnings": list(_get(result, "warnings") or []),
        "errors": list(_get(result, "errors") or []),
        "provenance": _provenance(_get(result, "provenance")),
    }


def normalise_solve(result: Any) -> dict:
    """The solve response (design Decision 10) from a SolveResult or a dict."""
    target = _get(result, "target")
    if target is not None and not isinstance(target, dict):
        target = {"output": _get(target, "output"), "value": _get(target, "value")}
    inner = _get(result, "result")
    return {
        "slug": _get(result, "slug"),
        "solveFor": _get(result, "solveFor", "solve_for"),
        "target": target,
        "grid": _get(result, "grid"),
        "value": _get(result, "value"),
        "unrounded": _get(result, "unrounded"),
        "reached": bool(_get(result, "reached")),
        "evaluations": _get(result, "evaluations"),
        "warnings": list(_get(result, "warnings") or []),
        "result": normalise_calculation(inner) if inner is not None else None,
    }


def normalise_usage(usage: Any) -> dict:
    """The usage figures from a client Usage or a dict."""
    allowance = _int(_get(usage, "allowance"))
    used = _int(_get(usage, "used"))
    remaining = _get(usage, "remaining")
    if remaining is None and allowance is not None and used is not None:
        remaining = max(0, allowance - used)
    return {
        "tier": _get(usage, "tier"),
        "period": _get(usage, "period"),
        "resetAt": _get(usage, "resetAt", "reset_at"),
        "allowance": allowance,
        "used": used,
        "remaining": _int(remaining),
    }


def _provenance(value: Any) -> dict | None:
    return dict(value) if isinstance(value, Mapping) else None


def _get(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# ── Reading a forward result for display ─────────────────────────────────────


@dataclass(frozen=True)
class Figure:
    """One forward figure as plain data: the number, where it came from, and its range."""

    computation: Computation
    value: float | None = None
    values: dict | None = None
    warnings: tuple = ()
    errors: tuple = ()
    provenance: dict | None = None
    cached: bool = False
    stored_at: float | None = None
    refusal: Refusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and self.value is not None

    @property
    def engine_version(self) -> str | None:
        return (self.provenance or {}).get("version")

    @property
    def formula_ref(self) -> str | None:
        return (self.provenance or {}).get("formulaRef")

    @property
    def outside(self) -> tuple:
        """Why the inputs lie outside the calculator's range, each naming the bound crossed."""
        return range_notes(self.provenance)


def read_figure(outcome: Outcome) -> Figure:
    if not outcome.ok:
        return Figure(outcome.computation, refusal=outcome.refusal, cached=outcome.cached)
    response = outcome.response
    values = response.get("values") or {}
    return Figure(
        computation=outcome.computation,
        value=values.get(outcome.computation.output),
        values=dict(values),
        warnings=tuple(response.get("warnings") or ()),
        errors=tuple(response.get("errors") or ()),
        provenance=response.get("provenance"),
        cached=outcome.cached,
        stored_at=outcome.stored_at,
    )


def range_notes(provenance: dict | None) -> tuple:
    """The provenance envelope's ``validRange`` as sentences naming each bound crossed."""
    valid = (provenance or {}).get("validRange") or {}
    inputs = (provenance or {}).get("inputs") or {}
    bounds = valid.get("bounds") or {}
    notes = []
    for key in valid.get("outside") or ():
        bound = bounds.get(key) or {}
        low, high, unit = bound.get("min"), bound.get("max"), bound.get("unit") or ""
        unit = f" {unit}" if unit else ""
        value = inputs.get(key)
        shown = f" {value:g}{unit}" if isinstance(value, (int, float)) else ""
        if low is not None and isinstance(value, (int, float)) and value < low:
            crossed = f"below the minimum {low:g}{unit}"
        elif high is not None and isinstance(value, (int, float)) and value > high:
            crossed = f"above the maximum {high:g}{unit}"
        else:
            crossed = "outside the stated range"
        notes.append(f"{key}{shown} is {crossed}")
    model = valid.get("model") or {}
    if model.get("inside") is False:
        notes.append(
            "outside the fitted model's validated range "
            f"({model.get('description') or 'see the formula reference'})"
        )
    return tuple(notes)
