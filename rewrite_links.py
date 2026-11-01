"""Rewrite links to novayagazeta.ru so they point at local Markdown files.

Every article URL in an article body becomes a relative path to the matching
.md file when that file exists. When it does not, the URL is replaced by a
Wayback Machine lookup link, which keeps the reference reachable after the site
shuts down.

The front matter `source:` field is left alone: it records where the article
came from and must keep the original address.

Usage:
  python rewrite_links.py --dry-run
  python rewrite_links.py
"""

import argparse
import glob
import json
import os
import re
import sys

from novaya import BASE, canon_url, slug_of, url_date

HERE = os.path.dirname(os.path.abspath(__file__))
ARTICLES = os.path.join(HERE, "articles")
ARCHIVE = "https://web.archive.org/web/*/%s"

# A URL inside a markdown link, and a bare URL standing in the text.
LINK_RE = re.compile(r"\]\((https?://(?:www\.)?novayagazeta\.ru[^)\s]*)\)")
# Stop at whitespace, closing punctuation and quotes; a second url glued right
# after another one is split by the http boundary below.
BARE_RE = re.compile(r"(?<!\]\()(?<!\()(https?://(?:www\.)?novayagazeta\.ru[^)\s\]<>\"]*)")


def local_path(url):
    date = url_date(url)
    if not date:
        return None
    y, m, d = date.split("-")
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "-", slug_of(url) or "x").strip("-")[:120]
    rel = "%s/%s/%s/%s.md" % (y, m, d, slug)
    if os.path.exists(os.path.join(ARTICLES, rel)):
        return rel
    return None


def build_titles():
    """url -> title, taken from each local file's front matter."""
    out = {}
    for path in glob.glob(os.path.join(ARTICLES, "*", "*", "*", "*.md")):
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(1200)
        except OSError:
            continue
        m = re.search(r'^source: "([^"]+)"', head, re.M)
        t = re.search(r'^title: "(.*)"$', head, re.M)
        if m and t:
            out[m.group(1)] = t.group(1).replace('\\"', '"')
    return out


def split_glued(text):
    """'http://a.htmlhttp://b' -> ['http://a.html', 'http://b']."""
    parts = re.split(r"(?=https?://)", text)
    return [p for p in parts if p]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    titles = build_titles()
    sys.stderr.write("titles known: %d\n" % len(titles))

    stats = {"local": 0, "archive": 0, "bare_local": 0, "bare_archive": 0}
    changed = 0
    bad = 0

    for path in glob.glob(os.path.join(ARTICLES, "*", "*", "*", "*.md")):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if "novayagazeta.ru" not in text:
            continue
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end < 0:
                continue
            head, body = text[:end + 5], text[end + 5:]
        else:
            head, body = "", text
        if "novayagazeta.ru" not in body:
            continue
        original = body

        # 1. markdown links
        def fix_link(m):
            url = m.group(1)
            canonical = canon_url(url)
            if not canonical:
                stats["archive"] += 1
                return "](%s)" % (ARCHIVE % url)
            rel = local_path(canonical)
            if rel:
                stats["local"] += 1
                return "](%s)" % rel
            stats["archive"] += 1
            return "](%s)" % (ARCHIVE % url)

        body = LINK_RE.sub(fix_link, body)

        # 2. bare urls in running text
        def fix_bare(m):
            raw = m.group(1)
            # a url that ends with .htmlhttp://... is two urls glued together
            pieces = split_glued(raw)
            rendered = []
            for piece in pieces:
                canonical = canon_url(piece)
                if canonical:
                    rel = local_path(canonical)
                    if rel:
                        label = titles.get(canonical) or slug_of(canonical) or piece
                        label = label.replace("[", "(").replace("]", ")")
                        rendered.append("[%s](%s)" % (label, rel))
                        stats["bare_local"] += 1
                        continue
                url = piece if canon_url(piece) else piece
                rendered.append("[%s](%s)" % (url, ARCHIVE % url))
                stats["bare_archive"] += 1
            return " ".join(rendered)

        body = BARE_RE.sub(fix_bare, body)

        if body != original:
            changed += 1
            if not args.dry_run:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(head + body)
                os.replace(tmp, path)

    sys.stderr.write("%s %d files\n" % ("would change" if args.dry_run else "changed", changed))
    sys.stderr.write("markdown links -> local %d, archive %d\n"
                     % (stats["local"], stats["archive"]))
    sys.stderr.write("bare urls      -> local %d, archive %d\n"
                     % (stats["bare_local"], stats["bare_archive"]))


if __name__ == "__main__":
    main()
