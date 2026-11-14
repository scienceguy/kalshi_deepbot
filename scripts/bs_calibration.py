"""
scripts/bs_calibration.py — Black-Scholes N(d2) calibration for hourly binary
threshold contracts (cpc-btc/eth/sol-above-hr-*).

Self-contained (stdlib only). For a CSV of realized hourly threshold contracts
it prints:
  * Brier score of pure N(d2) pricing
  * calibration (reliability) curve: mean predicted prob vs observed frequency
  * Expected Calibration Error (ECE)
  * overall and per-(strike-cushion-z) win rates

CSV (header required):
    symbol, spot, strike, minutes_left, sigma_annualized, outcome
    outcome in {0,1}; 1 means S_T >= K (Yes won).
Optional column: entry_price (dollars paid for Yes) for adverse-selection/EV math.

Math (pure Black-Scholes lognormal diffusion, no orderbook momentum):
    T = minutes_left / 525600                      [years]
    d2 = (ln(S/K) + (r - 0.5*sigma^2)*T) / (sigma*sqrt(T))
    p  = N(d2), clamped to [0.001, 0.999]
    z  = |ln(S/K)| / (sigma*sqrt(T))               [strike cushion in vol units]

Usage:
    python3 scripts/bs_calibration.py --csv realized_hourly.csv
    python3 scripts/bs_calibration.py --demo          # illustrative synthetic run
    python3 scripts/bs_calibration.py --live-snapshot # list live hourly markets
"""

import argparse
import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist

_N = NormalDist()
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0  # 525600
RISK_FREE = 0.05


# ── Core Black-Scholes N(d2) ──────────────────────────────────────────────

def bs_d2(spot, strike, minutes_left, sigma_annualized, risk_free=RISK_FREE):
    """Black-Scholes d2 for P(S_T >= K). Returns None if inputs unusable."""
    if minutes_left <= 0 or spot <= 0 or strike <= 0 or sigma_annualized <= 0:
        return None
    T = minutes_left / MINUTES_PER_YEAR
    try:
        return (math.log(spot / strike) +
                (risk_free - 0.5 * sigma_annualized * sigma_annualized) * T) / \
               (sigma_annualized * math.sqrt(T))
    except (ValueError, OverflowError):
        return None


def bs_prob(spot, strike, minutes_left, sigma_annualized, risk_free=RISK_FREE):
    """P(S_T >= K) = N(d2), clamped off 0 and 1 so log-loss/Brier stay finite."""
    d2 = bs_d2(spot, strike, minutes_left, sigma_annualized, risk_free)
    if d2 is None:
        return None
    return max(0.001, min(0.999, _N.cdf(d2)))


def cushion_z(spot, strike, minutes_left, sigma_annualized):
    """Strike cushion in vol units: |ln(S/K)| / (sigma*sqrt(T))."""
    if minutes_left <= 0 or spot <= 0 or strike <= 0 or sigma_annualized <= 0:
        return None
    T = minutes_left / MINUTES_PER_YEAR
    denom = sigma_annualized * math.sqrt(T)
    if denom <= 0:
        return None
    return abs(math.log(spot / strike)) / denom


# ── Metrics ───────────────────────────────────────────────────────────────

def brier_score(probs, outcomes):
    """Mean squared error of probability forecasts vs binary outcomes."""
    n = len(probs)
    if n == 0:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / n


def calibration_bins(probs, outcomes, n_bins=10):
    """Equal-frequency reliability bins. Returns list of dicts."""
    n = len(probs)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: probs[i])
    bins = []
    for b in range(n_bins):
        start = b * n // n_bins
        end = (b + 1) * n // n_bins
        idx = order[start:end]
        if not idx:
            continue
        mean_p = sum(probs[i] for i in idx) / len(idx)
        obs_freq = sum(outcomes[i] for i in idx) / len(idx)
        bins.append({
            "bin": b + 1,
            "n": len(idx),
            "mean_predicted": round(mean_p, 4),
            "observed_frequency": round(obs_freq, 4),
            "calibration_error": round(mean_p - obs_freq, 4),
        })
    return bins


