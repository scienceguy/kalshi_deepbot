# T16-03 independent gate review

Date: 2026-11-22
Reviewer role: independent review subagent. No source, test, or config edits; no orders, process changes, commit, or push.

What this change does: commit 81d71cd keys the measured floor to own-side price and adds a NO complement. Commit cd45df2 makes ATR-to-sigma V2 the default. This review checks the actual behavior and reproducible calibration evidence.

## Verdicts

| Finding | Verdict | Reason |
|---|---|---|
| F-04 measured floor | CHANGES REQUIRED | The NO longshot defect is avoided, but `1 - 0.9928 = 0.0072` is not a measured NO-favorite floor. The loaded measurement supports only YES 95-99c. Keep the floor disabled for NO. |
| F-05 sigma V2 | CHANGES REQUIRED | V2 improves pooled error and Brier score, but BTC worsens on the chronological evaluation slice. BNB, NEAR, and ZEC are NOT MEASURED locally. The global default is not validated. |

The current effective flags are `GATES_MEASURED_FLOOR_ENABLED=False`, `GATES_SIGMA_FROM_ATR_V2=True`, `DRY_RUN=True`, `NO_ONLY=True`, `ALLOW_NO_BUYS=True`, and `AGGRESSIVE_FILL=False`. Thus the NO-floor defect is latent while the floor is off. Sigma V2 is active on the ATR fallback path. The current maker path bypasses these gates.

## Review scope and snapshots

Reviewed `AGENT_TASKSv16.md` F-04/F-05/T16-03; current `gates.py`, `config.py`, the three requested test files; commits 304d8f8, 81d71cd, 711ca47, and cd45df2; and the calibration scripts and local data. HEAD moved concurrently from 8e6ecd90da2f51c1a889f34b97ef99f181c3e26e to 98ac689736a83a5c5169c64d3cf1a8abd6daacaf and then 6be979e8edb6e0b8336531eee74bb387b4552e4c. Use the file hashes below rather than assuming a quiescent tree.

- `gates.py`: `53a2c3d655772083c016ec7dd831127f5ccc1ce77be35ea599ad5e83da05673e`
- `tests/test_gates.py`: `a09f215010ba5e5ca8b6d3b24ba7bd2d34565ebaf91c77c52ed2536523989e53`
- `tests/test_gates_sigma_v2.py`: `4fc8d22b947186990347cfed26773bd7dbec7d7057b42d10aadaa7413ca77293`
- `tests/test_q_floor_holdout.py`: `1a2708acc97bc0ebe600addeba619ddb4f8ee8d3360031591c0e44d4861be393`
- `scripts/gates_sigma_validation.py`: `14a2fefdcaac1e3be6b894a72cb07a0e1c6f1ad13a1a67055416c5dcae0d9672`

These matched HEAD at the comparison. The pre-existing config worktree diff was outside the gate settings reviewed here.

MemPalace was searched before work for `T16-03 gate review NO floor sigma v2`. Prior prose was treated as a lead, not evidence. This review was not written to MemPalace by the reviewer.

## F-04 evidence

### Actual four-corner behavior

The focused suite and the real `TestMeasuredFloorFourCorners._run_floor_test` helper produced:

| Side and own price | q returned | Floor flag and source | Assessment |
|---|---:|---|---|
| YES 97c | 0.992800000000 | True, `config:GATES_MEASURED_Q` | Expected YES-band behavior. |
| YES 3c | 0.967921106004 | None | Correctly outside the band. |
| NO 97c | 0.969304604219 | True, `config:GATES_MEASURED_Q (NO complement)` | Incorrect NO-floor value; model q is unchanged. |
| NO 3c | 0.032078893996 | None | Correctly outside the band; the old longshot inflation is avoided. |

At `gates.py:633-648`, own-side band membership is correct. The NO value is not. `GATES_MEASURED_Q` estimates `P(YES wins | YES quote is 95-99c)`. Its complement estimates a YES-favorite loss probability under that same conditioning. It does not estimate `P(NO wins | NO quote is 95-99c)`, which is conditioned on the complementary longshot price region. The code comment claiming about 0.989 and the implemented 0.0072 cannot both be true.

