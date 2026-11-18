"""resting_guard.py — L5: sub-second protection of resting orders.

A resting maker order can be adversely selected: if spot moves toward
the strike or the book moves away, the order should be cancelled or
repriced fast — not at the next 15-second cycle, and not only by TTL.

``evaluate()`` is one fast-loop tick. It is cheap (cached data only),
non-raising, and idempotent: an order that left the ``resting`` state
is never cancelled again. Triggers:

    halt          ruin halt, venue-reconcile halt, daily-loss halt or a
                  signed pause_entries command (cancel all)
    feed_stale    the streaming feed was expected but is disconnected or
                  silent for more than feed_stale_seconds (dead-man
                  switch: cancel all)
    bbo_moved_away  the book crossed our resting price (a YES bid above
                  the ask, a NO ask below the market NO bid) — filling
                  now would be adverse selection
    spot_move     spot ate more than spot_move_fraction of the entry
                  cushion toward the strike

Cancels are idempotent and retried (caller keeps the order resting when
the venue refuses; the next tick retries). Every cancel records its
trigger and latency through the L1 latency recorder and emits an
``order.cancelled`` event with both.
"""

import logging
import time

log = logging.getLogger(__name__)

TRIGGER_HALT = "halt"
TRIGGER_FEED_STALE = "feed_stale"
TRIGGER_BBO_AWAY = "bbo_moved_away"
TRIGGER_SPOT_MOVE = "spot_move"

PM_TICK = 0.01


def adverse_move_fraction(side, above_market, spot_entry, spot_now,
                          strike) -> float | None:
    """Fraction of the entry cushion already eaten by an adverse spot
    move. Positive = spot moved against our side; negative = in our
    favour. ``None`` when the inputs cannot score the market (missing
    strike/spots, non-positive prices, zero entry cushion) — callers
    must treat ``None`` as "no opinion", never as an adverse move.
    """
    try:
        strike = float(strike)
        spot_entry = float(spot_entry)
        spot_now = float(spot_now)
    except (TypeError, ValueError):
        return None
    if strike <= 0 or spot_entry <= 0 or spot_now <= 0:
        return None
    if above_market:
        cushion = (spot_entry - strike) if side == "yes" else (strike - spot_entry)
        move = (spot_entry - spot_now) if side == "yes" else (spot_now - spot_entry)
    else:
        cushion = (strike - spot_entry) if side == "yes" else (spot_entry - strike)
        move = (spot_now - spot_entry) if side == "yes" else (spot_entry - spot_now)
    if cushion <= 0:
        return None  # entry failed its own gate; measure nothing
    return move / cushion


