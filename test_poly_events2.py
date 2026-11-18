import os, json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client

client = _get_authenticated_client()
offset = 0
limit = 100
found = []
while True:
    events = client.events.list({"limit": limit, "offset": offset, "active": True})
    ev_list = events.get('events', [])
    if not ev_list:
        break
    for e in ev_list:
        slug = e.get('slug', '') or ''
        title = e.get('title', '') or ''
        if 'updown' in slug.lower() or 'up-down' in slug.lower():
            found.append((slug, title))
    print(f"offset={offset} got={len(ev_list)} cumulative_found={len(found)}")
    if len(ev_list) < limit:
        break
    offset += limit

print(f"\nTotal updown markets found: {len(found)}")
for slug, title in found[:30]:
    print(f"  {slug} | {title[:60]}")