The tests do not catch this. `TestMeasuredFloorSide.test_no_favorite_in_band_gets_no_floor` expects the complement marker and q=0.50, not a measured NO win probability. `TestMeasuredFloorFourCorners.test_floor_no_favorite_in_band` even documents the 0.989/0.0072 contradiction but asserts only model q>0.9. Its NO-longshot test has no assertion; the separate `TestMeasuredFloorSide` test supplies that missing assertion. `tests/test_q_floor_holdout.py` exercises YES only and explicitly excludes NO 97c from the YES-equivalent 95-99c sample.

### Available floor data

Read-only calls to `scripts/q_floor_holdout.py` functions found:

- `local_data/superbot_local.db`: 19 deduplicated settled fills, zero YES-band holdout rows, zero NO-favorite rows.
- `local_data/superbot_local_dry.db`: 8 fills, zero in each floor population.
- `local_data/superbot_local_live.db`: zero fills.
- `local_data/q_floor.json`: absent.

Therefore the configured/fill-derived evidence supports no promoted floor at all, and specifically no own-side NO floor. If the file is absent and a user opts in, `_measured_q_floor` falls back to the old in-sample YES quote constant. That is another reason the global floor must stay off.

Required correction: remove or explicitly disable the NO complement branch. Add a NO floor only from a separately measured own-side NO holdout with a Wilson lower bound, adequate independent market count, and numeric tests. Do not infer it by complementing the YES-favorite statistic. The smallest safe change is YES-only or no floor; this review makes no source change.

F-04 verdict: CHANGES REQUIRED.

## F-05 evidence

### Problems in the submitted validation

`scripts/gates_sigma_validation.py` fetches a moving window ending at `datetime.now`; it has no local-input or fixed-time option. The raw November 22 candles behind the commit's 1.822x/0.909x and related claims are not stored as a report or dataset here, so those exact claims are NOT REPRODUCED. The script also documents realized standard deviation but computes each next-minute absolute log return times `sqrt(pi/2)`, then takes ratios. It emits no Brier score. `tests/test_gates_sigma_v2.py` uses synthetic GBM candles, useful for a unit test but not real-data promotion evidence. `tests/test_gates.py` repeats the prose ratios without measuring them.

### Independent real-data method

Inputs were the already-local recorded production Parquet files from `papylove/kalshi-15m-data` (see `scripts/hf_download_direct.py`):

- `hf_data/spot_history/YYYY-MM-DD.parquet`, 14 dates from 2026-09-28 through 2026-10-11.
- `hf_data/quote_history/YYYY-MM-DD.parquet`, the same dates.
- Dataset manifest SHA256: `a3180c25379eae7d0a5aa676edc33f99be0ab87d5050be2ba9e6fe177746f07e`. The manifest hashes each per-file `<sha256>  <path>\n` line, spot files first, then quote files.

No network fetch, synthesis, interpolation, or forward fill was used. For each asset, 15 consecutive exact 60-second rows supplied 14 Wilder true ranges and 14 close-to-close log returns. ATR is the mean true range. Realized sigma is the sample standard deviation (`ddof=1`) of those same 14 returns. V1 is `(ATR/close)*sqrt(pi/2)` and V2 is `(ATR/close)/1.6`. Any gap or nonpositive value excludes the window.

The locally shared gate assets are BTC, ETH, SOL, XRP, DOGE, and HYPE. BNB, NEAR, and ZEC are NOT MEASURED because corresponding local spot candles are absent. There are 112,991 valid continuous windows. No duplicate coin/timestamp rows were present. Quotes join candles only on exact `(coin, end_period_ts=ts)`; 210 null strikes are excluded, leaving 103,980 scored quotes in 7,470 markets.

For Brier scoring, minutes left is `(close_ts-end_period_ts)/60`; nonpositive times are excluded. The forecast exactly isolates Gate 3's ATR path: `p=normal_cdf(log(close/floor_strike)/(sigma*sqrt(minutes_left)))`. Outcome is the recorded YES settlement. Score is `mean((p-outcome)^2)`. The clustered score first averages within each ticker, then across tickers. No floor, momentum, fill, or fee adjustment is added. The chronological evaluation slice is 2026-10-09 through 2026-10-11. It is not claimed to be a pristine pre-registered holdout because this dataset has already supported other repository research.

### Sigma bias and error, all 14 dates

Bias below is `mean(estimate)/mean(realized)-1`, not a mean of per-row ratios. MAE and RMSE are in fractional log-return units per square-root minute. Values are rounded only for display; the reproduction command prints full precision.

