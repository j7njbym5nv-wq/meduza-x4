#!/usr/bin/env python3
"""Собирает EPUB-выпуски из RSS «Медузы» и JSON-каталоги для плагина CrossPoint.

Только стандартная библиотека Python 3.9+.

    python3 build.py --out docs --base-url https://<user>.github.io/<repo>
"""
import argparse
import datetime as dt
import html
import json
import re
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path

FEEDS = [
    # slug,      название раздела, URL ленты
    ("news",     "Новости",        "https://meduza.io/rss/news"),
    ("articles", "Статьи",         "https://meduza.io/rss/articles"),
    ("en",       "Meduza English", "https://meduza.io/rss/en/all"),
]
KEEP = 10  # сколько последних выпусков каждого раздела хранить
MSK = dt.timezone(dt.timedelta(hours=3))
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}

# ---------------------------------------------------------------- HTML → XHTML

BLOCK = {"p", "h3", "blockquote", "ul", "ol", "li"}
INLINE = {"b", "strong", "i", "em", "sub", "sup"}
RENAME = {"h1": "h3", "h2": "h3", "h4": "h3", "h5": "h3", "figcaption": "p"}
SKIP = {"script", "style", "svg", "iframe", "noscript", "button", "form", "video", "audio"}


class Sanitizer(HTMLParser):
    """Оставляет простую разметку, которую хорошо рисует CrossPoint; всегда даёт валидный XHTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def _close_to(self, tag):
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                return

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        tag = RENAME.get(tag, tag)
        if tag == "br":
            self.out.append("<br/>")
        elif tag in BLOCK:
            # блоки внутри <p>/<h3> не вкладываем — закрываем абзац
            while self.stack and self.stack[-1] in INLINE | {"p", "h3"}:
                self._close_to(self.stack[-1])
            if tag == "li" and not any(t in ("ul", "ol") for t in self.stack):
                self.out.append("<ul>")
                self.stack.append("ul")
            self.out.append(f"<{tag}>")
            self.stack.append(tag)
        elif tag in INLINE:
            self.out.append(f"<{tag}>")
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        tag = RENAME.get(tag, tag)
        if tag in self.stack:
            self._close_to(tag)

    def handle_data(self, data):
        if not self.skip and data:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self._close_to(self.stack[-1])
        s = "".join(self.out)
        s = re.sub(r"<(p|h3|li|b|strong|i|em)>\s*</\1>", "", s)  # пустые теги
        s = re.sub(r"<(ul|ol)>\s*</\1>", "", s)
        return s


def to_xhtml(fragment: str) -> str:
    p = Sanitizer()
    p.feed(fragment or "")
    p.close()
    body = p.result()
    try:
        ET.fromstring(f"<div>{body}</div>")
        return body
    except ET.ParseError:  # на всякий случай — только текст
        text = re.sub(r"<[^>]+>", "\n", fragment or "")
        return "".join(f"<p>{html.escape(t.strip())}</p>" for t in text.split("\n") if t.strip())


# ---------------------------------------------------------------- RSS

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (meduza-x4 epub builder)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_feed(raw: bytes):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        body = it.findtext("content:encoded", namespaces=NS) or it.findtext("description") or ""
        pub = it.findtext("pubDate") or ""
        items.append({
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "guid": (it.findtext("guid") or it.findtext("link") or title).strip(),
            "pub": pub,
            "body": body,
        })
    return items


def fmt_time(pub: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub).astimezone(MSK).strftime("%d.%m %H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------- EPUB

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" href="style.css" type="text/css"/></head>
<body>
{body}
</body>
</html>
"""

CSS = """body { margin: 0; }
h1 { font-size: 1.3em; margin: 0 0 .4em; }
h2 { font-size: 1.15em; margin: 0 0 .3em; }
h3 { font-size: 1.05em; margin: 1em 0 .3em; }
p { margin: 0 0 .5em; text-indent: 0; }
.meta { font-size: .8em; margin-bottom: 1em; }
blockquote { margin: .5em 1em; font-style: italic; }
li { margin-bottom: .3em; }
"""


