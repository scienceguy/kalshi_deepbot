# Polymarket US fee schedule (confirmed)

Source: https://docs.polymarket.us/fees, effective 12:00 AM ET Sat 2026-11-14.
Confirmed by the operator 2026-11-14 (US endpoint, not the international
docs.polymarket.com schedule that the repo previously cited).

## Taker fee

Formula: `Fee = Theta x C x p x (1 - p)` (C = contracts, p in $0.01-$0.99),
symmetric around p=0.50. `Theta` by trailing 30-day taker volume (preceding
ET calendar days, excluding today):

| Tier | Trailing 30-day taker volume | Theta |
|------|------------------------------|-------|
| 1    | < $10,000                    | 0.095 |
| 2    | $10,000 - < $50,000          | 0.085 |
| 3    | $50,000 - < $250,000         | 0.075 |
| 4    | >= $250,000                  | 0.0695 |

New-account onboarding: Day 1 = ET day of first fill. Days 1-7 use Theta =
0.0695 regardless of volume. Days 8-30 use the higher of (first-7-day volume
x 30/7) and trailing 30-day volume. Day 31 onward: trailing 30-day volume.
Maker/taker rebate tiers use trailing 30-day volume always. Manual overrides
take precedence.

## Makers

Makers never pay fees. Maker rebates by trailing 30-day maker volume:
Tier 1 none, Tier 2 -0.0035, Tier 3 -0.0070, Tier 4 -0.0104 (Tier >= 2 only).

## Rounding

Fees rounded to the nearest cent with banker's rounding (round half to even);
tiny or near-extreme trades can round to $0.00. Collected at fill; never on
cancel/expire.

## Combos

Separate curve, not modelled here:
`Fee = C x p x [Theta x (1-p) + 0.06 x (1-p)^4]`; maker 0.03 x C x p x (1-p);
no maker rebates.

## Implementation

`config.PM_FEE_MODEL` = `us_tier` (default) or `legacy_rate` (old
international constant via `PM_FEE_RATE`). Tier Theta via
`config.PM_TAKER_COEFFICIENT` (default 0.095, Tier 1 — conservative until
`scripts/fee_probe.py` confirms the account's real tier). Rounding helper:
`poly_us_trader._round_cents_half_even`.