def expected_calibration_error(probs, outcomes, n_bins=10):
    """ECE = sum_b (n_b/n) * |mean_predicted_b - observed_frequency_b|."""
    n = len(probs)
    if n == 0:
        return None
    order = sorted(range(n), key=lambda i: probs[i])
    ece = 0.0
    for b in range(n_bins):
        start = b * n // n_bins
        end = (b + 1) * n // n_bins
        idx = order[start:end]
        if not idx:
            continue
        w = len(idx) / n
        mean_p = sum(probs[i] for i in idx) / len(idx)
        obs = sum(outcomes[i] for i in idx) / len(idx)
        ece += w * abs(mean_p - obs)
    return round(ece, 4)


def z_bin(z):
    if z is None:
        return "unknown"
    if z >= 2.00:
        return "z>=2.00"
    if z >= 1.80:
        return "1.80<=z<2.00"
    if z >= 1.50:
        return "1.50<=z<1.80"
    if z >= 1.00:
        return "1.00<=z<1.50"
    return "z<1.00"


def win_rate_by_z(rows):
    """rows: list of dicts with z, outcome, p."""
    groups = defaultdict(list)
    for r in rows:
        groups[z_bin(r["z"])].append(r)
    out = []
    for label in ("z<1.00", "1.00<=z<1.50", "1.50<=z<1.80", "1.80<=z<2.00", "z>=2.00", "unknown"):
        grp = groups.get(label, [])
        if not grp:
            continue
        wr = sum(r["outcome"] for r in grp) / len(grp)
        mean_p = sum(r["p"] for r in grp) / len(grp)
        out.append({
            "cushion": label,
            "n": len(grp),
            "win_rate": round(wr, 4),
            "mean_Nd2": round(mean_p, 4),
            "edge_over_price_pp": round((wr - mean_p) * 100, 2),
        })
    return out


# ── CSV I/O ───────────────────────────────────────────────────────────────

def load_realized(csv_path):
    """Load realized contracts; enrich each with p=N(d2) and z."""
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            try:
                spot = float(raw["spot"])
                strike = float(raw["strike"])
                minutes_left = float(raw["minutes_left"])
                sigma = float(raw["sigma_annualized"])
                outcome = int(float(raw["outcome"]))
            except (KeyError, ValueError, TypeError):
                continue
            p = bs_prob(spot, strike, minutes_left, sigma)
            z = cushion_z(spot, strike, minutes_left, sigma)
            if p is None or z is None:
                continue
            row = {
                "symbol": raw.get("symbol", ""),
                "spot": spot,
                "strike": strike,
                "minutes_left": minutes_left,
                "sigma": sigma,
                "outcome": outcome,
                "p": p,
                "z": z,
            }
            if raw.get("entry_price") not in (None, ""):
                try:
                    row["entry_price"] = float(raw["entry_price"])
                except ValueError:
                    pass
            rows.append(row)
    return rows


def print_report(rows, label="calibration"):
    probs = [r["p"] for r in rows]
    outcomes = [r["outcome"] for r in rows]
    n = len(rows)
    print(f"=== Black-Scholes N(d2) calibration — {label} (n={n}) ===")
    br = brier_score(probs, outcomes)
    print(f"Brier score of N(d2):            {br:.6f}" if br is not None else "Brier score: n/a")
    print(f"Expected Calibration Error:      {expected_calibration_error(probs, outcomes)}")
    print()
    print("Calibration (reliability) curve — equal-frequency bins:")
    print(f"  {'bin':>4} {'n':>5} {'mean predicted':>16} {'observed freq':>15} {'cal err':>9}")
    for b in calibration_bins(probs, outcomes):
        print(f"  {b['bin']:>4} {b['n']:>5} {b['mean_predicted']:>16.4f} {b['observed_frequency']:>15.4f} {b['calibration_error']:>9.4f}")
    print()
    print("Win rate vs strike cushion z:")
    print(f"  {'cushion':>16} {'n':>5} {'win rate':>9} {'mean N(d2)':>11} {'empirical - N(d2) pp':>21}")
    for g in win_rate_by_z(rows):
        print(f"  {g['cushion']:>16} {g['n']:>5} {g['win_rate']:>9.4f} {g['mean_Nd2']:>11.4f} {g['edge_over_price_pp']:>21.2f}")
    print()
    print(f"Overall win rate:           {sum(outcomes)/n:.4f}")
    print(f"Mean predicted N(d2):       {sum(probs)/n:.4f}")
    print(f"Brier of unconditional (base rate): {brier_score([sum(outcomes)/n]*n, outcomes):.6f}")
    return {
        "n": n,
        "brier": br,
        "ece": expected_calibration_error(probs, outcomes),
        "calibration_bins": calibration_bins(probs, outcomes),
        "win_rate_by_z": win_rate_by_z(rows),
        "overall_win_rate": sum(outcomes) / n,
        "mean_predicted": sum(probs) / n,
    }


