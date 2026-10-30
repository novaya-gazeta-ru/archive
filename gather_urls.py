"""Collect candidate article URLs for novayagazeta.ru from four sources.

The site publishes no complete index: /feed/sitemap holds exactly 1000 recent
articles and ignores from/to/page parameters, and /feed/rss holds the last 100.
Old articles are still served, they are simply unreachable by navigation. So we
combine:

  wayback   Internet Archive CDX, one paginated query per year (best for history)
  search    /api/v1/search with q=<probe>, page 1..499 (newest 10k per probe)
  authors   /authors/<id> listing pages, ~49 links each
  cc        Common Crawl collection index
  pdf       open GCS bucket listing, names are YYYY/MM/DD/<slug>.pdf
  sitemap   /feed/sitemap and /feed/rss (recent)

Every finding is normalised to one canonical URL and written to urls.txt.

Usage:
  python gather_urls.py                # all sources
  python gather_urls.py wayback search # selected sources
"""

import concurrent.futures
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from novaya import BASE, UA, Fetcher, canon_url

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "urls.txt")
FOUND = os.path.join(HERE, "gather_found.jsonl")

_lock = threading.Lock()
_store = {}


def add(url, source):
    """Record a canonical URL; returns True when it is new."""
    canonical = canon_url(url)
    if not canonical:
        return False
    with _lock:
        if canonical in _store:
            return False
        _store[canonical] = source
        with open(FOUND, "a", encoding="utf-8") as f:
            f.write(json.dumps({"url": canonical, "source": source},
                               ensure_ascii=False) + "\n")
        return True


def progress(label, extra=""):
    with _lock:
        n = len(_store)
    sys.stderr.write("\r  %-24s %7d urls %s" % (label, n, extra))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Source: sitemap and rss
# --------------------------------------------------------------------------

def src_sitemap(fetcher):
    for path in ("/feed/sitemap", "/feed/rss"):
        status, body = fetcher.get(BASE + path)
        if status != 200:
            sys.stderr.write("  %s -> %s\n" % (path, status))
            continue
        n = 0
        for loc in re.findall(r"<loc>([^<]+)</loc>", body):
            n += 1 if add(loc, "sitemap" + path) else 0
        for guid in re.findall(r"<guid[^>]*>([^<]+)</guid>", body):
            n += 1 if add(guid, "rss") else 0
        sys.stderr.write("  %s: %d new\n" % (path, n))
        progress("sitemap")


# --------------------------------------------------------------------------
# Source: site search API
# --------------------------------------------------------------------------

# Russian letter probes. Each probe returns at most 500 pages x 20 records.
PROBES = list("абвгдежзийклмнопрстуфхцчшщыэюя") + [
    "2019", "2020", "2021", "2022", "2023", "2024", "2025",
    "Путин", "война", "Навальный", "Украина", "расследование",
    "Убили", "коррупция", "выборы", "протест",
]
MAX_PAGE = 499


def _search_page(fetcher, probe, page):
    url = ("%s/api/v1/search?%s" % (BASE, urllib.parse.urlencode(
        {"q": probe, "page": page})))
    status, body = fetcher.get(url)
    if status != 200:
        return probe, page, None
    try:
        data = json.loads(body)
    except ValueError:
        return probe, page, None
    return probe, page, data


