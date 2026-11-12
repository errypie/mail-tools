"""Вложения и ссылки писем для раздела «Вложения и ссылки»: что это, откуда и пара слов из письма вокруг."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

KINDS = ("photo", "file", "archive", "link")
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "tif", "tiff"}
ARCHIVE_EXT = {"zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz", "zst", "cab", "iso"}
SMALL_INLINE = 15 * 1024      # встроенные картинки меньше — логотипы и значки подписи
MAX_LINKS = 100               # ссылок из одного письма
MAX_URL = 2000
CONTEXT_CHARS = 220
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'«»]+", re.I)
# конец предложения: знак и следующее с заглавной, чтобы «см.», «т. е.» не рвали фразу
SENTENCE_END = re.compile(r"[.!?…](?=\s+[A-ZА-ЯЁ«\"(])|\n")
ATTACH_WORDS = re.compile(r"влож|прилага|прикреп|attach|во вложени|направляю|высылаю|отправляю", re.I)


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def attachment_kind(a: dict) -> Optional[str]:
    """photo, archive, file или None — вложение не показываем (вложенное письмо, логотип подписи)."""
    if a.get("file") is False:
        return None
    name, ctype = a.get("name") or "", (a.get("content_type") or "").lower()
    ext = _ext(name)
    image = (ctype.startswith("image/") and "svg" not in ctype) or ext in IMAGE_EXT
    if a.get("inline") and (not image or (a.get("size") or 0) < SMALL_INLINE):
        return None
    if image:
        return "photo"
    if ext in ARCHIVE_EXT or "zip" in ctype or "compressed" in ctype:
        return "archive"
    return "file"


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_excluded(domain: str, excluded: Sequence[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in excluded if d)


def clean_domains(values) -> List[str]:
    """Домены из настройки: без схемы, пути и «www.», в нижнем регистре."""
    if isinstance(values, str):
        values = re.split(r"[\s,;]+", values)
    out = []
    for v in values or []:
        v = str(v).strip().lower()
        if "://" in v:
            v = urlsplit(v).hostname or ""
        v = re.sub(r"^www\.", "", v.split("/")[0].lstrip("*."))
        if v and re.match(r"^[a-z0-9.-]+$", v) and v not in out:
            out.append(v[:255])
    return out[:200]


def normalize_url(href: str) -> Optional[str]:
    href = (href or "").strip().strip(".,;:)»\"'")
    if href.lower().startswith("www."):
        href = "http://" + href
    if not re.match(r"^https?://", href, re.I) or len(href) > MAX_URL:
        return None
    parts = urlsplit(href)
    if (parts.hostname or "").endswith("safelinks.protection.outlook.com"):  # защищённые ссылки Outlook
        target = parse_qs(parts.query).get("url")
        if target:
            return normalize_url(unquote(target[0]))
    return href if parts.hostname else None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _around(text: str, start: int, end: int) -> str:
    """Предложение вокруг упоминания: от конца прошлого предложения до конца текущего."""
    left = max(0, start - CONTEXT_CHARS)
    for m in SENTENCE_END.finditer(text, left, min(len(text), start + 4)):  # +4: заглядывание вперёд видит начало
        if m.end() <= start:
            left = m.end()
    m = SENTENCE_END.search(text, end)
    if m and m.start() - end < CONTEXT_CHARS:
        right = m.start() + (0 if m.group(0) == "\n" else 1)
    else:
        right = min(len(text), end + CONTEXT_CHARS // 2)
    return _squash(text[left:right])[:CONTEXT_CHARS * 2]


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.size = 0
        self.links: List[list] = []   # [href, начало текста ссылки, конец]
        self.open: Optional[list] = None
        self.skip = 0

    def _add(self, s: str):
        self.parts.append(s)
        self.size += len(s)

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        elif tag == "a":
            href = dict(attrs).get("href")
            self.open = [href, self.size, self.size] if href else None
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"):
            self._add("\n")
        elif tag in ("td", "th"):
            self._add(" ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.open:
            self.open[2] = self.size
            self.links.append(self.open)
            self.open = None
        elif tag in ("p", "div", "tr", "li", "table"):
            self._add("\n")

    def handle_data(self, data):
        if not self.skip:
            self._add(data)


def _quoted_from(text: str) -> int:
    """Где начинается цитата прошлых писем: дальше ссылки повторяют уже показанные."""
    m = re.search(r"\n\s*(?:-{2,}\s*(?:Original Message|Исходное сообщение)|From:|От:|Отправлено:|Sent:)", text)
    return m.start() if m else len(text)


def extract_links(body_html: str, body_text: str) -> List[dict]:
    """[{url, name, context, quoted}] без повторов. quoted — ссылка только в цитате прошлого письма."""
    found, seen = [], set()

    def add(url, name, ctx, quoted):
        url = normalize_url(url)
        if not url or url in seen or len(found) >= MAX_LINKS:
            return
        seen.add(url)
        name = _squash(name)
        if name and (name == url or URL_RE.fullmatch(name)):
            name = ""
        found.append({"url": url, "name": name[:200], "context": ctx, "quoted": quoted})

    if body_html:
        p = _Links()
        try:
            p.feed(body_html[:500000])
            p.close()
        except Exception:  # noqa: BLE001 — кривой HTML: берём то, что успели разобрать
            pass
        text = "".join(p.parts)
        cut = _quoted_from(text)
        for href, s, e in p.links:
            add(href, text[s:e], _around(text, s, e), s >= cut)
        for m in URL_RE.finditer(text):  # адреса просто текстом
            add(m.group(0), "", _around(text, m.start(), m.end()), m.start() >= cut)
    else:
        text = body_text or ""
        cut = _quoted_from(text)
        for m in URL_RE.finditer(text):
            add(m.group(0), "", _around(text, m.start(), m.end()), m.start() >= cut)
    return found


def attachment_context(name: str, body_text: str) -> str:
    """Фраза письма про вложение: где упомянуто имя файла или «во вложении»."""
    text = body_text or ""
    text = text[:_quoted_from(text)]
    stem = re.sub(r"\.[^.]{1,5}$", "", name or "").strip()
    for probe in (stem, stem.split()[0] if stem.split() else ""):
        if len(probe) >= 4:
            i = text.lower().find(probe.lower())
            if i >= 0:
                return _around(text, i, i + len(probe))
    m = ATTACH_WORDS.search(text)
    return _around(text, m.start(), m.end()) if m else ""


def extract(email: dict) -> List[dict]:
    """Всё, что показываем в разделе: вложения по номерам из письма и ссылки."""
    out = []
    body = email.get("body") or ""
    for i, a in enumerate(email.get("attachments") or []):
        kind = attachment_kind(a)
        if kind:
            out.append({"kind": kind, "att_index": i, "name": a.get("name") or "без имени", "url": None, "domain": "",
                        "size": int(a.get("size") or 0), "content_type": a.get("content_type") or "",
                        "context": attachment_context(a.get("name") or "", body), "quoted": False})
    for link in extract_links(email.get("body_html") or "", body):
        out.append({"kind": "link", "att_index": None, "name": link["name"], "url": link["url"],
                    "domain": domain_of(link["url"]), "size": 0, "content_type": "", "context": link["context"],
                    "quoted": link["quoted"]})
    return out


KIND_WORDS = {"photo": "фото", "file": "файл", "archive": "архив", "link": "ссылка"}
MAX_PROMPT_ITEMS = 15


def item_key(it: dict) -> str:
    """Ключ вложения или ссылки внутри письма: номер вложения или адрес."""
    return "a{}".format(it["att_index"]) if it.get("att_index") is not None else "u" + (it.get("url") or "")


def nkey(it: dict) -> str:
    """Ключ повтора: файл — имя без учёта регистра и размер до байта, ссылка — адрес."""
    if it.get("kind") == "link":
        return "u:" + (it.get("url") or "")
    return "f:{}|{}".format(_squash(it.get("name") or "").lower(), int(it.get("size") or 0))


def prompt_items(email: dict, excluded: Sequence[str] = ()) -> List[dict]:
    """Что попросить описать модель: вложения и ссылки письма без цитат и исключённых доменов. [{key, label}]."""
    out = []
    try:
        items = extract(email)
    except Exception:  # noqa: BLE001 — без описаний письмо всё равно разберём
        return out
    for it in items:
        if it["quoted"] or (it["kind"] == "link" and domain_excluded(it["domain"], excluded)):
            continue
        name = it["name"] or ""
        label = "{} «{}»".format(KIND_WORDS[it["kind"]], name) if name else KIND_WORDS[it["kind"]]
        if it["url"]:
            label += " — " + it["url"][:200]
        out.append({"key": item_key(it), "label": label})
        if len(out) >= MAX_PROMPT_ITEMS:
            break
    return out


def fts_query(q: str) -> str:
    """Запрос FTS5: каждое слово как начало слова, все слова обязательны."""
    words = re.findall(r"\w+", q or "", re.U)[:12]
    return " AND ".join('"{}"*'.format(w.replace('"', "")) for w in words)


__all__ = ["KINDS", "extract", "prompt_items", "item_key", "extract_links", "attachment_kind", "domain_of", "domain_excluded", "clean_domains",
           "normalize_url", "fts_query"]
