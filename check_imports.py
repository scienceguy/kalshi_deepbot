import binance_feed, gates, trader, config, polymarket_us_client
print('all modules import OK')
print('POLYMARKET_ENABLED =', config.POLYMARKET_ENABLED)
print('start_ws_streams exists:', hasattr(binance_feed, "start_ws_streams"))
print('spot_price exists:', hasattr(binance_feed, "spot_price"))
