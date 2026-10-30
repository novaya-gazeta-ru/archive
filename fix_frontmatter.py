"""Repair YAML front matter in already-downloaded Markdown files.

Titles and subtitles often contain ": " (for example "Суд Москвы: сенсация").
Written as a plain YAML scalar such a value is truncated by any parser, so the
field has to be quoted. This rewrites the front matter of every file in place
without touching the article body, which is far cheaper than re-downloading.

Usage:
  python fix_frontmatter.py --dry-run
  python fix_frontmatter.py
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ("source", "title", "subtitle", "date", "authors", "rubric", "genre", "tags")


def fix_frontmatter(text):
    if not text.startswith("---\n"):
        return text, False
    end = text.find("\n---\n", 4)
    if end < 0:
        return text, False
    block = text[4:end]
    lines = block.split("\n")
    out = []
    changed = False
    for line in lines:
        name, sep, value = line.partition(": ")
        if sep and name in FIELDS:
            value = value.strip()
            # Unquote first so re-quoting is idempotent.
            if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value[1:-1]
            else:
                value = value.replace("'", "'")
            new = "%s: %s" % (name, json.dumps(value, ensure_ascii=False))
            if new != line:
                changed = True
            out.append(new)
        else:
            out.append(line)
    if not changed:
        return text, False
    return "---\n" + "\n".join(out) + text[end:], True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    files = glob.glob(os.path.join(HERE, "articles", "*", "*", "*", "*.md"))
    fixed = 0
    for path in files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new, changed = fix_frontmatter(text)
        if not changed:
            continue
        fixed += 1
        if not args.dry_run:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            os.replace(tmp, path)
    sys.stderr.write("%s %d of %d files\n"
                     % ("would fix" if args.dry_run else "fixed", fixed, len(files)))


if __name__ == "__main__":
    main()
