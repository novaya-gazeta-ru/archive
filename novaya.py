"""Shared helpers for the novayagazeta.ru archive tool.

No third-party dependencies: everything here uses the Python standard library
only. Two jobs:

  1. HTTP access with retries and a simple rate limiter.
  2. Turning an article page into Markdown.

An article page carries the whole record as URL-encoded JSON in
<script id="__SERVER_DATA__">. That is far smaller and cleaner than the HTML
around it, so we never scrape the rendered DOM: we read the record and render
it ourselves.
"""

import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = "https://novayagazeta.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 novaya-archive/1.0")

# Slugs are YYYY/MM/DD/name, with a leading numeric id before 2015 or so.
ARTICLE_RE = re.compile(r"^/articles/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)/?$")
# Crawlers captured relative asset requests under /articles/... paths, so the
# same route also serves junk such as ic_mainLogo.svg. Those are not articles.
ASSET_EXT_RE = re.compile(
    r"\.(svg|png|jpe?g|gif|webp|css|js|json|xml|ico|woff2?|ttf|eot|mp4|mp3|"
    r"pdf|zip|txt|map|bmp|avif)$", re.I)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Fetcher:
    """Rate-limited, retrying HTTP client shared by all workers."""

    def __init__(self, rate=4.0, timeout=45, retries=4, verbose=False):
        self.min_interval = 1.0 / rate if rate > 0 else 0.0
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.stats = {"requests": 0, "retries": 0, "failures": 0, "throttled": 0}

    def _wait_turn(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next_at)
            self._next_at = at + self.min_interval
        delay = at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def get(self, url, statuses=(200,), allow_404=False):
        """Return (status, text). status 0 means every attempt failed."""
        last = 0
        for attempt in range(self.retries):
            self._wait_turn()
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                    status = r.status
                with self._lock:
                    self.stats["requests"] += 1
                if status in statuses:
                    return status, body.decode("utf-8", "replace")
                last = status
                if status == 404 and allow_404:
                    return status, ""
                if status not in (429, 500, 502, 503, 504):
                    return status, ""
            except urllib.error.HTTPError as e:
                last = e.code
                with self._lock:
                    self.stats["requests"] += 1
                if e.code == 404 and allow_404:
                    return 404, ""
                if e.code not in (429, 500, 502, 503, 504):
                    return e.code, ""
            except Exception:
                last = 0
            with self._lock:
                self.stats["retries"] += 1
            if last == 429:
                with self._lock:
                    self.stats["throttled"] += 1
            # Exponential backoff with jitter.
            time.sleep(min(30.0, 1.5 * (2 ** attempt)) + random.random())
        with self._lock:
            self.stats["failures"] += 1
        return last, ""


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

