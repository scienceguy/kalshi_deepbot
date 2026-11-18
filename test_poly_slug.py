import os, json
os.environ['BW_DIR'] = os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import _get_authenticated_client

client = _get_authenticated_client()

# Try the cpc-btc-updown-15m slug pattern
for slug in ["cpc-btc-updown-15m", "cpc-btc-updown-15m-2026-11-14", "cpc-eth-updown-15m"]:
    try:
        ev = client.events.retrieve_by_slug(slug)
        mks = ev.get("event", {}).get("markets", [])
        print(f"slug={slug}: FOUND {len(mks)} markets")
        for m in mks[:2]:
            print(f"  market slug={m.get('slug')} question={m.get('question','')[:60]}")
            print(f"    outcomes={m.get('outcomes')} clobTokenIds={m.get('clobTokenIds')}")
    except Exception as e:
        print(f"slug={slug}: ERROR {type(e).__name__}: {str(e)[:150]}")
