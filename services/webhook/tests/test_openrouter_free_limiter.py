"""Tests for the OpenRouter free-tier rate limiter (grug#870, epic #869).

Store-backed multi-process concurrency proof lives in
services/api/tests/test_pg_stores.py (real Postgres, per the repo's own
convention - see that file's module docstring: "the round-trip against a
real store is exercised by the GRUG_TEST_DATABASE_URL suite in CI", the
SAME split test_voice_pack.py documents for pg_install_store). This file
covers the POLICY layer around that store call: free-tier detection,
config parsing, the bounded queue-wait loop, fail-closed-on-store-error,
and telemetry - all with the store mocked, matching how this service's
other suites (test_voice_pack.py, test_async_dispatch.py) test against
adapters.pg_install_store.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import openrouter_free_limiter as ofl
import pytest
from adapters.pg_rate_limit_store import ReservationResult

# --- is_free_tier_model -----------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("meta-llama/llama-3.3-70b-instruct:free", True),
        ("qwen/qwen3-coder:free", True),
        ("anthropic/claude-haiku-4.5", False),
        ("anthropic/claude-opus-4.7", False),
        ("poolside/laguna-m.1", False),
        # ":free" must be the SUFFIX, not merely present somewhere.
        ("meta-llama/free:not-actually-free-model", False),
        # Trailing whitespace from a fat-fingered env var must not defeat
        # detection.
        ("meta-llama/llama-3.3-70b-instruct:free  ", True),
    ],
)
def test_is_free_tier_model(model: str, expected: bool) -> None:
    assert ofl.is_free_tier_model(model) is expected


# --- config parsing ----------------------------------------------------------


def test_config_defaults_match_openrouter_published_limits(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_OPENROUTER_FREE_RPM", raising=False)
    monkeypatch.delenv("GRUG_OPENROUTER_FREE_RPD", raising=False)
    monkeypatch.delenv("GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S", raising=False)
    assert ofl._requests_per_minute() == 20
    assert ofl._requests_per_day() == 1000
    assert ofl._max_queue_wait_seconds() == 30.0


def test_config_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPM", "5")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPD", "50")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S", "12.5")
    assert ofl._requests_per_minute() == 5
    assert ofl._requests_per_day() == 50
    assert ofl._max_queue_wait_seconds() == 12.5


def test_config_invalid_values_fall_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPM", "not-a-number")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPD", "also-bad")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S", "nope")
    assert ofl._requests_per_minute() == ofl._DEFAULT_RPM
    assert ofl._requests_per_day() == ofl._DEFAULT_RPD
    assert ofl._max_queue_wait_seconds() == ofl._DEFAULT_MAX_QUEUE_WAIT_SECONDS


def test_config_clamps_to_sane_bounds(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPM", "0")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPD", "999999999")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S", "-5")
    assert ofl._requests_per_minute() == ofl._MIN_RPM
    assert ofl._requests_per_day() == ofl._MAX_RPD
    assert ofl._max_queue_wait_seconds() == ofl._MIN_WAIT_SECONDS


# --- acquire_free_tier_slot: queueing / bounded wait / telemetry ------------


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Same test-hook pattern as llm_client._RETRY_SLEEP: replace the
    limiter's poll sleep with a no-op so queueing tests run instantly."""
    monkeypatch.setattr(ofl, "_QUEUE_SLEEP", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fast_bound(monkeypatch):
    """3 poll attempts (not the 30s default) so a fully-exhausted queue
    test doesn't need 30 mocked reservation calls."""
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S", "3")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPM", "20")
    monkeypatch.setenv("GRUG_OPENROUTER_FREE_RPD", "1000")


def _result(admitted: bool, minute=1, day=1) -> ReservationResult:
    return ReservationResult(admitted=admitted, minute_count=minute, day_count=day)


def test_admits_immediately_when_store_has_room(monkeypatch) -> None:
    with patch(
        "adapters.pg_rate_limit_store.try_reserve_slot",
        return_value=_result(True, minute=7, day=42),
    ) as mock_reserve:
        outcome = ofl.acquire_free_tier_slot("m:free")
    assert outcome.admitted is True
    assert outcome.queued is False
    assert outcome.minute_count == 7
    assert outcome.day_count == 42
    mock_reserve.assert_called_once()


def test_queues_then_admits_when_store_frees_up(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(ofl, "_QUEUE_SLEEP", sleeps.append)
    with patch(
        "adapters.pg_rate_limit_store.try_reserve_slot",
        side_effect=[_result(False, 20, 5), _result(False, 20, 5), _result(True, 3, 6)],
    ):
        outcome = ofl.acquire_free_tier_slot("m:free")
    assert outcome.admitted is True
    assert outcome.queued is True
    assert outcome.minute_count == 3
    assert len(sleeps) == 2  # slept between attempt 1->2 and 2->3, not after admission


def test_rejected_after_bound_exhausted(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(ofl, "_QUEUE_SLEEP", sleeps.append)
    with patch(
        "adapters.pg_rate_limit_store.try_reserve_slot",
        return_value=_result(False, 20, 5),
    ) as mock_reserve:
        outcome = ofl.acquire_free_tier_slot("m:free")
    assert outcome.admitted is False
    assert outcome.queued is True
    # GRUG_OPENROUTER_FREE_MAX_QUEUE_WAIT_S=3 / 1s poll interval == 3 attempts.
    assert mock_reserve.call_count == 3
    assert len(sleeps) == 2  # sleeps BETWEEN attempts, never after the last


def test_store_error_fails_closed_without_retrying(monkeypatch, caplog) -> None:
    """A down/misconfigured Postgres must reject immediately, not admit
    unmetered - admitting on a coordinator outage would silently recreate
    the exact in-process-bucket bug this limiter exists to prevent."""
    sleeps: list[float] = []
    monkeypatch.setattr(ofl, "_QUEUE_SLEEP", sleeps.append)
    with patch(
        "adapters.pg_rate_limit_store.try_reserve_slot",
        side_effect=RuntimeError("GRUG_DATABASE_URL is not set"),
    ) as mock_reserve, caplog.at_level(logging.WARNING):
        outcome = ofl.acquire_free_tier_slot("m:free")
    assert outcome.admitted is False
    assert outcome.queued is False  # never even reached one poll interval
    mock_reserve.assert_called_once()  # no retry against a definitively erroring store
    assert not sleeps
    assert "openrouter_free_limiter_store_error" in caplog.text


def test_cancel_event_stops_the_queue_early(monkeypatch) -> None:
    import threading

    sleeps: list[float] = []
    monkeypatch.setattr(ofl, "_QUEUE_SLEEP", sleeps.append)
    cancel_event = threading.Event()
    cancel_event.set()
    with patch(
        "adapters.pg_rate_limit_store.try_reserve_slot",
        return_value=_result(False, 20, 5),
    ) as mock_reserve:
        outcome = ofl.acquire_free_tier_slot("m:free", cancel_event=cancel_event)
    assert outcome.admitted is False
    mock_reserve.assert_called_once()  # cancelled before a second attempt
    assert not sleeps


def test_telemetry_never_raises_when_observability_unavailable(monkeypatch) -> None:
    """Telemetry is best-effort: a missing/broken observability import must
    never take down the call it is describing."""
    with patch(
        "adapters.pg_rate_limit_store.try_reserve_slot",
        return_value=_result(True, 1, 1),
    ), patch.dict("sys.modules", {"observability": None}):
        outcome = ofl.acquire_free_tier_slot("m:free")  # must not raise
    assert outcome.admitted is True