def canon_url(url):
    """Normalise any host/query/spelling variant to one canonical article URL.

    Returns None when the string is not an article path.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    if not path.startswith("/articles/") and not path.startswith("/amp/articles/"):
        # Some hostile inputs are just a path with an escaped prefix.
        m = re.search(r"/articles/\d{4}/\d{2}/\d{2}/[^/?#]+", parsed.path or url)
        if not m:
            return None
        path = m.group(0)
    path = path.replace("/amp/articles/", "/articles/", 1)
    m = ARTICLE_RE.match(path)
    if not m:
        return None
    if ASSET_EXT_RE.search(m.group(4)):
        return None
    return "%s/articles/%s/%s/%s/%s" % (BASE, m.group(1), m.group(2),
                                        m.group(3), m.group(4))


def url_date(url):
    m = ARTICLE_RE.match(url[len(BASE):])
    if not m:
        return None
    return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))


def slug_of(url):
    m = ARTICLE_RE.match(url[len(BASE):])
    return m.group(4) if m else None


# --------------------------------------------------------------------------
# HTML to Markdown
# --------------------------------------------------------------------------

_BLOCK = {"p", "div", "section", "article", "header", "footer", "figure",
          "figcaption", "table", "tr", "aside", "main", "nav"}
_SKIP = {"script", "style", "noscript", "svg", "iframe", "form", "button", "input"}


class _ToMd(HTMLParser):
    """Small HTML to Markdown converter for article fragments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.href = None
        self.link_text = []
        self.list_depth = 0
        self.quote_depth = 0
        self.just_closed = False

    # -- helpers ----------------------------------------------------------
    def _emit(self, text):
        """Append markup to the link text while inside <a>, else to the body."""
        if self.href is not None:
            self.link_text.append(text)
        else:
            self.out.append(text)
        if text.strip():
            self.just_closed = False

    def _emit_emphasis(self, marker):
        """Open an emphasis run, separating it from a run that just closed.

        <em>author</em><strong>title</strong> would otherwise come out as
        "*author***title**", where the closing and opening markers merge into
        one invalid run. Nested tags are untouched, so <strong><em>x</em>
        </strong> still renders as the valid "***x***".
        """
        if self.just_closed:
            self._emit(" ")
        self._emit(marker)

    def _newline(self, count=2):
        if self.href is not None:
            return
        tail = "".join(self.out[-3:])
        if not tail.endswith("\n" * count):
            self.out.append("\n" * count)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
            return
        if tag in _BLOCK:
            self._newline(2)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(2)
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "br":
            self._emit("\\\n")
        elif tag in ("strong", "b"):
            self._emit_emphasis("**")
        elif tag in ("em", "i"):
            self._emit_emphasis("*")
        elif tag == "sup":
            self._emit("^")
        elif tag == "code":
            self._emit("`")
        elif tag == "a":
            self.href = a.get("href") or ""
            self.link_text = []
        elif tag == "li":
            self._newline(1)
            self._emit("  " * max(0, self.list_depth - 1) + "- ")
        elif tag in ("ul", "ol"):
            self._newline(1)
            self.list_depth += 1
        elif tag == "blockquote":
            self._newline(2)
            self.quote_depth += 1
            self._emit("> ")
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt and alt.lower() != "add":
                self._emit("[%s]" % alt)
        elif tag == "hr":
            self._newline(2)
            self._emit("---")
            self._newline(2)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in ("strong", "b"):
            self._emit("**")
            self.just_closed = True
        elif tag in ("em", "i"):
            self._emit("*")
            self.just_closed = True
        elif tag == "code":
            self._emit("`")
        elif tag == "a":
            text = re.sub(r"\s+", " ", "".join(self.link_text)).strip()
            href = self.href or ""
            if href.startswith("//"):
                href = "https:" + href
            if href and not href.startswith("#") and text:
                self.out.append("[%s](%s)" % (text, href))
            elif text:
                self.out.append(text)
            self.href = None
            self.link_text = []
        elif tag in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
        elif tag == "blockquote":
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag in _BLOCK:
            self._newline(2)

    def handle_data(self, data):
        if self.skip:
            return
        text = re.sub(r"[ \t\r\n]+", " ", data)
        if not text:
            return
        if self.href is not None:
            self.link_text.append(text)
        else:
            self.out.append(text)



def html2md(fragment):
    """Convert an HTML fragment to Markdown. Images are dropped, captions stay."""
    if not fragment:
        return ""
    p = _ToMd()
    p.feed(fragment)
    p.close()
    md = "".join(p.out)
    md = re.sub(r"[ \t]+$", "", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"\*\*(\s*)\*\*", "", md)
    # Strip credentials that some old links carry inline.
    md = re.sub(r"(https?://)[^/\s@]+@", r"\1", md)
    lines = []
    for line in md.split("\n"):
        if line.strip():
            lines.append(line.rstrip())
        else:
            lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Article record
# --------------------------------------------------------------------------