| Asset | n | V1 bias | V2 bias | V1 MAE | V2 MAE | V1 RMSE | V2 RMSE |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTC | 18,831 | +0.477674922626 | -0.263116245844 | 0.000179357978 | 0.000098818234 | 0.000260149267 | 0.000124512993 |
| ETH | 18,835 | +0.613110013049 | -0.195577766070 | 0.000274772176 | 0.000097572622 | 0.000378558995 | 0.000140410451 |
| SOL | 18,835 | +0.757819022245 | -0.123414588416 | 0.000467582791 | 0.000103366539 | 0.000557744733 | 0.000148392755 |
| XRP | 18,835 | +0.743862365550 | -0.130374463977 | 0.000496003673 | 0.000115507201 | 0.000628346155 | 0.000186290900 |
| DOGE | 18,820 | +0.731111459545 | -0.136733058375 | 0.000537716368 | 0.000129832705 | 0.000677573993 | 0.000199528224 |
| HYPE | 18,835 | +0.700252432043 | -0.152121771878 | 0.000539982524 | 0.000141309414 | 0.000623117249 | 0.000195257350 |
| Pooled | 112,991 | +0.691153727410 | -0.156659094347 | 0.000415894788 | 0.000114399622 | 0.000542245464 | 0.000168262591 |

Pooled mean error improves from +0.000414359152132 to -0.000093919958662. The pooled median estimator/realized ratio moves from 1.649699367799 to 0.822668534708. V2 therefore reduces error but systematically underestimates this trailing realized measure; 1.6 is not proven universally calibrated.

### Brier before and after

Negative delta (`V2 minus V1`) favors V2.

| Slice | Rows / markets | V1 | V2 | Delta | Market-cluster V1 -> V2 |
|---|---:|---:|---:|---:|---:|
| All dates, all matched quotes | 103,980 / 7,470 | 0.157104150253 | 0.150008543442 | -0.007095606812 | 0.157091678998 -> 0.150050378761 |
| Eval, all assets | 17,374 / 1,242 | 0.156141615211 | 0.150213069706 | -0.005928545505 | 0.156178780330 -> 0.150229066887 |
| Eval BTC | 2,898 / 207 | 0.173781747082 | 0.186008248901 | +0.012226501820 | same |
| Eval ETH | 2,898 / 207 | 0.146400679574 | 0.136581373933 | -0.009819305640 | same |
| Eval SOL | 2,898 / 207 | 0.154366578363 | 0.143147278229 | -0.011219300134 | same |
| Eval XRP | 2,898 / 207 | 0.163779100303 | 0.159354152590 | -0.004424947714 | same |
| Eval DOGE | 2,884 / 207 | 0.155254678643 | 0.147179495989 | -0.008075182654 | 0.155481954074 -> 0.147290134018 |
| Eval HYPE | 2,898 / 207 | 0.143262622583 | 0.128993213648 | -0.014269408935 | same |
| Eval YES 95-99c, elapsed minute 6-11 | 636 / 287 | 0.024485020326 | 0.009273450790 | -0.015211569537 | 0.032900272137 -> 0.019333501281 |
| Eval NO 95-99c, elapsed minute 6-11 | 504 / 229 | 0.020310455719 | 0.008129019381 | -0.012181436337 | 0.021926315225 -> 0.009008097291 |

The positive pooled and favorite-band results support further paper study. They do not erase the BTC regression, and no result exists for BNB, NEAR, or ZEC. The submitted commit has no reproducible Brier table of its own. A single global default enabled for every asset is therefore not approved.

Required correction: keep or restore the global default off pending per-asset review, especially BTC, and real validation for the missing assets. Any narrower promotion needs its own recorded human/risk approval. Brier improvement alone does not meet T16-16: net-of-fee EV, a clustered lower bound, drawdown, and paper agreement were not measured.

F-05 verdict: CHANGES REQUIRED.

## Focused test evidence

Command (runtime files redirected to `/tmp/opencode`; production logging disabled in the test process):

```bash
PYTHONDONTWRITEBYTECODE=1 SUPERBOT_DATA_DIR=/tmp/opencode/t16-03-runtime \
GATES_LOG_FILE=/tmp/opencode/t16-03-runtime/gates_log.jsonl TMPDIR=/tmp/opencode \
/opt/superbot/.venv/bin/python -B - <<'PY'
import pytest
import gates
gates._write_log = lambda rec: None
raise SystemExit(pytest.main([
    '-q', '-p', 'no:cacheprovider',
    'tests/test_gates.py',
    'tests/test_gates_sigma_v2.py',
    'tests/test_q_floor_holdout.py',
]))
PY
```

