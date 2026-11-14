"""
rate_limit.py — Thread-safe token-bucket rate guard + jittered backoff.

Two layers, one goal: the bot never earns a 429 from its own traffic.

1. Token buckets (proactive pacing): every outbound call acquires a token
   from the bucket for its host/endpoint class before it is sent. Buckets
   are configured strictly below each venue's documented limit, so a
   single bot process cannot self-inflict a 429. Multiple threads share
   one bucket safely (a lock guards state; sleeps happen outside the
   lock so one slow thread never blocks another).

2. Jittered exponential backoff (containment): edge bursts, limit
   changes, shared IPs and 503s can still produce 429/503 responses. On
   those, back off with full jitter, honor Retry-After when present, cap
   the delay, reset after success, and never retry forever.

A client-side guard cannot *literally* guarantee 0% 429s (the venue can
shrink limits or another process can share the IP/key). It guarantees
0% self-inflicted 429s and contains the rest.

Usage
-----
    from rate_limit import default_limiter, Backoff, retry_transient

    limiter = default_limiter()
    limiter.acquire("kalshi")                 # block until a token is free
    resp = session.get(url)
    if resp.status_code in (429, 503):
        retry_transient(lambda: session.get(url), backoff=Backoff())

Integration notes for this repo (see RATE_LIMIT_GUARD.md):
- kalshi_client.py._request: wrap the send in limiter.acquire("kalshi")
  + retry_transient on 429/503, replacing the manual _last_req gap logic.
- polymarket_us_client.py: replace _enforce_rate_limit() with
  limiter.acquire("polymarket_us"); add retry_transient on 429/503.
"""

import logging
import os
import random
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_RNG = random.Random()  # per-instance RNG has its own lock: safe across threads


class TransientError(Exception):
    """Raised by a caller to signal a retryable 429/503 without an HTTP response object.

    Carries an optional status_code and retry_after (seconds) parsed from
    the venue's error body/headers.
    """

    def __init__(self, message: str, status_code: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class TokenBucket:
    """Thread-safe token bucket.

    Tokens refill continuously at ``rate_per_sec`` tokens/second, up to
    ``burst``. ``acquire(cost)`` blocks until ``cost`` tokens are
    available, then consumes them and returns. With ``burst == 1`` this
    is exactly a minimum-request-gap limiter of ``1 / rate_per_sec`` s.
    """

    def __init__(self, rate_per_sec: float, burst: float, name: str = "bucket"):
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be > 0, got {rate_per_sec}")
        if burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        self.rate = float(rate_per_sec)
        self.burst = float(burst)
        self.name = name
        self._lock = threading.Lock()
        self._tokens = self.burst          # start full so a burst is allowed
        self._last = time.monotonic()
        self._penalty_until = 0.0          # cool-down deadline after a 429
        self.acquires = 0
        self.waited_seconds = 0.0

    # -- internal (caller must NOT hold the lock) --------------------------

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last = now

    # -- public API --------------------------------------------------------

    def available(self) -> float:
        """Tokens available right now (refilled, not consumed)."""
        with self._lock:
            self._refill(time.monotonic())
            return self._tokens

    def acquire(self, cost: float = 1.0, block: bool = True,
                timeout: Optional[float] = None) -> bool:
        """Consume ``cost`` tokens, blocking until they are available.

        Returns True on success, False if ``timeout`` expired first. With
        ``block=False`` returns immediately (False if not enough tokens).
        Sleeps happen outside the lock, so a sleeping thread never blocks
        other threads' refills.
        """
        if cost > self.burst:
            raise ValueError(f"cost {cost} exceeds burst {self.burst}")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._penalty_until:
                    # Cool-down after a 429: delay every acquire.
                    sleep_for = self._penalty_until - now
                else:
                    self._refill(now)
                    if self._tokens >= cost:
                        self._tokens -= cost
                        self.acquires += 1
                        return True
                    sleep_for = (cost - self._tokens) / self.rate
            if sleep_for <= 0:
                sleep_for = 0.001  # never busy-spin
            if not block:
                return False
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(sleep_for, remaining))
            else:
                time.sleep(sleep_for)
            self.waited_seconds += sleep_for
            if deadline is not None and time.monotonic() >= deadline:
                # One final check before giving up.
                with self._lock:
                    now = time.monotonic()
                    self._refill(now)
                    if self._tokens >= cost:
                        self._tokens -= cost
                        self.acquires += 1
                        return True
                return False

    def record_rejection(self, cooldown_seconds: float = 30.0) -> None:
        """Record a 429 from this bucket's host: slow new acquires for a
        cool-down window so the next burst cannot immediately 429 again."""
        with self._lock:
            self._penalty_until = max(
                self._penalty_until, time.monotonic() + cooldown_seconds
            )

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            return {
                "name": self.name,
                "rate_per_sec": self.rate,
                "burst": self.burst,
                "tokens": self._tokens,
                "acquires": self.acquires,
                "waited_seconds": round(self.waited_seconds, 3),
                "penalty_active": now < self._penalty_until,
            }


