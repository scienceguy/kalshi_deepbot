# T16-04 NO-side band and window measurement

Date: 2026-11-22  
Data: real Kalshi production history only, from `hf_data/quote_history/*.parquet`  
Result: NO band that promotes does not exist on this dataset

## Scope and method

This is an ask-side quote study, not a fill simulation. It does not assume
depth at the ask and does not infer maker-fill probability. No band is
promoted from this report.

Input: 81 real quote-history files. After filtering to crypto assets
(BTC, ETH, SOL, XRP, DOGE, HYPE, NEAR, ZEC) and settled markets:
863,338 quotes across 60,000 markets, 2026-07-23 23:46Z through
2026-10-11 03:45Z.

Definitions:

- NO ask = `1 - yes_bid` (complement of the recorded YES bid, dollars).
- Buy one NO contract at that quote; settle from the venue `result` field.
- Fee = `fees.taker_fee_dollars(1.0, price)` from `fees.py`, standard
  multiplier 1.0. This is the exact repo fee code, not a re-implementation.
- Per-quote net = `outcome - price - fee`, where outcome is 1 if the market
  settles NO.
- Clustering: first average net within each market ticker, then compute the
  mean across markets. `lo95` is `mean - 1.96 * SE` of those market-level
  net values. This is a simple 95% lower bound, not the final T16-50
  cluster-robust estimator.

Reproduction command (read-only):

```bash
.venv/bin/python /tmp/opencode/measure_band.py
```

## Aggregate bands, all minutes

Selected rows (net EV in cents per contract):

| NO price | markets | win | price | fee | netEV | lo95 netEV | clears |
|---|---:|---:|---:|---:|---:|---:|---|
| 1-5c | 22,921 | 0.023995 | 2.5770c | 0.1749c | -0.3524c | -0.5493c | no |
| 5-10c | 21,436 | 0.068530 | 7.5319c | 0.4859c | -1.1648c | -1.5017c | no |
| 90-95c | 22,038 | 0.915782 | 92.2498c | 0.4988c | -1.1704c | -1.5358c | no |
| 95-100c | 26,118 | 0.969178 | 98.2335c | 0.1200c | -1.4357c | -1.6426c | no |

Every 1c-to-100c NO band measured here has a negative mean and a negative
95% lower bound after the repo fee model.

## Aggregate elapsed minutes, all prices

At minute 6-7 elapsed: netEV -1.8637c, lo95 -2.1939c (60,000 markets).  
At minute 10-11 elapsed: netEV -1.5229c, lo95 -1.7968c (59,985 markets).

No elapsed-minute window has a nonnegative lower bound.

## Target slices, 6-11 minutes elapsed, by asset

The configured YES 95-99c favourite band is equivalent to a NO 1-5c band.
With 6-11 minutes elapsed it is negative for every measured asset:

| asset | markets | netEV | lo95 |
|---|---:|---:|---:|
| BTC | 1,005 | -0.4517c | -1.5006c |
| ETH | 1,165 | -0.2208c | -1.2259c |
| SOL | 1,211 | -1.3241c | -2.1072c |
| XRP | 1,111 | -1.5778c | -2.3770c |
| DOGE | 1,157 | -0.7836c | -1.6783c |
| HYPE | 1,253 | -0.3646c | -1.2841c |
| NEAR | 1,000 | -0.6869c | -1.6495c |
| ZEC | 1,080 | -0.3852c | -1.3766c |

The best point estimate in any per-asset 95-100c NO slice was XRP:
+0.2207c over 1,114 markets, but its 95% lower bound was -0.7428c, so it
does not clear either.

## What is not measured

- Maker fills: the quote files do not contain our queue position, trade
  prints, or time-priority data. No maker-fill EV is claimed.
- Depth at the ask: the files do not contain top-of-book depth, so this is
  not a valid R8 dry-fill study and cannot be used as dry/live fill parity.
- Polymarket US: the local production dry DB has one `poly_candles` row and
  one `poly_markets` row, too sparse to measure.
- A pristine holdout: the HF set has already been used for other repository
  research; this is not the pre-registered T16-50 holdout.

## Conclusion

On the measured real Kalshi history, no NO-only taker band or window has a
positive clustered lower bound. Do not widen the band, enable live, or
relax risk limits based on this study. The bot must stay dry until T16-50
and T16-13 provide an executable-depth dataset and the pre-registered
holdout prove otherwise.
