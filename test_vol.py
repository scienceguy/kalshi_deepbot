import binance_feed
import gates

# Test composite_price
px = binance_feed.composite_price('BTCUSDT', timeout=10)
print('BTC composite price:', px)

# Test sigma_per_minute
sig = binance_feed.sigma_per_minute('BTCUSDT', lookback=60)
print('BTC sigma_per_minute:', sig)

# Test composite_sigma
csig = binance_feed.composite_sigma('BTCUSDT', lookback=60, timeout=10)
print('BTC composite_sigma:', csig)

# Test gates.sigma_per_min
gsig = gates.sigma_per_min('BTC')
print('gates.sigma_per_min BTC:', gsig)

# Test gates.spot_for
spot = gates.spot_for('BTC')
print('gates.spot_for BTC:', spot)
