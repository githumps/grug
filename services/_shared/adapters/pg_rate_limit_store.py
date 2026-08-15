"""Postgres-backed atomic rate-limit counters (grug#870).

Shared quota enforcement across ALL grug replicas (grug-webhook and
grug-consumer, 2 pods each - confirm with `kubectl get pods -n grug`) via
the SAME grug_kv single-table store every other idempotency claim in this
codebase uses (see pg_install_store.py's module docstring for the layout).
An in-process token bucket cannot work here: a provider-published cap like
OpenRouter's free-tier ceiling is ACCOUNT-WIDE, not per-process, so N
independently-bucketed replicas would each permit up to the full limit on
their own - Nx the real ceiling - and the overshoot would surface only as
silent 429s from the provider.

Same atomic idiom as claim_delivery / claim_review in pg_install_store.py:
a single `INSERT ... ON CONFLICT DO UPDATE ... WHERE <cond> RETURNING`
statement. Postgres evaluates the `WHERE` clause of the `ON CONFLICT DO
UPDATE` arm with the target row's lock already held, so two replicas
racing the SAME fixed window each get a correct, serialized answer - no
window's row can ever be admitted past its limit even under concurrent
writers on different pods. This is the exact mechanism proven by
test_pg_stores.py's `test_claim_delivery_concurrent_exactly_one_winner`;
this module reuses it for a counter instead of a single win-once flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from adapters import pg_base
from adapters.pg_base import get_pool

log = logging.getLogger("grug.adapters.pg_rate_limit_store")

# Small buffer past a window's natural expiry so a slow reader can never
# race the opportunistic hourly purge (pg_base.maybe_purge_expired) mid-
# window. The purge is what actually reclaims the row afterward; this TTL
# only has to outlive the window it counts, not be exact.
_WINDOW_TTL_SLACK_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """Outcome of one `try_reserve_slot` attempt.

    `minute_count`/`day_count` are the counters AS OF this attempt (best
    effort: the post-increment value when this window's increment ran,
    otherwise a fresh peek) - callers use them for utilisation telemetry,
    never for a second admission decision (the reservation itself already
    made the only decision that matters).
    """

    admitted: bool
    minute_count: int
    day_count: int


def _minute_bucket(ts: datetime) -> int:
    return int(ts.timestamp() // 60)


def _day_bucket(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def _try_increment(pk: str, sk: str, limit: int, ttl: int) -> int | None:
    """Atomically admit ONE more call into the (pk, sk) counter iff
    `count < limit`, returning the POST-increment count, or None if the
    window was already at capacity (this call was NOT admitted - the row
    is left unchanged).

    A window's first-ever call takes the plain INSERT arm unconditionally
    (a row that has never existed obviously has not hit its limit); every
    later call in the SAME window goes through the ON CONFLICT arm, whose
    `WHERE` clause is the entire race guard - see the module docstring.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, jsonb_build_object('count', 1), %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = grug_kv.data || jsonb_build_object(
                        'count',
                        COALESCE((grug_kv.data->>'count')::int, 0) + 1
                    ),
                    ttl = %(ttl)s
                WHERE COALESCE((grug_kv.data->>'count')::int, 0) < %(limit)s
            RETURNING (data->>'count')::int
            """,
            {"pk": pk, "sk": sk, "ttl": ttl, "limit": limit},
        ).fetchone()
    return row[0] if row is not None else None


def _peek(pk: str, sk: str) -> int:
    """Current count for a window WITHOUT incrementing it - telemetry-only
    read, never used for an admit decision, so a stale value under
    concurrent writers is harmless (it only ever labels a rejection that
    already happened)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT data->>'count' FROM grug_kv WHERE pk = %s AND sk = %s",
            (pk, sk),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def try_reserve_slot(
    *,
    name: str,
    minute_limit: int,
    day_limit: int,
    now: datetime | None = None,
) -> ReservationResult:
    """Attempt to admit one call under BOTH the per-minute and per-day
    ceilings for the named account-wide limiter.

    `name` scopes the counters (e.g. "openrouter_free") so a second
    limiter sharing this store never collides with this one's rows.
    Admission requires BOTH windows to have room; the two checks are two
    separate atomic statements (minute, then day), not one joint
    transaction. If the minute check succeeds but the day check then
    rejects, the minute counter is left incremented - deliberately: the
    day cap being the binding constraint means that minute slot was never
    going to be usable by anyone else in THAT window either, so the
    "waste" is not observable as an incorrect admission anywhere.
    """
    ts = now or datetime.now(timezone.utc)
    pk = f"RATELIMIT#{name}"
    minute_sk = f"MIN#{_minute_bucket(ts)}"
    day_sk = f"DAY#{_day_bucket(ts)}"
    minute_ttl = _minute_bucket(ts) * 60 + 60 + _WINDOW_TTL_SLACK_SECONDS
    day_ttl = int(ts.timestamp()) + 2 * 86400

    pg_base.maybe_purge_expired()

    minute_count = _try_increment(pk, minute_sk, minute_limit, minute_ttl)
    if minute_count is None:
        return ReservationResult(
            admitted=False,
            minute_count=_peek(pk, minute_sk),
            day_count=_peek(pk, day_sk),
        )

    day_count = _try_increment(pk, day_sk, day_limit, day_ttl)
    if day_count is None:
        return ReservationResult(
            admitted=False,
            minute_count=minute_count,
            day_count=_peek(pk, day_sk),
        )

    return ReservationResult(
        admitted=True, minute_count=minute_count, day_count=day_count,
    )
