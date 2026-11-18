import os, json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client

client = _get_authenticated_client()

# Check total count from events.list
events = client.events.list({"limit": 1})
print("events.list keys:", list(events.keys()))
print("sample:", json.dumps(events, default=str)[:500])

# Try markets.list with no filter to see all market types
mkts = client.markets.list({"limit": 20})
mkt_list = mkts.get('markets', [])
print("\nmarkets.list count:", len(mkt_list))
for m in mkt_list:
    slug = m.get('slug', '') or ''
    question = m.get('question', '') or ''
    print(f"  slug={slug} | q={question[:50]}")
