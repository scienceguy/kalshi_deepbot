#!/usr/bin/env python3
"""
Multifactor Backtest & Compounding Simulator
Evaluates parameter sensitivity across entry windows, price bands, edge margins, and fractional sizing allocations from $0.23.
"""
import glob
import json
import math
import os
import time
import polars as pl

ROOT = os.environ.get("HF_DATA_DIR", "/opt/superbot/hf_data")
FEE_RATE = 0.07

WINDOWS = [
    ("early_1_5", 1, 5),
    ("early_2_6", 2, 6),
    ("mid_2_10", 2, 10),
    ("mid_3_11", 3, 11),
    ("late_6_12", 6, 12),
    ("late_8_13", 8, 13),
    ("micro_13.5_14.8", 13, 15)
]

BANDS = [
    ("band_85_99", 85, 99),
    ("band_90_99", 90, 99),
    ("band_93_99", 93, 99),
    ("band_95_99", 95, 99)
]

MARGINS = [0.01, 0.02, 0.03, 0.04]
UNIVERSES = ["majors", "all"]
ALLOC_PCTS = [0.25, 0.50, 0.75, 1.00]

try:
    from scripts.common_hf_loader import load_kalshi_15m_data
except ImportError:
    from common_hf_loader import load_kalshi_15m_data

def run_simulation(trades_df, start_capital=0.23, alloc_pct=0.50):
    if trades_df.height == 0:
        return 0.0, 0.0, 0.0, 0
    
    sorted_trades = trades_df.sort(["open_ts", "minute"])
    balance = start_capital
    peak = balance
    max_dd = 0.0
    wins, losses = 0, 0
    
    for row in sorted_trades.iter_rows(named=True):
        ask_c = row["ask_c"]
        win = row["win"]
        fee_c = math.ceil(FEE_RATE * ask_c * (100.0 - ask_c) / 100.0)
        cost_per_contract = (ask_c + fee_c) / 100.0
        if cost_per_contract <= 0 or balance < 0.01:
            break
            
        # Allocate fraction of balance
        spend = max(0.01, min(balance, balance * alloc_pct))
        contracts = math.floor((spend / cost_per_contract) * 100) / 100.0
        if contracts < 0.01:
            # Fall back to minimum lot (0.01) if spend was below 1 contract cost
            contracts = 0.01 if balance >= cost_per_contract * 0.01 else 0.0
            
        if contracts < 0.01:
            continue
            
        actual_spend = round(contracts * cost_per_contract, 4)
        if actual_spend > balance:
            continue
            
        balance -= actual_spend
        if win:
            balance += contracts * 1.00
            wins += 1
        else:
            losses += 1
            
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            
    return balance, max_dd, wins / (wins + losses) if (wins + losses) > 0 else 0.0, wins + losses

def main():
    j = load_kalshi_15m_data(ROOT)
    j_majors = j.filter(pl.col("coin").is_in(["BTC", "ETH", "SOL"]))
    
    results = []
    
    for u in UNIVERSES:
        df_source = j_majors if u == "majors" else j
        for w_name, w0, w1 in WINDOWS:
            for b_name, b0, b1 in BANDS:
                for margin in MARGINS:
                    filt = (
                        (pl.col("minute") >= w0) & (pl.col("minute") <= w1) &
                        (pl.col("ask_c") >= b0) & (pl.col("ask_c") <= b1) &
                        ((pl.col("fv_c") - pl.col("ask_c")) >= margin * 100.0)
                    )
                    e = df_source.filter(filt).sort(["ticker", "minute"]).group_by("ticker").first()
                    if e.height < 30:
                        continue
                        
                    px = pl.col("ask_c")
                    fee = (FEE_RATE * px * (100.0 - px) / 100.0).ceil()
                    payout = pl.when(pl.col("win")).then(100.0).otherwise(0.0)
                    net = payout - px - fee
                    e2 = e.with_columns(net.alias("net"))
                    
                    n = e2.height
                    ev_c = float(e2["net"].mean())
                    win_rate = float(e2["win"].mean())
                    total_pnl = float(e2["net"].sum()) / 100.0
                    
                    # Test multiple allocation fractions
                    alloc_results = {}
                    for alloc in ALLOC_PCTS:
                        f_bal, m_dd, c_wr, c_trades = run_simulation(e2, start_capital=0.23, alloc_pct=alloc)
                        alloc_results[f"alloc_{int(alloc*100)}"] = {
                            "final_bal": f_bal,
                            "max_dd": m_dd,
                            "sim_trades": c_trades
                        }
                        
                    results.append({
                        "universe": u,
                        "window": w_name,
                        "band": b_name,
                        "margin": margin,
                        "trades": n,
                        "ev_c": ev_c,
                        "win_rate": win_rate,
                        "total_pnl": total_pnl,
                        **alloc_results
                    })
                    
    print(f"Evaluated multifactor configurations: {len(results)}")
    
    # Sort by EV/trade
    by_ev = sorted(results, key=lambda x: -x["ev_c"])
    print("\n" + "=" * 100)
    print("TOP 10 STRATEGIES BY STATISTICAL EDGE (EV / trade)")
    print("=" * 100)
    print(f"{'Univ':<7} {'Window':<15} {'Band':<12} {'Margin':<7} {'Trades':<7} {'EV/trade':<10} {'Win %':<7} {'Tot PnL':<9} {'$0.23 @ 25%':<12} {'$0.23 @ 50%':<12}")
    print("-" * 100)
    for r in by_ev[:10]:
        print(f"{r['universe']:<7} {r['window']:<15} {r['band']:<12} {r['margin']:<7.2f} {r['trades']:<7} {r['ev_c']:>+7.3f}c   {r['win_rate']*100:>5.1f}%   ${r['total_pnl']:>7.2f}   ${r['alloc_25']['final_bal']:>9.2f}   ${r['alloc_50']['final_bal']:>9.2f}")

    # Top surviving compounding from $0.23
    survived = [r for r in results if r["alloc_25"]["final_bal"] > 0.23]
    survived.sort(key=lambda x: -x["alloc_25"]["final_bal"])
    print("\n" + "=" * 100)
    print("TOP COMPOUNDING STRATEGIES FROM $0.23 (25% Fraction Allocation)")
    print("=" * 100)
    print(f"{'Univ':<7} {'Window':<15} {'Band':<12} {'Margin':<7} {'Trades':<7} {'EV/trade':<10} {'Win %':<7} {'Final Bal ($)':<14} {'Max DD':<8}")
    print("-" * 100)
    for r in survived[:10]:
        print(f"{r['universe']:<7} {r['window']:<15} {r['band']:<12} {r['margin']:<7.2f} {r['trades']:<7} {r['ev_c']:>+7.3f}c   {r['win_rate']*100:>5.1f}%   ${r['alloc_25']['final_bal']:>11.2f}   {r['alloc_25']['max_dd']*100:>6.1f}%")

    with open("/opt/superbot/multifactor_results.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    main()