def build_epub(path: Path, title: str, items, lang: str, stamp: dt.datetime):
    book_id = f"urn:uuid:{uuid.uuid4()}"
    chapters = []
    for n, it in enumerate(items, 1):
        t = html.escape(it["title"])
        when = fmt_time(it["pub"])
        body = (f"<h2>{t}</h2>\n<p class=\"meta\">{html.escape(when)}</p>\n"
                f"{to_xhtml(it['body'])}")
        chapters.append((f"a{n:03d}.xhtml", it["title"], XHTML.format(lang=lang, title=t, body=body)))

    toc_items = "\n".join(f'<li><a href="{f}">{html.escape(t)}</a></li>' for f, t, _ in chapters)
    toc_page = XHTML.format(lang=lang, title=html.escape(title), body=(
        f"<h1>{html.escape(title)}</h1>\n<ol>\n{toc_items}\n</ol>"))
    nav = XHTML.format(lang=lang, title="Содержание", body=(
        f'<nav epub:type="toc" id="toc"><h1>Содержание</h1><ol>\n'
        f'<li><a href="toc.xhtml">{html.escape(title)}</a></li>\n{toc_items}\n</ol></nav>'))

    files = [("toc.xhtml", title, toc_page)] + chapters
    manifest = "\n".join(
        f'<item id="c{i}" href="{f}" media-type="application/xhtml+xml"/>' for i, (f, _, _) in enumerate(files))
    spine = "\n".join(f'<itemref idref="c{i}"/>' for i in range(len(files)))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{lang}">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:creator>Meduza</dc:creator>
<dc:language>{lang}</dc:language>
<dc:date>{stamp.date().isoformat()}</dc:date>
<meta property="dcterms:modified">{stamp.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
{manifest}
</manifest>
<spine toc="ncx">
{spine}
</spine>
</package>
"""
    navpoints = "\n".join(
        f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{html.escape(t)}</text></navLabel>'
        f'<content src="{f}"/></navPoint>' for i, (f, t, _) in enumerate(files))
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/></head>
<docTitle><text>{html.escape(title)}</text></docTitle>
<navMap>
{navpoints}
</navMap>
</ncx>
"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", CSS, compress_type=zipfile.ZIP_DEFLATED)
        for f, _, content in files:
            z.writestr(f"OEBPS/{f}", content, compress_type=zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs")
    ap.add_argument("--base-url", required=True, help="https://<user>.github.io/<repo>")
    ap.add_argument("--max-items", type=int, default=30)
    args = ap.parse_args()

    out = Path(args.out)
    base = args.base_url.rstrip("/")
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    now = dt.datetime.now(MSK)
    (out / "issues").mkdir(parents=True, exist_ok=True)

    for slug, name, url in FEEDS:
        try:
            items = parse_feed(fetch(url))[: args.max_items]
        except Exception as e:  # одна упавшая лента не должна ломать остальные
            print(f"[{slug}] ошибка загрузки: {e}")
            continue
        if not items:
            continue
        lang = "en" if slug == "en" else "ru"
        top = items[0]["guid"]
        if state.get(slug) != top:
            fname = f"meduza-{slug}-{now:%Y%m%d-%H%M}.epub"
            title = f"Медуза · {name} · {now:%d.%m %H:%M}"
            build_epub(out / "issues" / fname, title, items, lang, now)
            state[slug] = top
            meta = json.loads((out / "issues" / f"{slug}.json").read_text(encoding="utf-8")) if (out / "issues" / f"{slug}.json").exists() else []
            meta.insert(0, {"id": fname[:-5], "title": title, "author": f"{len(items)} материалов",
                            "url": f"{base}/issues/{fname}"})
            for old in meta[KEEP:]:
                (out / "issues" / f"{old['id']}.epub").unlink(missing_ok=True)
            meta = meta[:KEEP]
            (out / "issues" / f"{slug}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{slug}] новый выпуск: {fname} ({len(items)} материалов)")
        else:
            print(f"[{slug}] без изменений")

    # общий каталог «Все выпуски»
    all_items = []
    for slug, _, _ in FEEDS:
        p = out / "issues" / f"{slug}.json"
        if p.exists():
            all_items += json.loads(p.read_text(encoding="utf-8"))
    all_items.sort(key=lambda x: x["id"].rsplit("-", 2)[-2:], reverse=True)
    (out / "issues" / "all.json").write_text(json.dumps(all_items, ensure_ascii=False, indent=1), encoding="utf-8")
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    (out / ".nojekyll").touch()


if __name__ == "__main__":
    main()
