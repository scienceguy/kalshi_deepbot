import math

import cfb_monitor as m


def test_fee_peaks_at_half_and_is_symmetric():
    assert m.taker_fee(0.50) == 0.02
    assert m.taker_fee(0.50) >= m.taker_fee(0.25)
    assert m.taker_fee(0.05) == m.taker_fee(0.95)
    assert m.taker_fee(0.02) == m.taker_fee(0.98)
    assert m.taker_fee(0.00) == 0.0


def test_prob_monotone_and_atm_half():
    ps = [m.p_above_held(75000.0, float(k), 0.0009, 0)
          for k in (74500, 74900, 75000, 75100, 75500)]
    assert all(a >= b for a, b in zip(ps, ps[1:])), ps
    assert abs(m.p_above_held(75000.0, 75000.0, 0.0009, 0) - 0.5) < 1e-12


def test_held_minute_is_deterministic():
    assert m.p_above_held(75000.0, 74000.0, 0.0009, 60) == 1.0
    assert m.p_above_held(75000.0, 76000.0, 0.0009, 60) == 0.0


def _sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def test_prob_matches_analytic():
    # P(avg > K) = P(N(0,sd) > ln(K/spot)) = sf(ln(K/spot)/sd)
    for K in (74500.0, 74950.0, 75000.0, 75050.0, 75500.0):
        z = math.log(K / 75000.0) / 0.0009
        assert abs(m.p_above_held(75000.0, K, 0.0009, 0) - _sf(z)) < 1e-9, K


def test_avg_ref_atm_half():
    assert abs(m.prob_avg_above(75000.0, 75000.0, 0.0009) - 0.5) < 1e-12


def test_window_half_open():
    pr = [dict(time=59000, value=1.0), dict(time=60000, value=2.0)]
    assert [p["time"] for p in m.settlement_window(pr, 60000)] == [59000]


def test_window_60_prints():
    pr = [dict(time=t, value=75000.0) for t in range(0, 61000, 1000)]
    win = m.settlement_window(pr, 60000)
    assert len(win) == 60, len(win)


def test_brti_avg():
    assert m.brti_avg([]) is None
    assert m.brti_avg([dict(value=1.0), dict(value=3.0)]) == 2.0


def test_coinflip_is_never_actionable():
    for ask in (0.1, 0.5, 0.9):
        row = m.score_row(ask, ask, 1.0 - ask)
        assert row["actionable"] is False, row


def test_99pct_at_5c_is_actionable():
    row = m.score_row(0.99, 0.05, 0.95)
    assert row["best"] == "yes"
    assert row["ev_yes_c"] > 0.0
    assert row["actionable"] is True


def test_selfcheck_runs():
    m._selfcheck()


def test_summary_reads_actionable_from_row_not_edge_key():
    # run_once used to read r["edge"]["actionable"], a key rows_from never sets,
    # so every summary said actionable=0 best_c=None even mid-settlement.
    snap = {
        "ts": 1794279600000, "secs_held": 30, "status": "scored",
        "close_ms": 1794279600000,
        "series": {"KXBTCD": {"ladder": [
            {"ticker": "T1", "strike": 70000.0, "yes_ask": 0.05,
             "no_ask": 0.95, "yes_bid": 0.04, "no_bid": 0.94, "p": 0.99},
        ]}},
    }
    rows = m.rows_from(snap)
    assert rows[0]["actionable"] is True, rows[0]
    assert "edge" not in rows[0]
    summ = m.summarize(rows, snap, 0)
    assert summ["actionable"] == 1, summ
    assert summ["best_c"] == rows[0]["best_c"]
    assert summ["best_c"] > 0.0


def test_fee_is_ceiled_once_per_order_not_per_contract():
    # 2 contracts at 70c: 0.07*2*0.7*0.3 = $0.0294 -> one cent for the order.
    # Per-contract it would be 2 * ceil(0.0147) = $0.04.
    two = m._fee_usd(2, 0.70)
    one = m._fee_usd(1, 0.70)
    assert abs(two - 0.03) < 1e-9, two
    assert two < 2 * one, (two, one)
    # 20 contracts at 5c: 0.07*20*0.05*0.95 = $0.0665 -> still one cent, $0.07
    assert abs(m._fee_usd(20, 0.05) - 0.07) < 1e-9


def test_settlement_sd_is_brownian_mean_sd():
    # sd of a Brownian time-average over the settling minute is sigma/sqrt(3)
    sig = 0.000442
    factor = m.sigma_avg(sig) / (sig / math.sqrt(3.0))
    assert abs(factor - 1.0125) < 5e-4, factor
    assert abs(m.prob_avg_above(75000.0, 75000.0, sig) - 0.5) < 1e-12


