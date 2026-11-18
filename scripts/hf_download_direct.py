#!/usr/bin/env python3
"""Direct HTTPS downloader for HF parquet datasets (bypasses httpx2 bug)."""
import os, sys, json, concurrent.futures as cf
import requests

REPO = "papylove/kalshi-15m-data"
DEST = "/opt/superbot/hf_data"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def list_files():
    r = requests.get(f"https://huggingface.co/api/datasets/{REPO}", timeout=60)
    r.raise_for_status()
    sib = r.json().get("siblings", [])
    return [s["rfilename"] for s in sib if s["rfilename"].endswith(".parquet")]

def dl(rel):
    out = os.path.join(DEST, rel)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return (rel, "skip", os.path.getsize(out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    url = f"{BASE}/{rel}"
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                tmp = out + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                os.replace(tmp, out)
                return (rel, "ok", os.path.getsize(out))
        except Exception as e:
            if attempt == 2:
                return (rel, f"ERR {e}", 0)
    return (rel, "ERR", 0)

def main():
    files = list_files()
    print(f"files: {len(files)}", flush=True)
    done = 0; errs = 0
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for rel, status, size in ex.map(dl, files):
            if status.startswith("ERR"):
                errs += 1
                print(f"FAIL {rel}: {status}", flush=True)
            else:
                done += 1
                if done % 25 == 0:
                    print(f"progress {done}/{len(files)}", flush=True)
    print(f"DONE ok={done} errs={errs}", flush=True)

if __name__ == "__main__":
    main()