def run_csv(csv_path):
    rows = load_realized(csv_path)
    if not rows:
        print("No usable rows in", csv_path)
        return
    return print_report(rows, os.path.basename(csv_path))


# ── Continuous live verification hook (importable by poly_us_trader.py) ──
class CalibrationVerifier:
    """
    Tracks calibrated win rate vs live settlements for hourly threshold contracts.

    poly_us_trader.py integration (3 call sites):
      1. Before placing a paper/real order:   verifier.record_entry(...)
      2. In check_settlements() after a fill: verifier.reconcile_settlement(slug, outcome)
      3. Once per cycle / on demand:         verifier.snapshot() -> Brier, ECE, win-rate-by-z

    Persists to a JSONL ledger so verification survives restarts.
    """

    def __init__(self, ledger_path="local_data/bs_calibration_ledger.jsonl"):
        self.ledger_path = ledger_path
        self.entries = {}          # slug -> entry dict (open positions awaiting settlement)
        self.settled = []          # list of completed records with outcome
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("outcome") is None:
                        self.entries[rec["slug"]] = rec
                    else:
                        self.settled.append(rec)
        except (json.JSONDecodeError, OSError):
            pass

    def _append(self, rec):
        d = os.path.dirname(self.ledger_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # -- call sites --------------------------------------------------------
    def record_entry(self, slug, side, price_dollars, predicted_p, z,
                     symbol="", strike=None, minutes_left=None, sigma=None,
                     opened_at=None):
        """Record a newly opened position: its predicted probability p=N(d2)."""
        rec = {
            "slug": slug,
            "side": side,
            "entry_price": price_dollars,
            "predicted_p": predicted_p,
            "z": z,
            "symbol": symbol,
            "strike": strike,
            "minutes_left": minutes_left,
            "sigma": sigma,
            "opened_at": opened_at or datetime.now(timezone.utc).isoformat(),
            "outcome": None,
        }
        self.entries[slug] = rec
        self._append(rec)

    def reconcile_settlement(self, slug, outcome):
        """
        Match an open entry by slug, record the realized outcome (1 = Yes won,
        0 = No won), and return the completed record. Returns None if slug not
        open. Safe to call repeatedly (idempotent per slug).
        """
        rec = self.entries.pop(slug, None)
        if rec is None:
            return None
        rec["outcome"] = int(outcome)
        rec["settled_at"] = datetime.now(timezone.utc).isoformat()
        rec["win"] = 1 if (rec["side"] == "yes") == bool(outcome) else 0
        self.settled.append(rec)
        self._append(rec)
        return rec

    # -- snapshot ----------------------------------------------------------
    def snapshot(self):
        """Return running calibration metrics over all settled contracts."""
        rows = []
        for r in self.settled:
            if r["outcome"] is None:
                continue
            rows.append({
                "p": r["predicted_p"],
                "outcome": r["outcome"],
                "z": r["z"],
                "win": r["win"],
                "entry_price": r.get("entry_price"),
            })
        if not rows:
            return {"n": 0, "message": "no settled contracts yet"}
        probs = [r["p"] for r in rows]
        outcomes = [r["outcome"] for r in rows]
        for r in rows:
            r["z_bin"] = z_bin(r["z"])
        return {
            "n": len(rows),
            "brier": brier_score(probs, outcomes),
            "ece": expected_calibration_error(probs, outcomes),
            "overall_win_rate": sum(outcomes) / len(outcomes),
            "mean_predicted": sum(probs) / len(probs),
            "win_rate_by_z": win_rate_by_z(rows),
            "calibration_bins": calibration_bins(probs, outcomes),
            "open_positions": len(self.entries),
        }

    def should_halt(self, max_brier=0.25, min_win_rate_z180=0.95):
        """Risk guard: halt new entries if calibration degrades past limits."""
        snap = self.snapshot()
        if snap["n"] == 0:
            return False, "no settled data yet"
        reasons = []
        if snap["brier"] is not None and snap["brier"] > max_brier:
            reasons.append(f"Brier {snap['brier']:.4f} > {max_brier}")
        for g in snap["win_rate_by_z"]:
            if g["cushion"] in ("z>=2.00", "1.80<=z<2.00"):
                if g["win_rate"] < min_win_rate_z180:
                    reasons.append(f"{g['cushion']} win rate {g['win_rate']:.2%} < {min_win_rate_z180:.0%}")
        return (len(reasons) > 0, "; ".join(reasons) if reasons else "calibration within limits")


# ── Task 2: rolling calibration monitor + adaptive volatility scaler ──
# Importable by poly_us_trader.py for continuous Brier calibration.


class RollingCalibrationMonitor(CalibrationVerifier):
    """
    Rolling calibration monitor for poly_us_trader.py.

    Extends CalibrationVerifier with:
      * running Brier score over the last ROLLING_BRIER_N (50) settled trades
      * price-band (92c-99c) calibration: mean model-implied p vs observed win rate
      * trailing TRAILING_WIN_N (10) trade win rate
      * circuit breaker: halt new entries if rolling Brier > BRIER_HALT_THRESHOLD
        (0.150) or trailing 10 win rate < WIN_RATE_HALT_THRESHOLD (95%)

    Persists to the same JSONL ledger as CalibrationVerifier, so calibration
    survives restarts. Call sites mirror CalibrationVerifier:
      1. record_entry(...) before placing an order
      2. reconcile_settlement(slug, outcome) at settlement
      3. snapshot_rolling() + circuit_breaker() once per cycle
    """

    ROLLING_BRIER_N = 50
    TRAILING_WIN_N = 10
    BRIER_HALT_THRESHOLD = 0.150
    WIN_RATE_HALT_THRESHOLD = 0.95
    PRICE_BAND_MIN = 0.92
    PRICE_BAND_MAX = 0.99

    def __init__(self, ledger_path="local_data/bs_calibration_ledger.jsonl"):
        super().__init__(ledger_path)
        self.brier_history = []  # list of (ts, brier) for drift detection

    def snapshot_rolling(self):
        """Calibration metrics over the rolling window (last 50 settled) + trailing 10."""
        settled = self.settled[-self.ROLLING_BRIER_N:]
        rows = []
        for r in settled:
            if r.get("outcome") is None or r.get("predicted_p") is None:
                continue
            rows.append({
                "p": r["predicted_p"],
                "outcome": r["outcome"],
                "z": r.get("z"),
                "win": r["win"],
                "entry_price": r.get("entry_price"),
            })
        if not rows:
            return {"n": 0, "message": "no settled contracts yet"}

        probs = [r["p"] for r in rows]
        outcomes = [r["outcome"] for r in rows]
        n = len(rows)
        brier = brier_score(probs, outcomes)

        # Trailing 10-trade win rate.
        trailing = rows[-self.TRAILING_WIN_N:]
        trailing_wr = (sum(r["win"] for r in trailing) / len(trailing)) if trailing else None

        # Price-band calibration (92c-99c): model-implied p vs empirical frequency.
        band = None
        band_rows = [
            r for r in rows
            if r["entry_price"] is not None
            and self.PRICE_BAND_MIN <= r["entry_price"] <= self.PRICE_BAND_MAX
        ]
        if band_rows:
            band_p = sum(r["p"] for r in band_rows) / len(band_rows)
            band_wr = sum(r["win"] for r in band_rows) / len(band_rows)
            band = {
                "n": len(band_rows),
                "mean_predicted_p": round(band_p, 4),
                "observed_win_rate": round(band_wr, 4),
                "calibration_gap_pp": round((band_p - band_wr) * 100, 2),
            }

        ece = expected_calibration_error(probs, outcomes)

        # Brier drift vs previous snapshot.
        drift = None
        if brier is not None:
            now = datetime.now(timezone.utc).timestamp()
            self.brier_history.append((now, brier))
            if len(self.brier_history) > 1:
                drift = brier - self.brier_history[-2][1]
            if len(self.brier_history) > 200:
                self.brier_history = self.brier_history[-200:]

        return {
            "n": n,
            "rolling_brier": round(brier, 6) if brier is not None else None,
            "brier_drift": round(drift, 6) if drift is not None else None,
            "ece": ece,
            "trailing_10_n": len(trailing),
            "trailing_10_win_rate": round(trailing_wr, 4) if trailing_wr is not None else None,
            "price_band_92_99c": band,
            "overall_win_rate": round(sum(outcomes) / n, 4),
            "mean_predicted": round(sum(probs) / n, 4),
            "open_positions": len(self.entries),
        }

    def circuit_breaker(self):
        """
        Automated halt decision for new entries.

        Returns (halt: bool, reason: str). Halts when:
          * rolling Brier (last 50) > 0.150, OR
          * trailing 10-trade win rate < 95%.
        """
        snap = self.snapshot_rolling()
        if snap["n"] == 0:
            return False, "no settled data yet"
        reasons = []
        brier = snap["rolling_brier"]
        if brier is not None and brier > self.BRIER_HALT_THRESHOLD:
            reasons.append(f"rolling Brier {brier:.4f} > {self.BRIER_HALT_THRESHOLD}")
        wr = snap["trailing_10_win_rate"]
        if wr is not None and wr < self.WIN_RATE_HALT_THRESHOLD:
            reasons.append(
                f"trailing 10 win rate {wr:.2%} < {self.WIN_RATE_HALT_THRESHOLD:.0%}"
            )
        return (len(reasons) > 0, "; ".join(reasons) if reasons else "calibration within limits")


class AdaptiveVolatilityScaler:
    """
    Adaptive volatility scaling for strike-cushion hurdles.

    Scales the annualized sigma (and, on degradation, the min-z hurdle) based
    on observed Brier drift:

        drift = (brier - reference_brier) / reference_brier

    * Drift > 0 (calibration degrading, model overconfident):
      sigma scales UP -> computed cushion z = |ln(S/K)|/(sigma*sqrt(T)) falls
      for the same spot/strike distance -> fewer entries clear the hurdle ->
      effective cushion widens -> more conservative.
      min_z also widens (secondary knob).

    * Drift < 0 (calibration improving):
      sigma scales DOWN -> computed z rises -> hurdle narrows -> more entries.

    Clamped so one bad snapshot cannot blind the model.
    """

    def __init__(self, base_sigma=0.80, base_min_z=1.80, gain=2.0,
                 scale_min=0.5, scale_max=2.0, z_hurdle_max=2.5):
        self.base_sigma = float(base_sigma)
        self.base_min_z = float(base_min_z)
        self.gain = float(gain)
        self.scale_min = float(scale_min)
        self.scale_max = float(scale_max)
        self.z_hurdle_max = float(z_hurdle_max)
        self.reference_brier = None  # set from first non-None snapshot

    def update_reference(self, brier):
        """Set the reference Brier from the first available snapshot."""
        if brier is not None and self.reference_brier is None:
            self.reference_brier = float(brier)

    def scale(self, brier):
        """
        Return (scale, adjusted_sigma, adjusted_min_z, drift).

        scale:     multiplier applied to base_sigma (clamped to [scale_min, scale_max])
        adjusted_sigma: base_sigma * scale
        adjusted_min_z: base_min_z widened only when drift > 0, capped at z_hurdle_max
        drift:     (brier - reference) / reference, or None if unset
        """
        self.update_reference(brier)
        if brier is None or self.reference_brier is None or self.reference_brier <= 0:
            return 1.0, self.base_sigma, self.base_min_z, None

        drift = (brier - self.reference_brier) / self.reference_brier
        drift = max(-1.0, min(1.0, drift))

        # Sigma scales with full drift (both directions).
        scale = 1.0 + self.gain * drift
        scale = max(self.scale_min, min(self.scale_max, scale))

        # Min-z hurdle widens only on degradation; never narrows below base.
        z_hurdle_scale = 1.0 + self.gain * max(0.0, drift)
        z_hurdle_scale = min(self.z_hurdle_max / self.base_min_z, z_hurdle_scale)

        adjusted_sigma = self.base_sigma * scale
        adjusted_min_z = self.base_min_z * z_hurdle_scale

        return scale, adjusted_sigma, adjusted_min_z, drift


# ── Demo / live snapshot ─────────────────────────────────────────────────

def demo_dataset(path, n=2000, seed=7):
    """Generate an illustrative synthetic hourly-threshold dataset.

    True win probability = N(d2) (so calibration is ideal by construction).
    Realized vol is lower than the composite sigma used to price, so deep-cushion
    contracts win more often than N(d2) quotes — reproducing the >=99% at z>=1.80
    regime. Labeled illustrative.
    """
    import random
    random.seed(seed)
    rows = []
    symbols = ["BTC", "ETH", "SOL"]
    spot_px = {"BTC": 67000.0, "ETH": 3600.0, "SOL": 160.0}
    for _ in range(n):
        sym = random.choice(symbols)
        spot = spot_px[sym] * random.uniform(0.97, 1.03)
        z_target = random.choice([0.6, 1.1, 1.5, 1.9, 2.3])
        sigma = random.uniform(0.70, 1.10)
        minutes_left = random.uniform(10, 60)
        T = minutes_left / MINUTES_PER_YEAR
        move = math.exp(z_target * sigma * math.sqrt(T))
        side = random.choice([-1, 1])
        strike = spot * (move if side == 1 else 1 / move)
        p = bs_prob(spot, strike, minutes_left, sigma)
        realized_p = min(0.999, max(0.001, p + (0.03 if z_target >= 1.8 else -0.01)))
        outcome = 1 if random.random() < realized_p else 0
        entry_price = p + random.uniform(-0.04, 0.06)  # noisy momentum-ish entry
        entry_price = min(0.99, max(0.01, entry_price))
        rows.append({
            "symbol": sym, "spot": round(spot, 2), "strike": round(strike, 2),
            "minutes_left": round(minutes_left, 2), "sigma_annualized": round(sigma, 3),
            "outcome": outcome, "entry_price": round(entry_price, 4),
        })
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "spot", "strike", "minutes_left",
                                          "sigma_annualized", "outcome", "entry_price"])
        w.writeheader()
        w.writerows(rows)
    return path


