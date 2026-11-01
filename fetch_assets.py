"""Download the images embedded in articles and record where each one went.

The Markdown archive was built from the article record JSON, where every image
block carries a previewUrl and a caption. The captions are already in the .md
files, but the images were dropped. This script walks the same URL list again,
collects those previewUrl values, downloads them into articles/_assets/ and
writes an index that link_assets.py can use to place them in the Markdown.

Files are named by the md5 of their bytes, so an image reused across many
articles is stored once. The first two hex characters become a subdirectory,
which keeps any single folder from holding tens of thousands of files.

Images are kept in the format the site serves (webp); nothing is converted.

Usage:
  python fetch_assets.py --limit 20      # trial run
  python fetch_assets.py                 # full run, resumable
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import novaya

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "articles", "_assets")
INDEX = os.path.join(HERE, "assets_map.json")
STATE = os.path.join(HERE, "assets_state.json")

_lock = threading.RLock()
_counts = {"new": 0, "dup": 0, "fail": 0, "noimg": 0, "articles": 0}
_map = {}       # article url -> list of {local, caption, remote, w, h}
_done = {}      # article url -> True


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                d = json.load(f)
            _map.update(d.get("map") or {})
            _done.update(d.get("done") or {})
        except ValueError:
            pass


def save_state():
    tmp = STATE + ".tmp"
    with _lock:
        payload = json.dumps({"map": _map, "done": _done}, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, STATE)


def asset_path(data):
    """Content-addressed path: _assets/ab/ab12cd34....webp"""
    digest = hashlib.md5(data).hexdigest()
    return digest, os.path.join(ASSETS, digest[:2], digest + ".webp")


def absolute(url):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return novaya.BASE + url
    return url


def download(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                absolute(url),
                headers={"User-Agent": novaya.UA,
                         "Referer": novaya.BASE + "/"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def handle_article(fetcher, art_url):
    status, body = fetcher.get(art_url, allow_404=True)
    if status != 200:
        return art_url, [], "http %s" % status
    record = novaya.parse_record(body)
    if not record:
        return art_url, [], "no record"

    entries = []
    for block in record.get("body") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if not (isinstance(kind, str) and kind.startswith("image")):
            continue
        remote = block.get("previewUrl")
        if not isinstance(remote, str) or not remote:
            continue
        caption = novaya.html2md(block.get("data")) if isinstance(block.get("data"), str) else ""
        caption = re.sub(r"\s+", " ", caption or "").strip()
        entries.append({"remote": absolute(remote), "caption": caption,
                        "w": block.get("width"), "h": block.get("height")})
    return art_url, entries, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--rate", type=float, default=12.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--urls", default=os.path.join(HERE, "urls.txt"))
    args = ap.parse_args()

    os.makedirs(ASSETS, exist_ok=True)
    load_state()
    urls = [l.strip() for l in open(args.urls, encoding="utf-8") if l.strip()]
    todo = [u for u in urls if u not in _done]
    if args.limit:
        todo = todo[:args.limit]
    sys.stderr.write("%d urls, %d already scanned, %d to scan\n"
                     % (len(urls), len(_done), len(todo)))

    fetcher = novaya.Fetcher(rate=args.rate, timeout=45)
    started = time.time()
    index_lock = threading.Lock()
    processed = [0]

    def work(art_url):
        art, entries, err = handle_article(fetcher, art_url)
        with index_lock:
            processed[0] += 1
            n = processed[0]
        if not entries:
            with _lock:
                _counts["noimg"] += 1
                _done[art] = True
        else:
            resolved = []
            for e in entries:
                data = download(e["remote"])
                if not data:
                    with _lock:
                        _counts["fail"] += 1
                    continue
                digest, path = asset_path(data)
                with _lock:
                    if os.path.exists(path):
                        _counts["dup"] += 1
                    else:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        tmp = path + ".part"
                        with open(tmp, "wb") as f:
                            f.write(data)
                        os.replace(tmp, path)
                        _counts["new"] += 1
                rel = os.path.relpath(path, os.path.dirname(ASSETS)).replace("\\", "/")
                resolved.append({"local": rel, "caption": e["caption"],
                                 "w": e["w"], "h": e["h"]})
            with _lock:
                _map[art] = resolved
                _done[art] = True
                _counts["articles"] += 1
        with _lock:
            total = sum(_counts.values())
            if n % 200 == 0:
                rate = n / max(0.001, time.time() - started)
                sys.stderr.write(
                    "\r  %6d/%d  img new=%d dup=%d fail=%d  noimg=%d  %.1f/s   "
                    % (n, len(todo), _counts["new"], _counts["dup"],
                       _counts["fail"], _counts["noimg"], rate))
                sys.stderr.flush()
        if n % 2000 == 0:
            save_state()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(work, todo))
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
    finally:
        save_state()

    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(_map, f, ensure_ascii=False)
    sys.stderr.write("\n\nscanned %d articles\n" % _counts["articles"])
    sys.stderr.write("images: new=%d duplicate=%d failed=%d\n"
                     % (_counts["new"], _counts["dup"], _counts["fail"]))
    sys.stderr.write("articles without images: %d\n" % _counts["noimg"])
    total = 0
    for root, _, files in os.walk(ASSETS):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    sys.stderr.write("assets on disk: %d files, %.1f MB\n"
                     % (_counts["new"], total / 1048576))


if __name__ == "__main__":
    main()
