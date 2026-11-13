#!/usr/bin/env python3
"""Generic direct-HTTPS downloader for an HF dataset (bypasses the httpx2 bug).

Usage: python scripts/hf_download_repo.py <repo_id> <dest_dir> [--ext .csv]
"""
import os
import sys
import concurrent.futures as cf

import requests

repo = sys.argv[1]
dest = sys.argv[2]
exts = tuple(a for a in sys.argv[3:] if a.startswith(".")) or (".csv", ".parquet", ".json", ".jsonl")
BASE = f"https://huggingface.co/datasets/{repo}/resolve/main"


def list_files():
    r = requests.get(f"https://huggingface.co/api/datasets/{repo}?full=true", timeout=60)
    r.raise_for_status()
    # 'siblings' may not carry size; use the tree API for robustness
    r2 = requests.get(f"https://huggingface.co/api/datasets/{repo}/tree/main", timeout=60)
    out = []
    if r2.ok:
        for it in r2.json():
            if it.get("type") == "file" and it["path"].endswith(exts):
                out.append(it["path"])
    if not out:
        out = [s["rfilename"] for s in r.json().get("siblings", [])
               if s["rfilename"].endswith(exts)]
    return out


def dl(rel):
    out = os.path.join(dest, rel)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return (rel, "skip", os.path.getsize(out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    for attempt in range(3):
        try:
            with requests.get(f"{BASE}/{rel}", stream=True, timeout=180, allow_redirects=True) as r:
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
    print(f"repo={repo} files={len(files)}", flush=True)
    ok = err = 0
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for rel, status, size in ex.map(dl, files):
            if status.startswith("ERR"):
                err += 1
                print("FAIL", rel, status, flush=True)
            else:
                ok += 1
    print(f"DONE ok={ok} errs={err}", flush=True)


if __name__ == "__main__":
    main()
