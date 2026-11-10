"""Показ HTML-писем: письмо отрисовывается в изолированном iframe без скриптов, картинки из интернета
по умолчанию скрыты (по ним отправитель узнаёт, что письмо открыли), встроенные картинки подставляются из вложений."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Dict, List, Tuple

DROP_WITH_CONTENT = {"script", "noscript", "iframe", "frame", "frameset", "object", "embed", "applet", "template", "title"}
DROP_TAG = {"base", "link", "meta", "html", "head", "body", "param"}
VOID = {"area", "br", "col", "hr", "img", "input", "source", "track", "wbr"}
URL_ATTRS = {"href", "src", "background", "action", "formaction", "poster", "srcset", "xlink:href", "data", "lowsrc"}
REMOTE_RE = re.compile(r"^\s*(https?:)?//", re.I)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
CSS_IMPORT_RE = re.compile(r"@import[^;]*;?", re.I)
CSS_BAD_RE = re.compile(r"expression\s*\(|behavior\s*:|-moz-binding", re.I)

BASE_STYLE = """
html { background: #fff; color: #1f1f1f; }
body { margin: 0; padding: 16px 20px; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
       overflow-wrap: anywhere; }
img { max-width: 100%; height: auto; }
table { max-width: 100%; }
pre { white-space: pre-wrap; }
a { color: #0f6cbd; }
blockquote { margin: 0 0 0 8px; padding-left: 10px; border-left: 3px solid #d1d1d1; color: #555; }
img[data-blocked] { display: inline-block; min-width: 24px; min-height: 24px; background: #f0f0f0; outline: 1px dashed #c8c8c8; }
"""


class _Rewriter(HTMLParser):
    def __init__(self, inline: Dict[str, str], allow_remote: bool):
        super().__init__(convert_charrefs=False)
        self.inline = inline
        self.allow_remote = allow_remote
        self.out: List[str] = []
        self.skip = 0
        self.blocked = 0
        self.in_style = False

    # --- адреса и стили -----------------------------------------------------
    def _url(self, value: str) -> str:
        """Возвращает безопасный адрес или пустую строку."""
        v = html.unescape(value or "").strip()
        low = v.lower()
        if low.startswith("cid:"):
            return self.inline.get(v[4:].strip("<>").lower(), "")
        if low.startswith("data:image/") and not low.startswith("data:image/svg"):
            return v
        if REMOTE_RE.match(v):
            if self.allow_remote:
                return v
            self.blocked += 1
            return ""
        return ""

    def _css(self, text: str) -> str:
        text = CSS_BAD_RE.sub("", CSS_IMPORT_RE.sub("", text))
        return CSS_URL_RE.sub(lambda m: "url('{}')".format(self._url(m.group(2)).replace("'", "%27")), text)

    # --- теги ---------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def _start(self, tag, attrs, closed):
        if tag in DROP_WITH_CONTENT:
            if not closed and tag not in VOID:
                self.skip += 1
            return
        if self.skip or tag in DROP_TAG:
            return
        parts = [tag]
        blocked_before = self.blocked
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name in ("srcdoc", "formaction", "action", "ping", "target"):
                continue
            value = value or ""
            if name == "style":
                value = self._css(value)
            elif name == "href":
                v = html.unescape(value).strip()
                if not re.match(r"^(https?:|mailto:|tel:|#)", v, re.I):
                    continue
            elif name == "srcset":
                if not self.allow_remote:
                    if value.strip():
                        self.blocked += 1
                    continue
                if any(not REMOTE_RE.match(u.strip().split(" ")[0]) for u in value.split(",") if u.strip()):
                    continue
            elif name in URL_ATTRS:
                value = self._url(value)
                if not value:
                    continue
            parts.append('{}="{}"'.format(name, html.escape(value, quote=True)))
        if tag == "img" and self.blocked > blocked_before:
            parts.append("data-blocked")
        if tag == "a":
            parts.append('target="_blank" rel="noopener noreferrer"')
        if tag == "style":
            self.in_style = True
        self.out.append("<{}{}>".format(" ".join(parts), "" if not closed else ""))

    def handle_endtag(self, tag):
        if tag in DROP_WITH_CONTENT:
            if self.skip:
                self.skip -= 1
            return
        if self.skip or tag in DROP_TAG or tag in VOID:
            return
        if tag == "style":
            self.in_style = False
        self.out.append("</{}>".format(tag))

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(self._css(data) if self.in_style else data)

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append("&{};".format(name))

    def handle_charref(self, name):
        if not self.skip:
            self.out.append("&#{};".format(name))

    def handle_comment(self, data):
        pass  # условные комментарии Outlook нам не нужны


def render(raw: str, inline: Dict[str, str], allow_remote: bool) -> Tuple[str, int]:
    """Документ для iframe srcdoc и число скрытых картинок из интернета."""
    w = _Rewriter(inline, allow_remote)
    try:
        w.feed(raw or "")
        w.close()
        body = "".join(w.out)
    except Exception:  # noqa: BLE001 — сломанный HTML показываем текстом
        body = "<pre>{}</pre>".format(html.escape(raw or ""))
    img_src = "data: https: http:" if allow_remote else "data:"
    csp = ("default-src 'none'; img-src {img}; style-src 'unsafe-inline'; font-src data:{fonts}; "
           "form-action 'none'".format(img=img_src, fonts=" https:" if allow_remote else ""))
    doc = ('<!doctype html><html><head><meta charset="utf-8">'
           '<meta http-equiv="Content-Security-Policy" content="{csp}">'
           '<base target="_blank"><style>{base}</style></head><body>{body}</body></html>').format(
        csp=csp, base=BASE_STYLE, body=body)
    return doc, w.blocked


def clean_fragment(raw: str) -> str:
    """Текст из редактора перед отправкой: без скриптов, обработчиков и опасных ссылок."""
    w = _Rewriter({}, True)
    w.feed(raw or "")
    w.close()
    return "".join(w.out)


def text_document(text: str) -> str:
    """Письмо без HTML: тот же вид, что у HTML-писем, ссылки кликабельны."""
    escaped = html.escape(text or "")
    linked = re.sub(r"(https?://[^\s<>&\"]+)", r'<a href="\1">\1</a>', escaped)
    doc, _ = render('<div style="white-space: pre-wrap">{}</div>'.format(linked), {}, False)
    return doc
