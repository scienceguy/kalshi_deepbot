"""Tests for rate_limit.py — token bucket pacing, thread safety, jittered backoff."""

import threading
import time

import pytest

from rate_limit import Backoff, RateLimiter, TokenBucket, TransientError, retry_transient


def test_bucket_paces_to_min_gap():
    """burst=1, rate=10/s => 4 gaps of 0.1s for 5 acquires."""
    b = TokenBucket(rate_per_sec=10.0, burst=1.0, name="t")
    start = time.monotonic()
    for _ in range(5):
        assert b.acquire()
    elapsed = time.monotonic() - start
    assert 0.38 < elapsed < 0.62, f"min-gap pacing off: {elapsed:.3f}s"


def test_bucket_allows_burst_then_paces():
    b = TokenBucket(rate_per_sec=10.0, burst=4.0, name="t")
    start = time.monotonic()
    for _ in range(4):
        assert b.acquire()
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.05  # full burst is immediate
    # 5th token needs 0.1s refill
    assert b.acquire() is True
    assert time.monotonic() - start >= 0.09


def test_acquire_honors_timeout():
    b = TokenBucket(rate_per_sec=1.0, burst=1.0, name="t")
    b.acquire()  # drain the single token
    start = time.monotonic()
    assert b.acquire(timeout=0.05) is False
    assert time.monotonic() - start < 0.2  # gave up promptly, not blocked for 1s


def test_nonblocking_acquire_never_waits():
    b = TokenBucket(rate_per_sec=1.0, burst=1.0, name="t")
    assert b.acquire(block=False) is True
    assert b.acquire(block=False) is False


def test_thread_safety_under_contention():
    """10 threads x 5 acquires on a 50 rps / burst-1 bucket must serialise:
    >= 49/50 s ~= 0.98s, lock protects state, no thread jumps the queue."""
    b = TokenBucket(rate_per_sec=50.0, burst=1.0, name="contention")

    def worker():
        for _ in range(5):
            assert b.acquire()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.85, f"contention broke pacing: {elapsed:.3f}s"
    assert b.acquires == 50


def test_record_rejection_slows_acquires():
    b = TokenBucket(rate_per_sec=100.0, burst=100.0, name="t")
    b.record_rejection(cooldown_seconds=0.3)
    start = time.monotonic()
    assert b.acquire() is True
    assert time.monotonic() - start >= 0.28  # waited out most of the cool-down


def test_backoff_ceilings_grow_with_attempt():
    bo = Backoff(base=0.5, cap=4.0, max_attempts=6)
    for attempt in range(6):
        d = bo.delay(attempt)
        assert 0.0 <= d <= min(4.0, 0.5 * 2.0 ** attempt), (attempt, d)
    # retry_after wins when present, capped at cap
    assert bo.delay(0, retry_after=100.0) <= 4.0
    assert bo.delay(0, retry_after=0.5) >= 0.49


def test_backoff_none_is_deterministic_ceiling():
    bo = Backoff(base=0.5, cap=4.0, jitter="none")
    assert bo.delay(0) == 0.5
    assert bo.delay(2) == 2.0
    assert bo.delay(10) == 4.0  # capped


def test_retry_transient_succeeds_after_retries():
    calls = {"n": 0}

    class Resp:
        status_code = 200

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("429", status_code=429, retry_after=0.01)
        return Resp()

    resp = retry_transient(
        flaky, backoff=Backoff(base=0.01, cap=0.1, max_attempts=5)
    )
    assert calls["n"] == 3 and isinstance(resp, Resp)


def test_retry_transient_exhausts_retries():
    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise TransientError("429", status_code=429)

    with pytest.raises(TransientError) as exc:
        retry_transient(
            always_429,
            backoff=Backoff(base=0.01, cap=0.05, max_attempts=2),
        )
    assert exc.value.status_code == 429
    assert calls["n"] == 3  # first try + 2 retries


def test_retry_transient_response_path():
    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}

    calls = {"n": 0}

    def flaky_http():
        calls["n"] += 1
        if calls["n"] == 1:
            return Resp(429)
        return Resp(200)

    resp = retry_transient(
        flaky_http,
        backoff=Backoff(base=0.01, cap=0.1, max_attempts=5),
    )
    assert calls["n"] == 2 and resp.status_code == 200


def test_rate_limiter_defaults_register():
    lim = RateLimiter()
    from rate_limit import configure_defaults

    lim = configure_defaults(lim)
    for name in ("kalshi", "kalshi_balance", "polymarket_us", "polymarket_gamma"):
        assert lim.bucket(name) is not None
    assert lim.bucket("kalshi").rate == 5.0  # 0.2s gap default
    assert lim.bucket("kalshi_balance").rate == 1.0
    stats = lim.stats()
    assert stats["kalshi"]["burst"] == 4.0
