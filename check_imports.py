"""Dev convenience: verify the import-heavy modules still import cleanly."""
import binance_feed
import config
import gates  # noqa: F401 — imported to prove the module imports
import trader  # noqa: F401 — imported to prove the module imports
import polymarket_us_client  # noqa: F401 — imported to prove the module imports

print('all modules import OK')
print('POLYMARKET_ENABLED =', config.POLYMARKET_ENABLED)
print('start_ws_streams exists:', hasattr(binance_feed, "start_ws_streams"))
print('spot_price exists:', hasattr(binance_feed, "spot_price"))