def src_search(fetcher, workers=4, max_page=MAX_PAGE):
    for probe in PROBES:
        # Page 1 tells us whether the probe is worth paginating at all.
        _, _, first = _search_page(fetcher, probe, 1)
        if not first:
            sys.stderr.write("  probe %r failed\n" % probe)
            continue
        count = first.get("recordsCount") or 0
        added = sum(1 for r in (first.get("records") or [])
                    if add(_slug_to_url(r.get("slug")), "search:" + probe))
        pages = min(max_page, (count + 19) // 20)
        progress("search", "%s %d recs" % (probe, count))
        if pages > 1:
            def work(page):
                return _search_page(fetcher, probe, page)
            with concurrent.futures.ThreadPoolExecutor(workers) as ex:
                for _, page, data in ex.map(work, range(2, pages + 1)):
                    if not data or not data.get("records"):
                        continue
                    for r in data["records"]:
                        if add(_slug_to_url(r.get("slug")), "search:" + probe):
                            added += 1
                    if page % 25 == 0:
                        progress("search", "%s p%d/%d" % (probe, page, pages))
        sys.stderr.write("  probe %-14r count=%-7d pages=%-4d new=%d\n"
                         % (probe, count, pages, added))
        progress("search")


def _slug_to_url(slug):
    return "%s/articles/%s" % (BASE, slug) if slug else None


# --------------------------------------------------------------------------
# Source: author pages
# --------------------------------------------------------------------------

def src_authors(fetcher, max_id=400, workers=6):
    def one(aid):
        status, body = fetcher.get("%s/authors/%d" % (BASE, aid), allow_404=True)
        if status != 200:
            return aid, []
        return aid, re.findall(r'href="(/articles/[^"]+)"', body)

    live = 0
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for aid, links in ex.map(one, range(1, max_id + 1)):
            if links:
                live += 1
                for link in links:
                    add(link, "author:%d" % aid)
            if aid % 20 == 0:
                progress("authors", "id %d, %d live" % (aid, live))
    sys.stderr.write("  authors: %d live ids in 1..%d\n" % (live, max_id))
    progress("authors")


# --------------------------------------------------------------------------
# Source: Wayback CDX
# --------------------------------------------------------------------------

CDX = "http://web.archive.org/cdx/search/cdx"


def _cdx_json(url, params, tries=4):
    query = urllib.parse.urlencode(params)
    # CDX wants the target as a url= parameter and needs a literal '*'.
    target = urllib.parse.quote(url, safe="*")
    full = "%s?url=%s&%s" % (CDX, target, query)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(3 * (attempt + 1))
    return ""


def _cdx_num_pages(prefix):
    body = _cdx_json(prefix, {"showNumPages": "true", "collapse": "urlkey"}, tries=5)
    m = re.search(r"^\s*(\d+)\s*$", body)
    if m:
        return int(m.group(1))
    try:
        return int(json.loads(body).get("pages") or 0)
    except Exception:
        return 0


def _cdx_urls(prefix, page):
    """Return the original URLs on one CDX page.

    output=txt is the simplest shape (one URL per line), but the endpoint
    occasionally ignores the format, so JSON arrays are handled as well.
    """
    body = _cdx_json(prefix, {"output": "txt", "fl": "original",
                              "collapse": "urlkey", "page": str(page)}, tries=5)
    urls = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            try:
                arr = json.loads(line)
            except ValueError:
                continue
            if isinstance(arr, list) and arr and isinstance(arr[0], str):
                urls.append(arr[0])
            continue
        if line.startswith("{"):
            try:
                urls.append(json.loads(line).get("original") or "")
            except ValueError:
                pass
            continue
        urls.append(line)
    return [u for u in urls if u and u != "original"]


def src_wayback(fetcher, years=None, workers=6):
    years = years or range(1999, 2027)
    for year in years:
        prefix = "novayagazeta.ru/articles/%d/*" % year
        pages = _cdx_num_pages(prefix)
        if not pages:
            sys.stderr.write("  %d: no index (%s)\n" % (year, pages))
            continue
        added = 0
        done = 0
        # CDX pages are slow and independent, so fetch them in parallel.
        with concurrent.futures.ThreadPoolExecutor(workers) as ex:
            futures = {ex.submit(_cdx_urls, prefix, p): p for p in range(pages)}
            for fut in concurrent.futures.as_completed(futures):
                done += 1
                try:
                    originals = fut.result()
                except Exception:
                    originals = []
                for original in originals:
                    if add(original, "wayback"):
                        added += 1
                progress("wayback", "%d p%d/%d" % (year, done, pages))
        sys.stderr.write("  %d: %d pages, %d new\n" % (year, pages, added))
        progress("wayback")


# --------------------------------------------------------------------------
# Source: Common Crawl
# --------------------------------------------------------------------------

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"


def src_cc(fetcher, collections=3, workers=3):
    try:
        with urllib.request.urlopen(CC_COLLINFO, timeout=60) as r:
            ids = [c["id"] for c in json.loads(r.read().decode("utf-8"))]
    except Exception as e:
        sys.stderr.write("  commoncrawl: collinfo failed: %s\n" % e)
        return
    ids = ids[:collections]

    def one(cid):
        url = ("https://index.commoncrawl.org/%s-index?%s"
               % (cid, urllib.parse.urlencode(
                   {"url": "novayagazeta.ru/articles/*", "output": "json"})))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                return cid, r.read().decode("utf-8", "replace")
        except Exception as e:
            return cid, ""

    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for cid, body in ex.map(one, ids):
            added = 0
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    original = json.loads(line).get("url") or ""
                except ValueError:
                    continue
                if add(original, "commoncrawl:" + cid):
                    added += 1
            sys.stderr.write("  %s: %d new\n" % (cid, added))
            progress("commoncrawl")


# --------------------------------------------------------------------------
# Source: open GCS bucket with the PDFs
# --------------------------------------------------------------------------

def src_pdf(fetcher):
    base = "https://storage.googleapis.com/novaya-static"
    marker, total = "", 0
    while True:
        url = "%s?%s" % (base, urllib.parse.urlencode(
            {"prefix": "pdf/", "marker": marker, "max-keys": 1000}))
        status, body = fetcher.get(url)
        if status != 200:
            break
        keys = re.findall(r"<Key>([^<]+)</Key>", body)
        if not keys:
            break
        for key in keys:
            m = re.match(r"pdf/(\d{4}/\d{2}/\d{2}/[^/]+)\.pdf$", key)
            if m:
                if add("%s/articles/%s" % (BASE, m.group(1)), "pdf"):
                    total += 1
        if "<IsTruncated>true</IsTruncated>" not in body:
            break
        marker = keys[-1]
        progress("pdf", "%d keys" % total)
    sys.stderr.write("  pdf: %d new\n" % total)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    ap = argparse.ArgumentParser(description="collect article URLs")
    ap.add_argument("sources", nargs="*", default=None,
                    help="any of: sitemap pdf cc search authors wayback")
    ap.add_argument("--search-pages", type=int, default=MAX_PAGE,
                    help="max search-API pages per probe (20 records each)")
    ap.add_argument("--author-max-id", type=int, default=400)
    ap.add_argument("--cc-collections", type=int, default=3)
    ap.add_argument("--wayback-from", type=int, default=1999)
    ap.add_argument("--wayback-to", type=int, default=2026)
    ap.add_argument("--wayback-workers", type=int, default=6)
    ap.add_argument("--rate", type=float, default=4.0)
    args = ap.parse_args(argv[1:])

    sources = args.sources or ["sitemap", "pdf", "cc", "search", "authors", "wayback"]
    # Resume: previous findings are already in gather_found.jsonl.
    if os.path.exists(FOUND):
        with open(FOUND, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("url"):
                    _store[d["url"]] = d.get("source") or "?"
        sys.stderr.write("resuming with %d known urls\n" % len(_store))

    fetcher = Fetcher(rate=args.rate, timeout=60)
    plan = {
        "sitemap": lambda: src_sitemap(fetcher),
        "pdf": lambda: src_pdf(fetcher),
        "cc": lambda: src_cc(fetcher, collections=args.cc_collections),
        "search": lambda: src_search(fetcher, max_page=args.search_pages),
        "authors": lambda: src_authors(fetcher, max_id=args.author_max_id),
        "wayback": lambda: src_wayback(
            fetcher, years=range(args.wayback_from, args.wayback_to + 1),
            workers=args.wayback_workers),
    }
    for name in sources:
        if name not in plan:
            sys.stderr.write("unknown source %r\n" % name)
            continue
        sys.stderr.write("\n== %s\n" % name)
        t0 = time.time()
        plan[name]()
        sys.stderr.write("\n  %s done in %.0fs, total %d urls\n"
                         % (name, time.time() - t0, len(_store)))

    urls = sorted(_store)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + "\n")
    sys.stderr.write("\nwrote %d urls to %s\n" % (len(urls), OUT))
    by_source = {}
    for source in _store.values():
        by_source[source.split(":")[0]] = by_source.get(source.split(":")[0], 0) + 1
    for name, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        sys.stderr.write("  %-12s %7d\n" % (name, n))


if __name__ == "__main__":
    main(sys.argv)