class RestingGuard:
    def __init__(self, *, lock, list_orders, cancel_one, spot_for, bbo_for,
                 feed_age, halt_active, tick: float = PM_TICK,
                 spot_move_fraction: float = 0.5, bbo_ticks: int = 5,
                 feed_stale_seconds: float = 30.0, recorder=None):
        if not 0.0 < float(spot_move_fraction) < 1.0:
            raise ValueError("spot_move_fraction must be in (0, 1)")
        if int(bbo_ticks) < 1:
            raise ValueError("bbo_ticks must be >= 1")
        self._lock = lock
        self._list_orders = list_orders       # () -> list of order dicts
        self._cancel_one = cancel_one          # (order, reason) -> bool
        self._spot_for = spot_for              # (symbol) -> float | None
        self._bbo_for = bbo_for                # (slug) -> (yes_bid, yes_ask)
        self._feed_age = feed_age              # () -> float | None
        self._halt_active = halt_active        # () -> bool
        self._tick = float(tick)
        self._spot_frac = float(spot_move_fraction)
        self._bbo_ticks = int(bbo_ticks)
        self._feed_stale = float(feed_stale_seconds)
        self._recorder = recorder

    def evaluate(self) -> dict:
        """One fast-loop tick. Non-raising."""
        try:
            return self._evaluate()
        except Exception as e:
            log.warning("resting guard evaluate failed (non-fatal): %s", e)
            return {"checked": 0, "cancelled": [], "error": str(e)}

    def _evaluate(self) -> dict:
        # H7: hold the lock only to snapshot state. Trigger evaluation and the
        # venue cancel happen outside it so a slow venue call cannot block the
        # trading cycle, and a slow cycle cannot freeze protection.
        with self._lock:
            orders = [o for o in (self._list_orders() or [])
                      if o.get("state") == "resting"]
        if not orders:
            return {"checked": 0, "cancelled": []}
        halt = self._safe(self._halt_active) or False
        feed_age = self._safe(self._feed_age)
        cancelled = []
        t_tick = time.monotonic()
        for o in orders:
            if o.get("state") != "resting":
                continue  # cancelled by the cycle while we were evaluating
            reason = self._trigger(o, halt=halt, feed_age=feed_age)
            if reason is None:
                continue
            oid = o.get("order_id")
            t0 = time.monotonic()
            try:
                ok = bool(self._cancel_one(o, reason))
            except Exception as e:
                log.warning("guard cancel %s failed (retry next tick): %s",
                            oid, e)
                ok = False
            dt_ms = (time.monotonic() - t0) * 1000.0
            self._record(reason, dt_ms)
            if not ok:
                continue
            cancelled.append({"order_id": oid, "trigger": reason,
                              "cancel_latency_ms": round(dt_ms, 2)})
            self._emit(o, reason, dt_ms)
        tick_ms = (time.monotonic() - t_tick) * 1000.0
        if tick_ms > 250.0:
            log.warning("guard tick took %.0fms (budget 250ms)", tick_ms)
        return {"checked": len(orders), "cancelled": cancelled}

    def _trigger(self, o, *, halt, feed_age) -> str | None:
        if halt:
            return TRIGGER_HALT
        if feed_age is not None and feed_age > self._feed_stale:
            return TRIGGER_FEED_STALE
        side = o.get("side", "")
        price = float(o.get("price", 0.0) or 0.0)
        slug = o.get("slug", "")
        bbo = self._safe(lambda: self._bbo_for(slug))
        if bbo:
            yes_bid, yes_ask = bbo
            if side == "yes" and yes_ask is not None:
                # Resting YES bid vs the market ask: once our price crosses
                # it we fill at our stale, too-high price — adverse selection.
                away = (price - float(yes_ask)) / self._tick
                if away > self._bbo_ticks:
                    return TRIGGER_BBO_AWAY
            elif side == "no" and yes_ask is not None:
                # Resting NO sell at P (NO price) vs the market NO bid
                # (1 - YES ask): once the bid crosses our ask we fill adversely.
                away = ((1.0 - float(yes_ask)) - price) / self._tick
                if away > self._bbo_ticks:
                    return TRIGGER_BBO_AWAY
        spot = self._safe(lambda: self._spot_for(o.get("symbol")))
        frac = adverse_move_fraction(side, bool(o.get("above_market")),
                                     o.get("spot"), spot, o.get("strike"))
        if frac is not None and frac > self._spot_frac:
            return TRIGGER_SPOT_MOVE
        return None

    def _record(self, reason: str, dt_ms: float) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.record("cancel", dt_ms)
            self._recorder.record(f"cancel.{reason}", dt_ms)
        except Exception:
            pass

    def _emit(self, o, reason: str, dt_ms: float) -> None:
        try:
            import emit
            emit.emit_event(
                "poly_us_trader", "order.cancelled",
                {"order_id": o.get("order_id"), "slug": o.get("slug", ""),
                 "reason": reason, "cancel_latency_ms": round(dt_ms, 2)},
                key=f"order.cancelled:{o.get('order_id')}:{reason}")
        except Exception as e:
            log.debug("guard cancel event emit failed: %s", e)

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None