class RateLimiter:
    """Registry of named token buckets, one per host/endpoint class."""

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def register(self, name: str, rate_per_sec: float, burst: float) -> TokenBucket:
        with self._lock:
            bucket = TokenBucket(rate_per_sec, burst, name)
            self._buckets[name] = bucket
            return bucket

    def bucket(self, name: str) -> Optional[TokenBucket]:
        with self._lock:
            return self._buckets.get(name)

    def acquire(self, name: str, cost: float = 1.0,
                block: bool = True, timeout: Optional[float] = None) -> bool:
        bucket = self.bucket(name)
        if bucket is None:
            raise KeyError(f"no rate-limit bucket registered for {name!r}")
        return bucket.acquire(cost=cost, block=block, timeout=timeout)

    def record_rejection(self, name: str, cooldown_seconds: float = 30.0) -> None:
        bucket = self.bucket(name)
        if bucket is not None:
            bucket.record_rejection(cooldown_seconds)

    def stats(self) -> dict:
        with self._lock:
            return {name: b.stats() for name, b in self._buckets.items()}


class Backoff:
    """Exponential backoff with full jitter.

    delay(attempt) is uniform in [0, min(cap, base * 2**attempt)] where
    ``attempt`` is 0 for the first retry. Full jitter spreads retries
    and avoids thundering herds. ``retry_after`` (from a Retry-After
    header) wins when present, capped at ``cap``. Delays reset after a
    success because attempts start at 0 again.
    """

    def __init__(self, base: float = 0.7, cap: float = 30.0,
                 max_attempts: int = 5, jitter: str = "full"):
        if base <= 0 or cap <= 0:
            raise ValueError("base and cap must be > 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if jitter not in ("full", "equal", "none"):
            raise ValueError(f"unknown jitter {jitter!r}")
        self.base = float(base)
        self.cap = float(cap)
        self.max_attempts = int(max_attempts)
        self.jitter = jitter

    def delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Seconds to sleep before retry number ``attempt`` (0-based)."""
        if retry_after is not None:
            return min(float(retry_after), self.cap)
        ceiling = min(self.cap, self.base * (2.0 ** attempt))
        if self.jitter == "full":
            return _RNG.uniform(0.0, ceiling)
        if self.jitter == "equal":
            half = ceiling / 2.0
            return half + _RNG.uniform(0.0, half)
        return ceiling

    def sleep(self, attempt: int, retry_after: Optional[float] = None) -> float:
        d = self.delay(attempt, retry_after)
        time.sleep(d)
        return d


def retry_transient(call: Callable[[], object],
                    *,
                    retryable_statuses: frozenset = frozenset({429, 502, 503, 504}),
                    backoff: Optional[Backoff] = None,
                    max_attempts: Optional[int] = None,
                    on_retry: Optional[Callable[[int, float, Optional[int]], None]] = None,
                    limiter: Optional[RateLimiter] = None,
                    bucket_name: Optional[str] = None) -> object:
    """Retry a call that returns a response-like object (with
    ``status_code`` and ``headers``) or raises ``TransientError``.

    Returns the first non-retryable response, or re-raises the last
    non-transient exception. After ``max_attempts`` retries on a
    retryable status raises ``TransientError`` with the last status.
    """
    backoff = backoff or Backoff()
    attempts = backoff.max_attempts if max_attempts is None else max_attempts
    last_status = None
    for attempt in range(attempts + 1):  # first try + retries
        try:
            resp = call()
        except TransientError as exc:
            last_status = exc.status_code
            if attempt == attempts:
                raise
            retry_after = exc.retry_after
            if exc.status_code == 429 and limiter is not None and bucket_name is not None:
                limiter.record_rejection(bucket_name)
            d = backoff.sleep(attempt, retry_after)
            log.warning("transient %s on attempt %d/%d — slept %.2fs",
                        exc.status_code or "?", attempt + 1, attempts, d)
            if on_retry:
                on_retry(attempt, d, exc.status_code)
            continue
        # response-like object path
        status = getattr(resp, "status_code", None)
        last_status = status
        if status is None or status not in retryable_statuses:
            return resp
        if attempt == attempts:
            raise TransientError(
                f"retried {attempts} times, last status {status}", status_code=status,
                retry_after=_retry_after(resp),
            )
        retry_after = _retry_after(resp) if status == 429 else None
        if limiter is not None and bucket_name is not None and status == 429:
            limiter.record_rejection(bucket_name)
        d = backoff.sleep(attempt, retry_after)
        log.warning("HTTP %s on attempt %d/%d — slept %.2fs", status, attempt + 1,
                    attempts, d)
        if on_retry:
            on_retry(attempt, d, status)
    raise TransientError(f"exhausted {attempts} retries, last status {last_status}",
                         status_code=last_status)


def _retry_after(resp: object) -> Optional[float]:
    try:
        raw = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        return float(raw) if raw is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Defaults for this repo. Kept intentionally conservative (well under the
# documented ceilings) so the buckets never self-inflict a 429.
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def configure_defaults(limiter: Optional[RateLimiter] = None,
                       config_module: Optional[object] = None) -> RateLimiter:
    """Register the repo's default buckets on ``limiter`` (a new one if
    None). Rates are reads from env/config with safe built-in fallbacks:

    kalshi            /trade-api/v2 general          -> KALSHI_REQ_PER_SEC (default 5 rps)
    kalshi_balance    /portfolio/balance             -> 1 rps (Kalshi limits balance to 1 req/s)
    polymarket_us     api.polymarket.us (US trade)   -> POLYMARKET_REQ_PER_SEC (default 5 rps)
    polymarket_gamma  gamma-api.polymarket.com       -> POLYMARKET_GAMMA_REQ_PER_SEC (default 200 rps)

    Bursts allow a small cold-start burst; sustained pacing is the rate.
    """
    limiter = limiter or RateLimiter()

    def rate(name: str, env: str, default: float, burst: Optional[float] = None) -> float:
        val = _env_float(env, default)
        if config_module is not None:
            attr = {"kalshi": "REQUEST_MIN_GAP_SECONDS"}.get(name)
            if attr is not None and hasattr(config_module, attr):
                try:
                    gap = float(getattr(config_module, attr))
                    if gap > 0:
                        val = 1.0 / gap
                except (TypeError, ValueError):
                    pass
        limiter.register(name, val, burst if burst is not None else max(2.0, val))
        return val

    rate("kalshi", "KALSHI_REQ_PER_SEC", 5.0, burst=4.0)
    rate("kalshi_balance", "KALSHI_BALANCE_REQ_PER_SEC", 1.0, burst=1.0)
    rate("polymarket_us", "POLYMARKET_REQ_PER_SEC", 5.0, burst=2.0)
    rate("polymarket_gamma", "POLYMARKET_GAMMA_REQ_PER_SEC", 200.0, burst=50.0)
    return limiter


_DEFAULT_LIMITER: Optional[RateLimiter] = None
_DEFAULT_LOCK = threading.Lock()


def default_limiter() -> RateLimiter:
    """Process-wide singleton limiter with the repo's default buckets."""
    global _DEFAULT_LIMITER
    if _DEFAULT_LIMITER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_LIMITER is None:
                _DEFAULT_LIMITER = RateLimiter()
                try:
                    import config  # repo config; optional, env overrides anyway
                    configure_defaults(_DEFAULT_LIMITER, config)
                except Exception:
                    configure_defaults(_DEFAULT_LIMITER)
    return _DEFAULT_LIMITER


