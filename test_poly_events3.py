import os, json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client

client = _get_authenticated_client()
offset = 0
limit = 100
found_btc = []
found_updown = []
while True:
    events = client.events.list({"limit": limit, "offset": offset})
    ev_list = events.get('events', [])
    if not ev_list:
        break
    for e in ev_list:
        slug = e.get('slug', '') or ''
        title = e.get('title', '') or ''
        if 'btc' in slug.lower() or 'bitcoin' in slug.lower():
            found_btc.append((slug, title))
        if 'updown' in slug.lower() or 'up-down' in slug.lower():
            found_updown.append((slug, title))
    print(f"offset={offset} got={len(ev_list)} btc={len(found_btc)} updown={len(found_updown)}")
    if len(ev_list) < limit:
        break
    offset += limit

print(f"\nTotal btc markets: {len(found_btc)}")
for slug, title in found_btc[:20]:
    print(f"  {slug} | {title[:60]}")
print(f"\nTotal updown markets: {len(found_updown)}")
for slug, title in found_updown[:20]:
    print(f"  {slug} | {title[:60]}")