Output: `98 passed in 2.26s`.

The four-corner output above came from the same test helper, invoked for `(yes,97,96000)`, `(yes,3,96000)`, `(no,97,94000)`, and `(no,3,96000)`, with `_write_log` disabled. This is actual gate execution, not a reading of test names. Green tests do not override the numeric NO-floor defect or the real-data BTC result.

## Remaining safety concerns

1. `GATES_MEASURED_FLOOR_ENABLED=False` is the only current protection from the floor logic. Do not enable it for NO as written. The absent-file YES fallback is also not holdout evidence.
2. The config comment says the four-corner fix justifies dry NO buying. The tests validate the wrong NO complement, so that rationale is not sound.
3. Sigma V2 applies only when no `sigma_per_sqrt_min`/`sigma_per_min` is supplied. A supplied composite sigma bypasses this conversion. `trader.py:3055-3063` also bypasses gates for maker mode, so this review says nothing about maker profitability.
4. The Brier analysis isolates raw Gate 3 probability. It is not an execution backtest and contains no fill, fee, latency, PnL, or drawdown claim.
5. Fourteen days of third-party recorded history are below the repository's 60-day data target. The chronological slice is not pristine, and overlapping volatility windows are descriptive, not independent samples.
6. No confidence interval or net-EV lower bound is claimed. The original commit's moving-window ratios remain NOT REPRODUCED, not silently replaced by new claims about November 22.

Overall verdict: fix F-04 and F-05 before approval. Do not promote the NO floor or the current global sigma default on the submitted evidence.

## Appendix A: calibration reproduction command

Run from `/opt/superbot`. This is the self-contained read-only command used for the reported tables; it writes no dataset or report files.

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/superbot/.venv/bin/python -B - <<'PY'
from pathlib import Path
import json
import numpy as np
import pandas as pd
from scipy.special import ndtr

DAYS = ['2026-09-28','2026-09-29','2026-09-30','2026-10-01',
        '2026-10-02','2026-10-03','2026-10-04','2026-10-05',
        '2026-10-06','2026-10-07','2026-10-08','2026-10-09',
        '2026-10-10','2026-10-11']
ASSETS = ['BTC','ETH','SOL','XRP','DOGE','HYPE']

def read_tree(kind, columns):
    return pd.concat([pd.read_parquet(Path('hf_data')/kind/(d+'.parquet'),
                                     columns=columns) for d in DAYS],
                     ignore_index=True)

s = read_tree('spot_history', ['coin','ts','open','high','low','close'])
s = s[s.coin.isin(ASSETS)].sort_values(['coin','ts']).reset_index(drop=True)
g = s.groupby('coin', group_keys=False)
s['prev_close'] = g.close.shift(1)
s['tr'] = np.maximum(s.high-s.low,
                     np.maximum((s.high-s.prev_close).abs(),
                                (s.low-s.prev_close).abs()))
s['ret'] = np.log(s.close)-np.log(s.prev_close)
s['gap60'] = g.ts.diff().eq(60)
s['atr'] = g.tr.transform(lambda x: x.rolling(14, min_periods=14).mean())
s['realized'] = g.ret.transform(
    lambda x: x.rolling(14, min_periods=14).std(ddof=1))
s['continuous'] = g.gap60.transform(
    lambda x: x.rolling(14, min_periods=14).sum()).eq(14)
s['v1'] = s.atr/s.close*np.sqrt(np.pi/2)
s['v2'] = s.atr/s.close/1.6
f = s[s.continuous & s.atr.gt(0) & s.realized.gt(0) & s.close.gt(0)].copy()

def bias_row(coin, x):
    r = x.realized.to_numpy()
    ans = {'coin': coin, 'n': len(x), 'realized_mean': r.mean()}
    for name in ['v1','v2']:
        e = x[name].to_numpy()
        err = e-r
        ans[name] = {'mean_error': err.mean(),
                     'mean_ratio_minus_1': e.mean()/r.mean()-1,
                     'MAE': np.abs(err).mean(),
                     'RMSE': np.sqrt(np.mean(err**2)),
                     'median_estimator_over_realized': np.median(e/r)}
    return ans

print('BIAS_FULL')
for coin, x in list(f.groupby('coin')) + [('ALL',f)]:
    print(json.dumps(bias_row(coin,x), sort_keys=True))

