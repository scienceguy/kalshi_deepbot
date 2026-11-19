import sys
sys.argv = ["probe"]
import config
from kalshi_client import KalshiClient
from market_finder import _fetch_orderbook, _parse_api_time, _build
from datetime import datetime, timezone

c = KalshiClient()
for s in ("KXBTC15M", "KXETH15M", "KXSOL15M"):
    for mkt in (c.get_markets(series_ticker=s, limit=3) or []):
        ct = _parse_api_time(mkt.get("close_time", ""))
        ml = (ct - datetime.now(timezone.utc)).total_seconds() / 60 if ct else 0
        ob = _fetch_orderbook(c, mkt["ticker"])
        b = _build(mkt, s, ct, ml, ob)
        print(f"{mkt['ticker']:<34} min={ml:5.1f} ya={b.yes_ask} na={b.no_ask} "
              f"yl={b.yes_liquidity} nl={b.no_liquidity} ob={bool(ob)}")
print("band", config.ENTRY_BAND_MIN, config.ENTRY_BAND_MAX,
      "window", config.ENTRY_WINDOW_START_MIN, config.ENTRY_WINDOW_END_MIN)
