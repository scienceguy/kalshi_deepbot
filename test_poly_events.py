import os, json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client

client = _get_authenticated_client()
events = client.events.list()
ev_list = events.get('events', [])
print("total events:", len(ev_list))
for e in ev_list[:50]:
    slug = e.get('slug', '')
    title = e.get('title', '')[:60]
    if 'updown' in slug.lower() or 'btc' in slug.lower() or 'bitcoin' in slug.lower():
        print(f"  slug={slug} | title={title}")
        # print markets
        mks = e.get('markets', [])
        for m in mks[:3]:
            print(f"    market: slug={m.get('slug')} question={m.get('question','')[:50]}")