q = read_tree('quote_history',
              ['coin','ticker','open_ts','close_ts','end_period_ts','minute',
               'yes_bid','yes_ask','result','floor_strike'])
d = q.merge(f, left_on=['coin','end_period_ts'], right_on=['coin','ts'],
            how='inner', validate='many_to_one')
d = d[d.floor_strike.gt(0) & d.close.gt(0) & d.result.isin(['yes','no'])].copy()
d['minutes_left'] = (d.close_ts-d.end_period_ts)/60.0
d = d[d.minutes_left.gt(0)].copy()
d['outcome'] = d.result.eq('yes').astype(float)
z = np.log(d.close/d.floor_strike)
t = np.sqrt(d.minutes_left)
d['b1'] = (ndtr(z/(d.v1*t))-d.outcome)**2
d['b2'] = (ndtr(z/(d.v2*t))-d.outcome)**2
d['date'] = pd.to_datetime(d.end_period_ts, unit='s', utc=True).dt.strftime('%Y-%m-%d')

def brier_row(label, x):
    by_market = x.groupby('ticker')[['b1','b2']].mean()
    return {'slice': label, 'rows': len(x), 'markets': len(by_market),
            'v1': x.b1.mean(), 'v2': x.b2.mean(),
            'delta_v2_minus_v1': x.b2.mean()-x.b1.mean(),
            'market_cluster_v1': by_market.b1.mean(),
            'market_cluster_v2': by_market.b2.mean(),
            'market_cluster_delta': by_market.b2.mean()-by_market.b1.mean()}

print('BRIER')
print(json.dumps(brier_row('FULL_ALL',d), sort_keys=True))
h = d[d.date >= '2026-10-09']
print(json.dumps(brier_row('EVAL_2026-10-09_TO_11_ALL',h), sort_keys=True))
for coin, x in h.groupby('coin'):
    print(json.dumps(brier_row('EVAL_'+coin,x), sort_keys=True))
fav = h.minute.between(6,11)
print(json.dumps(brier_row('EVAL_YES_95_99_MINUTE_6_11',
                           h[fav & h.yes_ask.between(.95,.99)]), sort_keys=True))
print(json.dumps(brier_row('EVAL_NO_95_99_MINUTE_6_11',
                           h[fav & (1-h.yes_bid).between(.95,.99)]), sort_keys=True))
PY
```

## Final verification

- Final HEAD observed during verification: `e2f1b6a0130088c25b9cc12c3ee06578a4df2200`; concurrent agents continued advancing it. The five hashed review files still matched their working-tree contents and had no reported modifications.
- `LC_ALL=C grep -n '[^ -~]' docs/T16_GATE_REVIEW.md`: no matches, exit 1 as expected for this check.
- `/opt/superbot/.venv/bin/python -B scripts/verify_source.py --all`: `all clean`, exit 0.
- The only project-tree file created by this reviewer is `docs/T16_GATE_REVIEW.md`; disposable test/analysis runtime files were redirected to `/tmp/opencode`. The other reported config modification predated this review and was not touched.

## Independent reproduction addendum

A second read-only reviewer run on 2026-11-22 reproduced the evidence independently rather than relying on the report prose:

- Focused tests: `98 passed in 2.27s` with `/opt/superbot/.venv/bin/python`.
- The four measured-floor corner outputs matched the table above. A separate NO 97c run with model q=0.001 returned q=0.007200000000 and source `config:GATES_MEASURED_Q (NO complement)`, directly confirming that the implemented complement is not a NO-favorite measurement.
- The dataset manifest SHA256 matched `a3180c25379eae7d0a5aa676edc33f99be0ab87d5050be2ba9e6fe177746f07e`. Independent recomputation produced 112,991 valid sigma windows and 103,980 scored quotes in 7,470 markets; the printed sigma and Brier results reproduced to full displayed precision.
- BNB, NEAR, and ZEC remain NOT MEASURED. The separate verdicts remain F-04 CHANGES REQUIRED and F-05 CHANGES REQUIRED.

## Post-fix status

The reviewer findings were corrected in commit `52eb7e7`: the NO-side
complement floor was removed, and `GATES_SIGMA_FROM_ATR_V2` was reverted to
`False` pending per-asset promotion evidence. After the fix, focused tests
reported 98 passed, and all source gates reported clean. No live order,
balance read, credential, or bot-process change was made.
