import os
import json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client, get_market_by_slug

client = _get_authenticated_client()

# Try to find a BTC updown 15m market
# List events to find updown markets
try:
    events = client.events.list()
    print("events.list type:", type(events))
    print("events keys:", list(events.keys()) if isinstance(events, dict) else "not dict")
except Exception as e:
    print("events.list error:", type(e).__name__, str(e)[:200])

# Try a known slug pattern
for slug in ["btc-updown-15m-2026-11-14", "bitcoin-updown-15m"]:
    try:
        m = get_market_by_slug(slug)
        print(f"\nSlug {slug}:", json.dumps(m, default=str)[:500])
    except Exception as e:
        print(f"\nSlug {slug} error:", type(e).__name__, str(e)[:150])