def live_snapshot():
    """List currently-tradeable Polymarket US hourly threshold markets."""
    try:
        import polymarket_us_client as p
    except Exception as e:
        print("polymarket_us_client not importable:", e)
        return
    seen = set()
    for sym in ("BTC", "ETH", "SOL"):
        try:
            events = p.search_events(f"{sym} above")
            for ev in events[:2]:
                eslug = ev.get("slug")
                if not eslug:
                    continue
                full = p.get_event_by_slug(eslug)
                for m in (full.get("event") or {}).get("markets", []) or []:
                    ms = m.get("slug", "")
                    if "above-hr" not in ms:
                        continue
                    if ms in seen:
                        continue
                    seen.add(ms)
                    terms = m.get("assetPriceTerms") or {}
                    strike = None
                    sd = terms.get("strike") or terms.get("priceToBeat")
                    if isinstance(sd, dict):
                        strike = sd.get("value")
                    end = terms.get("windowEnd") or m.get("endDate")
                    minutes_left = None
                    if end:
                        try:
                            ed = datetime.fromisoformat(end.replace("Z", "+00:00"))
                            minutes_left = (ed - datetime.now(timezone.utc)).total_seconds() / 60.0
                        except Exception:
                            pass
                    lp = sp = None
                    op = m.get("outcomePrices")
                    if isinstance(op, str):
                        try:
                            op = json.loads(op)
                        except Exception:
                            op = []
                    if isinstance(op, list) and len(op) >= 1:
                        try:
                            lp = float(op[0]); sp = round(1.0 - lp, 4)
                        except Exception:
                            pass
                    print(f"{ms:55s} strike={strike} min_left={minutes_left and round(minutes_left,1)} yes={lp} no={sp}")
        except Exception as e:
            print(f"{sym} snapshot skipped: {e}")


def main():
    ap = argparse.ArgumentParser(description="N(d2) calibration for hourly threshold contracts")
    ap.add_argument("--csv", help="path to realized hourly-threshold CSV")
    ap.add_argument("--demo", action="store_true", help="run an illustrative synthetic calibration")
    ap.add_argument("--live-snapshot", action="store_true",
                    help="list live cpc-*-above-hr-* markets with strike/minutes-left/quote")
    args = ap.parse_args()

    if args.demo:
        path = "/data/data/com.termux/files/home/tmp/opencode/bs_calib_demo.csv"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        demo_dataset(path)
        return run_csv(path)

    if args.live_snapshot:
        return live_snapshot()

    if args.csv:
        return run_csv(args.csv)

    ap.print_help()


if __name__ == "__main__":
    main()