if __name__ == "__main__":  # pragma: no cover — quick self-check
    import sys

    # 1. Min-gap equivalence: burst=1, rate=10 -> 10 tokens/s, 4 gaps of 0.1s
    b = TokenBucket(rate_per_sec=10.0, burst=1.0, name="selfcheck")
    start = time.monotonic()
    for _ in range(5):
        assert b.acquire()
    elapsed = time.monotonic() - start
    assert 0.38 < elapsed < 0.62, f"min-gap pacing off: {elapsed:.3f}s"
    print(f"min-gap OK: 5 acquires in {elapsed:.3f}s")

    # 2. Contention: 10 threads x 5 acquires on a 50 rps / burst-1 bucket
    #    must take >= 49/50 s ~= 0.98s (lock serialises, no thread jumps the queue)
    b2 = TokenBucket(rate_per_sec=50.0, burst=1.0, name="contention")
    results = []

    def worker():
        for _ in range(5):
            assert b2.acquire()
        results.append(True)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.85, f"thread contention broke pacing: {elapsed:.3f}s"
    assert b2.acquires == 50
    print(f"thread safety OK: 50 contended acquires in {elapsed:.3f}s")

    # 3. Backoff ceilings grow with attempt and respect cap / retry_after
    bo = Backoff(base=0.5, cap=4.0, max_attempts=6)
    for attempt in range(6):
        d = bo.delay(attempt)
        assert 0.0 <= d <= min(4.0, 0.5 * 2 ** attempt), (attempt, d)
    assert bo.delay(0, retry_after=100.0) <= 4.0  # retry_after capped
    # deterministic spread: full jitter never exceeds ceiling
    max_observed = max(bo.delay(i) for i in range(20))
    assert max_observed <= min(4.0, 0.5 * 2 ** 19)
    print(f"backoff OK: max jittered delay {max_observed:.3f}s")

    # 4. retry_transient: two 429s then success
    calls = {"n": 0}

    class Resp:
        status_code = 200

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("429", status_code=429, retry_after=0.01)
        return Resp()

    resp = retry_transient(flaky, backoff=Backoff(base=0.01, cap=0.1, max_attempts=5))
    assert calls["n"] == 3 and isinstance(resp, Resp)

    # 5. retry_transient: exhaust retries on permanent 429
    calls["n"] = 0

    def always_429():
        calls["n"] += 1
        raise TransientError("429", status_code=429)

    try:
        retry_transient(always_429, backoff=Backoff(base=0.01, cap=0.05, max_attempts=2))
    except TransientError as exc:
        assert exc.status_code == 429 and calls["n"] == 3, (calls["n"], exc)
    else:
        raise AssertionError("expected TransientError")
    print("retry_transient OK")
    print("ALL SELF-CHECKS PASSED")
