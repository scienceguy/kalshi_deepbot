import os, json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client

client = _get_authenticated_client()

# Try to get a sample event to see structure
events = client.events.list({"limit": 5})
ev_list = events.get('events', [])
for e in ev_list:
    slug = e.get('slug', '') or ''
    title = e.get('title', '') or ''
    cat = e.get('category', '') or e.get('categories', [])
    print(f"slug={slug} | title={title[:50]} | category={cat}")

# Try markets.list to see what's available
mkts = client.markets.list({"limit": 5})
mkt_list = mkts.get('markets', [])
print("\n--- Sample markets ---")
for m in mkt_list:
    slug = m.get('slug', '') or ''
    question = m.get('question', '') or ''
    print(f"slug={slug} | question={question[:60]}")