def parse_record(html):
    """Extract the article record from a page, or None when absent."""
    m = re.search(r'<script id="__SERVER_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.startswith("%7B") or raw.startswith("%7b"):
        raw = urllib.parse.unquote(raw)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    for q in data.get("queries") or []:
        key = q.get("queryKey") or []
        if key and key[0] == "article":
            state = q.get("state") or {}
            return state.get("data")
    return None


def html_article_block(html):
    """Fallback body: the rendered article container.

    Some old records carry body: [] in the JSON while the page still renders
    the text inside <article id="article">, so we read that instead.
    """
    start = html.find('id="article"')
    if start < 0:
        return ""
    start = html.rfind("<", 0, start)
    end = html.find("</article>", start)
    if end < 0:
        return ""
    block = html[start:end]
    # Drop chrome that is not part of the text.
    block = re.sub(r"<header\b.*?</header>", "", block, flags=re.S)
    block = re.sub(r"<script\b.*?</script>", "", block, flags=re.S)
    block = re.sub(r"<style\b.*?</style>", "", block, flags=re.S)
    block = re.sub(r"<aside\b.*?</aside>", "", block, flags=re.S)
    # Cut everything from the first donation or contact widget on; the rest
    # of the container is site chrome rather than article text.
    for marker in ("Поддержите", "Нажимая кнопку", "Стать соучастником",
                   "Если у вас есть вопросы, пишите"):
        cut = block.find(marker)
        if cut > 0:
            block = block[:block.rfind("<p", 0, cut)] if block.rfind("<p", 0, cut) > 0 else block[:cut]
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", block, re.S)
    if not paragraphs:
        return ""
    return "\n".join("<p>%s</p>" % p for p in paragraphs)


def _plain(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _author_line(authors):
    names = []
    for a in authors or []:
        name = " ".join(x for x in [(a.get("name") or "").strip(),
                                    (a.get("surname") or "").strip()] if x)
        caption = (a.get("caption") or "").strip()
        if name and caption:
            name = "%s (%s)" % (name, caption)
        if name:
            names.append(name)
    return ", ".join(names)


def _ymd(dateiso, datems):
    if dateiso:
        return dateiso
    if datems:
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(int(datems) / 1000.0,
                                          tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return ""
    return ""


def _yaml_scalar(value):
    """Quote a value so colons, quotes and hashes cannot break the YAML.

    Titles routinely contain ": " ("Суд Москвы: сенсация"), which a plain
    scalar would truncate at when the front matter is parsed.
    """
    return json.dumps(value, ensure_ascii=False)


def render_markdown(record, url, html=None):
    """Render one article record to Markdown with YAML front matter."""
    title = _plain(record.get("title"))
    subtitle = _plain(record.get("subtitle"))
    authors = _author_line(record.get("authors"))
    tags = record.get("tags") or []
    out = ["---",
           "source: %s" % _yaml_scalar(url),
           "title: %s" % _yaml_scalar(title),
           "subtitle: %s" % _yaml_scalar(subtitle),
           "date: %s" % _yaml_scalar(_ymd(record.get("dateISO"), record.get("date"))),
           "authors: %s" % _yaml_scalar(authors),
           "rubric: %s" % _yaml_scalar(record.get("typeRubricDisplayName") or ""),
           "genre: %s" % _yaml_scalar(record.get("typeGenreDisplayName") or ""),
           "tags: %s" % _yaml_scalar(", ".join(t.replace(",", ";") for t in tags)),
           "---", ""]
    out.append("# %s" % (title or slug_of(url)))
    out.append("")
    if subtitle:
        # The subtitle is emitted in italics, so any emphasis already inside it
        # has to be neutralised or the delimiters nest badly.
        out.append("*%s*" % subtitle.replace("*", "").strip())
        out.append("")

    written = False
    for block in record.get("body") or []:
        if not isinstance(block, dict):
            continue
        raw_kind = block.get("type")
        kind = raw_kind if isinstance(raw_kind, str) else ""
        data = block.get("data")
        if not kind:
            # A numeric type is an embed id (video, gallery widget). Such a
            # block only carries a caption, which we keep as a note.
            caption = (block.get("caption") or "").strip()
            if caption:
                out.append("*[media: %s]*" % caption)
                out.append("")
            continue
        if kind.startswith("embed"):
            link = (block.get("link") or "").strip()
            caption = (block.get("caption") or "").strip()
            if link:
                label = caption or (block.get("network") or "embed")
                out.append("**%s:** %s" % (label, link))
                out.append("")
                written = True
            continue
        if kind == "image/single":
            caption = html2md(data) if isinstance(data, str) else ""
            caption = caption.strip()
            if caption:
                out.append("*%s*" % caption)
                out.append("")
            continue
        if not kind:
            continue
        if kind.startswith("embed"):
            continue
        if not isinstance(data, str) or not data.strip():
            continue
        text = html2md(data)
        if not text:
            continue
        written = True
        if kind == "text/quote":
            out.append("\n".join("> " + line if line.strip() else ">"
                                 for line in text.split("\n")))
            out.append("")
        elif kind == "text/framed":
            label = (block.get("typeDisplay") or "").strip()
            if label:
                out.append("> **%s**" % label)
                out.append(">")
            out.append("\n".join("> " + line if line.strip() else ">"
                                 for line in text.split("\n")))
            out.append("")
        else:
            out.append(text)
            out.append("")

    if not written:
        # Records with an empty body: take whichever fallback carries more
        # text, since the lead sometimes holds the whole article and
        # sometimes only a teaser.
        from_container = html2md(html_article_block(html)) if html else ""
        from_lead = html2md(record.get("lead"))
        fallback = (from_container if len(from_container) >= len(from_lead)
                    else from_lead)
        if fallback:
            out.append(fallback)
            out.append("")

    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body + "\n"
