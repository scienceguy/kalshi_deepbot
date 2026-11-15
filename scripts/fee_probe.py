"""Compare the venue's real taker fee against our model (A4).

Owner runs this with network access. Uses orders.preview on a few sample
orders; if the preview response carries a fee field, prints the venue's
number next to _taker_fee's model and the implied Theta.

Usage:
    python scripts/fee_probe.py [market_slug]

The venue preview must be a TAKER (marketable) order, so
participateDontInitiate is deliberately False here.
"""

import sys

import config
import polymarket_us_client as poly
from poly_us_trader import _taker_fee


def _find_fees(obj, path=""):
    """Recursively collect (path, number) for every fee-ish key in the response."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "fee" in str(k).lower() and isinstance(v, (int, float)):
                found.append((f"{path}.{k}", float(v)))
            else:
                found.extend(_find_fees(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_find_fees(item, f"{path}[{i}]"))
    return found


def _implied_theta(fee, contracts, price):
    denom = contracts * price * (1.0 - price)
    return fee / denom if denom > 0 else None


def main(slug: str) -> int:
    samples = [
        ("yes", 0.95, 100),
        ("yes", 0.50, 100),
        ("yes", 0.05, 100),
    ]
    theta = float(getattr(config, "PM_TAKER_COEFFICIENT", 0.095))
    print(f"Model: PM_FEE_MODEL={config.PM_FEE_MODEL} Theta={theta} "
          f"(source docs.polymarket.us/fees, eff 2026-11-14)")
    rc = 0
    for side, price, contracts in samples:
        model_fee = _taker_fee(contracts, price)
        venue_fee = None
        try:
            client = poly._get_authenticated_client()
            intent = ("ORDER_INTENT_BUY_LONG" if side == "yes"
                      else "ORDER_INTENT_BUY_SHORT")
            resp = client.orders.preview({"request": {
                "marketSlug": slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": poly._poly_amount(poly._poly_price(side, price)),
                "quantity": contracts,
                "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                "participateDontInitiate": False,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            }})
            fees = _find_fees(resp)
            if fees:
                venue_fee = fees[0][1]
                print(f"  venue fee fields: {fees}")
            else:
                print(f"  preview OK but no fee field in response keys: "
                      f"{list(resp) if isinstance(resp, dict) else type(resp)}")
        except Exception as e:
            print(f"  preview failed for {side} {price} x{contracts}: {e}")
            rc = 2
        implied = _implied_theta(venue_fee, contracts, price) if venue_fee is not None else None
        print(f"  {side.upper()} {price} x{contracts}: "
              f"model={model_fee:.4f} venue={venue_fee} "
              f"implied_Theta={implied} diff="
              f"{(venue_fee - model_fee) if venue_fee is not None else 'n/a'}")
    if rc == 2:
        print("NOTE: no network/preview data — cannot confirm the tier; "
              "Tier 1 Theta=0.095 stays the conservative default.")
    return rc


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "btc-15m"
    raise SystemExit(main(slug))
