"""Place downloaded images into the Markdown files.

fetch_assets.py writes assets_map.json: for every article URL, the images found
in its record, each with the local file and the caption that the renderer had
already written into the .md as an italic line.

This script walks the .md files, finds the matching caption line and inserts a
Markdown image reference in front of it, so the picture appears where it was in
the original article. Files that already carry image links are skipped, which
makes the pass safe to repeat.

Usage:
  python link_assets.py --dry-run
  python link_assets.py
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARTICLES = os.path.join(HERE, "articles")
ASSETS = os.path.join(ARTICLES, "_assets")
INDEX = os.path.join(HERE, "assets_map.json")


def target_path(url):
    from novaya import url_date, slug_of
    date = url_date(url) or "0000-00-00"
    y, m, d = date.split("-")
    slug = slug_of(url) or "unknown"
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "-", slug).strip("-")[:120]
    return os.path.join(ARTICLES, y, m, d, slug + ".md")


def normalise(text):
    """Compare captions loosely: the .md strips formatting, so do the same."""
    text = re.sub(r"[*_`\[\]]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def insert_images(body, entries):
    """Insert ![](path) before each matching caption line, in document order.

    Images whose caption is empty have no anchor in the text, so they go right
    after the H1 instead: in the record those are almost always lead pictures
    that sat at the top of the article.
    """
    lines = body.split("\n")
    placed = 0
    used = set()
    out = []
    for line in lines:
        matched = None
        stripped = normalise(line)
        if stripped:
            for i, e in enumerate(entries):
                if i in used:
                    continue
                cap = normalise(e.get("caption") or "")
                if cap and stripped == cap:
                    matched = i
                    break
        if matched is not None:
            e = entries[matched]
            used.add(matched)
            # Alt text stays empty: the caption is already on its own italic
            # line right below, so repeating it in brackets reads as noise.
            out.append("![](%s)" % e["local"])
            out.append("")
            placed += 1
        out.append(line)

    leftovers = [e for i, e in enumerate(entries) if i not in used]
    if leftovers:
        # After the H1 line, which is the first line starting with a single '#'.
        insert_at = None
        for idx, line in enumerate(out):
            if line.startswith("# "):
                insert_at = idx + 1
                break
        if insert_at is None:
            insert_at = 0
        block = []
        for e in leftovers:
            # No caption exists for these, and nothing follows the image, so
            # the alt text is the only place the description can live.
            alt = (e.get("caption") or "").replace("[", "(").replace("]", ")")
            block.append("![%s](%s)" % (alt, e["local"]))
            block.append("")
            placed += 1
        out[insert_at:insert_at] = block
    return "\n".join(out), placed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(INDEX, encoding="utf-8") as f:
        index = json.load(f)
    sys.stderr.write("articles with images in the index: %d\n" % len(index))

    changed = 0
    images_added = 0
    missing_file = 0
    not_found = 0

    for url, entries in index.items():
        if not entries:
            continue
        path = target_path(url)
        if not os.path.exists(path):
            missing_file += 1
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if "](articles/_assets/" in text or "](_assets/" in text:
            continue
        # split front matter so it is never touched
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end < 0:
                continue
            head, body = text[:end + 5], text[end + 5:]
        else:
            head, body = "", text
        new_body, placed = insert_images(body, entries)
        if placed == 0:
            not_found += 1
            continue
        images_added += placed
        changed += 1
        if not args.dry_run:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(head + new_body)
            os.replace(tmp, path)

    sys.stderr.write("%s %d files, %d images inserted\n"
                     % ("would change" if args.dry_run else "changed",
                        changed, images_added))
    if not_found:
        sys.stderr.write("captions not matched in %d files\n" % not_found)
    if missing_file:
        sys.stderr.write("markdown file missing for %d urls\n" % missing_file)


if __name__ == "__main__":
    main()
