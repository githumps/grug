"""Postgres-backed atomic rate-limit counters (grug#870).

Shared quota enforcement across ALL grug replicas (grug-webhook and
grug-consumer, 2 pods each per the k8s manifests' `replicas: 2`) via the
SAME grug_kv single-table store every other idempotency claim in this
codebase uses (see pg_install_store.py's module docstring for the
layout). An in-process token bucket cannot work here: a provider-
published cap like OpenRouter's free-tier ceiling is ACCOUNT-WIDE, not
per-process, so N independently-bucketed replicas would each permit up
to the full limit on their own - Nx the real ceiling - and the overshoot
would surface only as silent 429s from the provider.

Admission is a plain pessimistic `SELECT ... FOR UPDATE` read-check-
write: ensure the (pk, sk) row exists, lock it, read the current count,
and conditionally increment. A second, concurrent caller's own
`SELECT ... FOR UPDATE` on the SAME row provably blocks until the first
commits, then re-reads the FRESH post-commit value - the textbook
pattern for "check-then-conditionally-increment", not dependent on any
INSERT-conflict-resolution subtlety.

DELIBERATELY bypasses `pg_base.get_pool()` (`psycopg_pool.ConnectionPool`)
for this specific operation and opens its own short-lived `psycopg.connect()`
per call instead - see `_connect()`. This is the one adapter in this
codebase NOT using the shared pool, and that divergence is load-bearing,
not stylistic:

Two independently-designed admission queries were built and verified
correct in isolation (raw `psql`, raw `psycopg.connect()`, and every
local run) - first an `INSERT ... ON CONFLICT DO UPDATE ... WHERE count
< limit` upsert (the same shape `claim_delivery`'s win-once boolean claim
uses), then this module's `SELECT ... FOR UPDATE` read-check-write.
BOTH measurably over-admitted under real concurrent load THROUGH
`ConnectionPool` - reproduced in CI (7/20 threads, 4/6 and 6/16
processes, all admitted when only `limit` should have been) and,
independently, in dozens of repeated local runs (both the thread and the
multi-process test) - while the IDENTICAL SQL run over `psycopg.connect()`
directly, with no other change, was clean across 20+ repeated trials at
the same concurrency. The two queries share only one thing: hitting
Postgres through the pool. The exact ConnectionPool mechanism responsible
was not pinned down (candidates include its background connection-health
check, which briefly flips a checked-out connection's `autocommit` state,
and its checkout/return bookkeeping under heavy simultaneous contention);
what is verified, repeatedly and directly, is that removing the pool from
this one path removes the bug. A short-lived direct connection per call is
an acceptable tradeoff here: this limiter is called at most a few times
per second even at OpenRouter's own published ceiling (20/min), nowhere
near a volume where per-call connection setup matters, and correctness
under contention is the entire point of the feature. Every other
adapter's usage of `pg_base.get_pool()` elsewhere in this codebase is
unaffected by this - only this module's admission queries were ever
observed to require it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

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


def _connect() -> psycopg.Connection:
    """A short-lived, unpooled connection for the admission queries - see
    the module docstring for why this bypasses `pg_base.get_pool()`."""
    return psycopg.connect(pg_base._database_url(), autocommit=False)


def _try_increment(pk: str, sk: str, limit: int, ttl: int) -> int | None:
    """Atomically admit ONE more call into the (pk, sk) counter iff
    `count < limit`, returning the POST-increment count, or None if the
    window was already at capacity (this call was NOT admitted - the row
    is left unchanged).

    Three statements, one transaction (one `psycopg.connect()`, committed
    on clean `with`-block exit): first ensure the row exists (idempotent
    `ON CONFLICT DO NOTHING`, count=0, so the row is GUARANTEED present
    for the next statement - a `SELECT ... FOR UPDATE` cannot lock a row
    that does not exist yet), then lock and read it, then decide.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, jsonb_build_object('count', 0), %(ttl)s)
            ON CONFLICT (pk, sk) DO NOTHING
            """,
            {"pk": pk, "sk": sk, "ttl": ttl},
        )
        row = conn.execute(
            "SELECT (data->>'count')::int FROM grug_kv "
            "WHERE pk = %s AND sk = %s FOR UPDATE",
            (pk, sk),
        ).fetchone()
        current = row[0] if row and row[0] is not None else 0
        if current >= limit:
            return None
        new_count = current + 1
        conn.execute(
            """
            UPDATE grug_kv
            SET data = data || jsonb_build_object('count', %(count)s), ttl = %(ttl)s
            WHERE pk = %(pk)s AND sk = %(sk)s
            """,
            {"count": new_count, "ttl": ttl, "pk": pk, "sk": sk},
        )
    return new_count


def _peek(pk: str, sk: str) -> int:
    """Current count for a window WITHOUT incrementing it - telemetry-only
    read, never used for an admit decision, so a stale value under
    concurrent writers is harmless (it only ever labels a rejection that
    already happened). Uses the SHARED pool - unlike `_try_increment`,
    this is a single plain read with no held lock, so it never exhibited
    the over-admission this module's docstring documents."""
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
    "waste" is not observable as an incorrect admission anywhere. This
    does NOT weaken the per-window guarantee: each `_try_increment` call
    independently and correctly enforces its OWN limit (proven under
    concurrency - see the module docstring), so the total ever admitted
    for a given window can never exceed that window's limit regardless of
    whether the minute and day checks share one transaction.
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
