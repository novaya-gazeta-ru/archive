"""Download novayagazeta.ru articles as Markdown.

Reads urls.txt (produced by gather_urls.py) and writes one .md file per
article under articles/YYYY/MM/DD/<slug>.md. Images and other media are not
downloaded; image captions are kept as italic text.

State lives in state.json, so the run can be stopped and started again at any
time. Already-written files are skipped unless --force is given.

Usage:
  python fetch_articles.py                 # download everything in urls.txt
  python fetch_articles.py --workers 8      # more parallelism
  python fetch_articles.py --limit 100      # smoke test
  python fetch_articles.py --retry-missing  # only URLs that previously failed
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from novaya import Fetcher, parse_record, render_markdown, url_date, slug_of

HERE = os.path.dirname(os.path.abspath(__file__))
URLS = os.path.join(HERE, "urls.txt")
OUT_DIR = os.path.join(HERE, "articles")
STATE = os.path.join(HERE, "state.json")
FAILED = os.path.join(HERE, "failed.txt")

_lock = threading.RLock()
_state = {"done": {}, "missing": {}}
_counters = {"ok": 0, "skip": 0, "gone": 0, "empty": 0, "error": 0}


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                data = json.load(f)
            _state["done"] = data.get("done") or {}
            _state["missing"] = data.get("missing") or {}
        except ValueError:
            pass


def save_state():
    tmp = STATE + ".tmp"
    with _lock:
        payload = json.dumps(_state, ensure_ascii=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, STATE)


def target_path(url):
    date = url_date(url) or "0000-00-00"
    y, m, d = date.split("-")
    slug = slug_of(url) or "unknown"
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "-", slug).strip("-")[:120]
    return os.path.join(OUT_DIR, y, m, d, slug + ".md")


def fetch_one(fetcher, url):
    """Return (kind, message). kind is one of ok/gone/empty/error."""
    status, body = fetcher.get(url, allow_404=True)
    if status == 404:
        return "gone", "404"
    if status != 200:
        return "error", "http %s" % status
    record = parse_record(body)
    if not record:
        return "empty", "no __SERVER_DATA__"
    markdown = render_markdown(record, url, html=body)
    path = target_path(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    os.replace(tmp, path)
    return "ok", str(len(markdown))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rate", type=float, default=8.0,
                    help="max requests per second across all workers")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="re-download known files")
    ap.add_argument("--retry-missing", action="store_true",
                    help="only retry URLs recorded as failed earlier")
    ap.add_argument("--urls", default=URLS)
    args = ap.parse_args()

    if not os.path.exists(args.urls):
        sys.stderr.write("missing %s - run gather_urls.py first\n" % args.urls)
        return 2

    load_state()
    with open(args.urls, encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if args.retry_missing:
        todo = [u for u in urls if u in _state["missing"]]
        sys.stderr.write("retrying %d previously failed urls\n" % len(todo))
    else:
        todo = []
        on_disk = 0
        for u in urls:
            if args.force:
                todo.append(u)
                continue
            if u in _state["done"]:
                _counters["skip"] += 1
                continue
            # Also skip when the file survived an interrupted run: the
            # state file may lag behind what is already written.
            if os.path.exists(target_path(u)):
                _counters["skip"] += 1
                on_disk += 1
                _state["done"][u] = "ok"
                continue
            todo.append(u)
        sys.stderr.write("%d urls, %d already done (%d found on disk), %d to fetch\n"
                         % (len(urls), _counters["skip"], on_disk, len(todo)))
    if args.limit:
        todo = todo[:args.limit]
        sys.stderr.write("limit: %d urls this run\n" % len(todo))

    fetcher = Fetcher(rate=args.rate, timeout=45)
    started = time.time()
    first = [True]

    def work(url):
        try:
            kind, message = fetch_one(fetcher, url)
        except Exception as e:
            kind, message = "error", repr(e)[:120]
        with _lock:
            _counters[kind] = _counters.get(kind, 0) + 1
            if kind in ("ok", "gone", "empty"):
                _state["done"][url] = kind
                _state["missing"].pop(url, None)
            else:
                _state["missing"][url] = message
            total = sum(_counters.values())
            if total % 25 == 0 or first[0]:
                first[0] = False
                rate = total / max(0.001, time.time() - started)
                sys.stderr.write(
                    "\r  %6d/%d  ok=%d skip=%d gone=%d empty=%d err=%d  %.1f/s   "
                    % (total, len(todo), _counters["ok"], _counters["skip"],
                       _counters["gone"], _counters["empty"], _counters["error"],
                       rate))
                sys.stderr.flush()
        if total % 200 == 0:
            save_state()
        return kind

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(work, todo))
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted, saving state\n")
    finally:
        save_state()

    if _state["missing"]:
        with open(FAILED, "w", encoding="utf-8") as f:
            for url, message in sorted(_state["missing"].items()):
                f.write("%s\t%s\n" % (url, message))
    sys.stderr.write("\n\ndone: ok=%d gone=%d empty=%d error=%d skipped=%d in %.0fs\n"
                     % (_counters["ok"], _counters["gone"], _counters["empty"],
                        _counters["error"], _counters["skip"],
                        time.time() - started))
    sys.stderr.write("requests=%d retries=%d throttled=%d failures=%d\n"
                     % (fetcher.stats["requests"], fetcher.stats["retries"],
                        fetcher.stats["throttled"], fetcher.stats["failures"]))
    done = len(_state["done"])
    sys.stderr.write("state: %d urls settled, markdown in %s\n" % (done, OUT_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
