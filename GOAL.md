# GOAL — kalshi_deepbot and its spin-off projects

The "Objective" block below is the exact text to hand to `/goal`. Everything
after it is the durable committed copy: the reasoning, the guardrails, and how
to reverse any of it.

## Objective

Grow trading capital only from edges that survive an out-of-sample, net-of-fee
test with a confidence interval that clears zero, and keep every state change
reversible from a fresh clone.

Program: (a) prove or kill each candidate edge in its own small repo, (b)
consolidate only the survivors into one bot. Do not add live order paths to a
candidate that has not cleared its gate. Do not lower a risk limit to make a
number look right.

Gates: momentum/trend on Kalshi 15M crypto is CLOSED (no variant cleared zero;
TurbineFI 4,904 backtests, gk_ 5,973 trades at -2.1c/trade, DegenHedge 13,082
signals, 60s BRTI settlement smoothing). OFI is CLOSED (AUC 0.766 alone but no
increment over price: 0.975 -> 0.980; every fee-aware bet negative). Remaining
candidates, each gated on its own evidence: panic fade (published positive,
unreplicated, needs discover/holdout + bootstrap CI), market making (strongest
academic evidence; needs queue, inventory and adverse-selection machinery),
cross-venue arbitrage (the only structurally sound one: buy UP on Kalshi and
DOWN on Polymarket in the same window when the basket costs under $1.00 after
fees). Sub-second execution is a prerequisite for arbitrage, not a feature: a
two-leg basket must land before the mispricing closes, so measure host
round-trip latency before claiming the strategy is viable.

ML must stay continuously trained: `scripts/train_ml.py` on the termux job
(7002, 6h) retrains from recorded history; a crash in that job is a P&L bug,
not a nuisance. Paper evidence and live evidence are different claims; only
report the second as the second.

Definition of done, per candidate: its own repo; a README stating the gate and
the honest limits; tests for the money math; recorded evidence with sample
counts and a fee-inclusive CI; a named reverse command for every commit; and
either promotion on a CI that clears zero or deletion of the code and its
config block. Consolidate only candidates that cleared. No secrets in any
repo; never claim a command succeeded without its output; ask before a
destructive git command or any live-armed action.

## Where each candidate stands

| Candidate | Repo | Status | Gate |
|---|---|---|---|
| Momentum / trend scalper | (deleted from this repo) | **CLOSED** — negative, CI below zero | deleted at `b1089ab`; `git revert b1089ab` to restore |
| OFI / microstructure | `scienceguy/kalshi-ofi-sanity` | **CLOSED** — informative, no increment over price, fee-aware bets negative | `b651231` |
| Cross-venue arbitrage | `scienceguy/kalshi-polymarket-arb` | **MEASURING** — detector live, no arb observed yet | needs a basket < $1.00 after fees, repeatedly |
| Panic fade | (not built) | **PENDING** — one positive commercial backtest, no holdout | discover/holdout + bootstrap CI |
| Market making | (not built) | **PENDING** — strongest academic evidence | needs queue + inventory + adverse-selection model |
| Sub-second execution | `kalshi-polymarket-arb/latency_probe.py` | **BLOCKED on host** | Termux p50 99-234 ms; needs OCI/GCP |

## Why the momentum result is final

Not a tuning problem. Kalshi settles `KXBTC15M` on the 60-second CF Benchmarks
BRTI average, which smooths exactly the intra-window move a scalper trades;
resting maker orders are picked off by informed flow (Bartlett & O'Hara 2026);
and momentum reverses at short horizons. At the high prices where momentum
fires, the ceil-to-cent taker fee makes breakeven a 96-100% win rate.

## Sub-second execution plan

1. **Measured baseline.** `latency_probe.py` on this host: Kalshi 98.8 ms p50,
   Gamma 100.6 ms, CLOB 233.7 ms. A two-leg basket lands in 0.5-1 s.
2. **Host.** OCI Always Free ARM (4 Ampere / 24 GB) as the free option; GCP
   C3/U4 with DPDK/OpenOnload + Firefly clock sync for the sub-2µs class. Both
   require the operator's cloud credentials — provisioning is not something an
   agent does unattended.
3. **Acceptance test.** Re-run `latency_probe.py` on the host and compare
   p50/p95; then replace HTTP with websocket book deltas + pre-signed order
   submission and measure that path too. Sub-second is claimed only when the
   probe shows it.
4. **Leg risk.** A basket is riskless only if both legs fill. The live path
   needs fire-both/hedge-remainder/abort handling before any money moves.

## Reversal

- One change: `git revert <sha>` in the repo that holds it — every commit is
  self-contained and names its own reversal.
- A whole spin-off: it is a self-contained repo; `rm -rf` it.
- This goal itself: `/goal clear`.

## Ask-first

Destructive git commands, any live-armed trading, provisioning paid cloud
resources, and lowering a risk limit (`CASH_FLOOR`, `EXPOSURE_CEILING`,
`PER_TRADE`, drawdown halt) all need the operator's explicit yes.
