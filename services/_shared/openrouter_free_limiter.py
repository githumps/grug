"""Cross-process rate limiter for OpenRouter free-tier (`:free`) calls
(grug#870, epic #869).

OpenRouter publishes ACCOUNT-WIDE caps for `:free`-suffixed model ids
(openrouter.ai/docs/api-reference/limits): 20 requests/minute and, with
purchased credits, 1000 requests/day. Grug runs `grug-webhook` and
`grug-consumer` as separate Deployments, 2 pods each - any of the 4 can
dispatch an OpenRouter call from the same `llm_client.py`. An in-process
limiter has no way to see what the other 3 replicas are doing, so it would
let each admit up to the full 20/min independently: 4x the real ceiling,
discovered only as silent 429s from OpenRouter. The actual counting lives
in `adapters.pg_rate_limit_store` (the shared Postgres `grug_kv` table
every other cross-replica claim in this codebase already uses); this
module owns the POLICY around that counter - which calls it gates, the
bounded queue wait, config, and telemetry.

Only calls whose model id ends in `:free` are gated. Cave (owned hardware)
and Poolside never carry that suffix and are never metered by this module
- `is_free_tier_model` is the entire cost they pay (one string compare).

A call at the ceiling QUEUES: it polls the shared counter for a bounded
wall-clock window before giving up. Giving up returns
`RateLimitOutcome(admitted=False, ...)`; `llm_client._call_backend` turns
that into `RateLimitTimeoutError`, an `httpx.RequestError` subclass every
existing `_call_backend` caller already catches alongside real transport
failures - the SAME `all_failed` / `"partial review: "` degradation
vocabulary (`_partial_review_reason`, `persona.py`'s `_derive_conclusion`)
already used for a Cave/SaaS outage now also covers "the queue never
opened a slot". No new `degraded_reason` is introduced.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.openrouter_free_limiter")

_LIMITER_NAME = "openrouter_free"

# OpenRouter's published, account-wide (NOT per-model, NOT per-process)
# free-tier ceiling, verified against openrouter.ai/docs/api-reference/
# limits 2026-08-15. 1000/day (not the unfunded 50/day default) because
# the operator has purchased credits - see that page's "if you've
# purchased credits" note.
_DEFAULT_RPM = 20
_DEFAULT_RPD = 1000
_DEFAULT_MAX_QUEUE_WAIT_SECONDS = 30.0
_MIN_RPM, _MAX_RPM = 1, 600
_MIN_RPD, _MAX_RPD = 1, 100_000
_MIN_WAIT_SECONDS, _MAX_WAIT_SECONDS = 0.0, 300.0

# How often a queued call re-checks the shared counter. Coarse enough to
# keep Postgres load trivial even under a full 4-replica pile-up, fine
# enough that a slot freed by the minute-window rollover is picked up
# quickly relative to the default 30s bound.
_POLL_INTERVAL_SECONDS = 1.0


def is_free_tier_model(model: str) -> bool:
    """OpenRouter's convention: a model id ending in `:free` is served
    from the shared, rate-limited free pool. Grug's currently-configured
    OpenRouter models (`anthropic/claude-haiku-4.5`,
    `anthropic/claude-opus-4.7`) are not `:free` variants, so this gate is
    a no-op for them today; `GRUG_OPENROUTER_REVIEW_MODEL` is already
    env-overridable, so a `:free` model is one config change away."""
    return model.strip().endswith(":free")


def _parse_positive_int(env_var: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(env_var, str(default))
    try:
        value = int(raw)
    except ValueError:
        log.warning("openrouter_free_config_invalid", extra={"var": env_var, "value": raw})
        return default
    return min(hi, max(lo, value))


def _parse_nonneg_float(env_var: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(env_var, str(default))
    try:
        value = float(raw)
    except ValueError:
        log.warning("openrouter_free_config_invalid", extra={"var": env_var, "value": raw})
        return default
    return min(hi, max(lo, value))


def _requests_per_minute() -> int:
    return _parse_positive_int("GRUG_OPENROUTER_FREE_RPM", _DEFAULT_RPM, _MIN_RPM, _MAX_RPM)


def _requests_per_day() -> int:
    return _parse_positive_int("GRUG_OPENROUTER_FREE_RPD", _DEFAULT_RPD, _MIN_RPD, _MAX_RPD)


def _max_queue_wait_seconds() -> float:
    return _parse_nonneg_float(
        "GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S",
        _DEFAULT_MAX_QUEUE_WAIT_SECONDS,
        _MIN_WAIT_SECONDS,
        _MAX_WAIT_SECONDS,
    )


class RateLimitTimeoutError(httpx.RequestError):
    """The shared OpenRouter free-tier limiter could not admit this call
    within its bounded queue wait (or the coordinating store errored - see
    `acquire_free_tier_slot`, which fails CLOSED on a store error rather
    than admitting unmetered).

    Subclasses `httpx.RequestError` so every existing `_call_backend`
    caller's `except (_BackendConfigError, httpx.RequestError,
    httpx.TimeoutException)` clause already treats this exactly like any
    other backend transport failure - no call site needs to change, and
    the failure flows into the SAME `all_failed` / `"partial review: "`
    degradation vocabulary a real outage already uses.
    """


@dataclass(frozen=True, slots=True)
class RateLimitOutcome:
    admitted: bool
    waited_seconds: float
    queued: bool
    minute_count: int
    day_count: int
    minute_limit: int
    day_limit: int


# Test hook - replaced with a no-op in unit tests to avoid real sleeps,
# same pattern as llm_client._RETRY_SLEEP.
def _QUEUE_SLEEP(seconds: float) -> None:
    time.sleep(seconds)


def _emit_telemetry(*, model: str, outcome: RateLimitOutcome) -> None:
    """One structured log line (the operator-grep / DD-log-monitor token)
    plus the DogStatsD gauges a dashboard graphs directly - same dual
    emission `observability.emit_gauge`'s other callers use (e.g.
    `dispatch.py`'s `grug.sqs.messages_visible`).

    Every gauge is emitted on EVERY call, admitted or not (0.0 for the
    outcomes that didn't happen) - "are we hitting the cap" must never
    depend on correlating separate admitted/queued/rejected series that
    can each independently go quiet; same "always report, never skip"
    lesson ADR-0022 documents for the enforcement gauge. Utilisation is
    reported every time for the same reason: a dashboard tracking
    `minute_utilization` must see it update on every admitted call, not
    only when something is queued or rejected.
    """
    log_token = (
        "openrouter_free_rejected" if not outcome.admitted
        else "openrouter_free_queued" if outcome.queued
        else "openrouter_free_admitted"
    )
    log.log(
        logging.WARNING if not outcome.admitted else logging.INFO,
        log_token,
        extra={
            "model": model,
            "waited_seconds": round(outcome.waited_seconds, 3),
            "queued": outcome.queued,
            "minute_count": outcome.minute_count,
            "minute_limit": outcome.minute_limit,
            "day_count": outcome.day_count,
            "day_limit": outcome.day_limit,
        },
    )
    try:
        from observability import emit_gauge  # type: ignore  # late: webhook-image only
    except Exception:  # noqa: BLE001 - telemetry must never break the call it describes
        return
    tags = {"model": model}
    emit_gauge("grug.openrouter_free.admitted", 1.0 if outcome.admitted else 0.0, tags)
    emit_gauge("grug.openrouter_free.rejected", 0.0 if outcome.admitted else 1.0, tags)
    emit_gauge("grug.openrouter_free.queued", 1.0 if outcome.queued else 0.0, tags)
    emit_gauge("grug.openrouter_free.queue_wait_ms", outcome.waited_seconds * 1000, tags)
    emit_gauge(
        "grug.openrouter_free.minute_utilization",
        outcome.minute_count / outcome.minute_limit if outcome.minute_limit else 0.0,
        tags,
    )
    emit_gauge(
        "grug.openrouter_free.day_utilization",
        outcome.day_count / outcome.day_limit if outcome.day_limit else 0.0,
        tags,
    )


def acquire_free_tier_slot(
    model: str, *, cancel_event: threading.Event | None = None,
) -> RateLimitOutcome:
    """Block (bounded) until a slot opens under BOTH the OpenRouter
    free-tier per-minute and per-day ceilings, or the queue-wait bound
    elapses. Callers MUST check `.admitted` - False means "treat this
    exactly like a backend failure", never like a call that quietly
    didn't happen.

    The store round-trip is attempted on every poll tick; a store error
    (Postgres unreachable) is NOT retried for the rest of the bound -
    hammering a definitively erroring store adds nothing, and admitting
    unmetered on a store error would silently recreate the exact
    in-process-bucket bug this limiter exists to prevent (no coordinator
    reachable == no cross-replica guarantee), so it fails CLOSED instead.
    """
    from adapters.pg_rate_limit_store import (
        try_reserve_slot,  # late: pg-backed, webhook-image only
    )

    rpm = _requests_per_minute()
    rpd = _requests_per_day()
    max_wait = _max_queue_wait_seconds()
    max_attempts = max(1, math.ceil(max_wait / _POLL_INTERVAL_SECONDS)) if max_wait > 0 else 1
    start = time.monotonic()
    minute_count = 0
    day_count = 0

    for attempt in range(1, max_attempts + 1):
        try:
            result = try_reserve_slot(name=_LIMITER_NAME, minute_limit=rpm, day_limit=rpd)
        except Exception as e:  # noqa: BLE001 - fail CLOSED, see docstring
            log.warning(
                "openrouter_free_limiter_store_error",
                extra={"model": model, "kind": type(e).__name__},
            )
            break
        minute_count, day_count = result.minute_count, result.day_count
        if result.admitted:
            outcome = RateLimitOutcome(
                admitted=True,
                waited_seconds=time.monotonic() - start,
                queued=attempt > 1,
                minute_count=minute_count,
                day_count=day_count,
                minute_limit=rpm,
                day_limit=rpd,
            )
            _emit_telemetry(model=model, outcome=outcome)
            return outcome
        if attempt == max_attempts or (cancel_event is not None and cancel_event.is_set()):
            break
        _QUEUE_SLEEP(_POLL_INTERVAL_SECONDS)

    # `queued` here means "waited through at least one poll interval before
    # giving up" - true whenever more than the first attempt ran, false for
    # an immediate rejection (store error, or cancelled, on attempt 1).
    outcome = RateLimitOutcome(
        admitted=False,
        waited_seconds=time.monotonic() - start,
        queued=attempt > 1,
        minute_count=minute_count,
        day_count=day_count,
        minute_limit=rpm,
        day_limit=rpd,
    )
    _emit_telemetry(model=model, outcome=outcome)
    return outcome
