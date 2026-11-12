"""SQLite: письма, запуски скиллов, категории, настройки и журнал обработки."""
from __future__ import annotations

import json
import re
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional, Sequence

from .media import KINDS as MEDIA_KINDS, extract as extract_media, fts_query, item_key, nkey as media_nkey
from .meetings import DEFAULT_INVITE_PROMPT, MEETING_CATEGORY

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    has_attachments INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',      -- new | classifying | classified | failed
    category TEXT,
    confidence REAL,
    summary TEXT,
    reason TEXT,
    jira_key TEXT,
    recommend_action INTEGER NOT NULL DEFAULT 0,
    action_hint TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    error TEXT,
    session_id TEXT,
    classified_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS emails_received ON emails(received_at DESC);
CREATE INDEX IF NOT EXISTS emails_status ON emails(status);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting',  -- starting | running | done | error
    session_id TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_email ON runs(email_id);
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    action_title TEXT NOT NULL DEFAULT '',
    action_template TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    cwd TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '',
    include_dirs TEXT NOT NULL DEFAULT '',
    approval_mode TEXT NOT NULL DEFAULT '',
    permission TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    body_html TEXT NOT NULL DEFAULT '',
    for_new INTEGER NOT NULL DEFAULT 0,
    for_reply INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'new',         -- new | reply | reply_all | forward
    ref_email_id INTEGER REFERENCES emails(id),
    to_list TEXT NOT NULL DEFAULT '[]',
    cc_list TEXT NOT NULL DEFAULT '[]',
    bcc_list TEXT NOT NULL DEFAULT '[]',
    subject TEXT NOT NULL DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',   -- [{name, size, content_type, file}] или {ref: индекс вложения исходного}
    status TEXT NOT NULL DEFAULT 'draft',     -- draft | sending | sent | error
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS status_log_email ON status_log(email_id);
CREATE TABLE IF NOT EXISTS service_errors (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                     -- outlook | gigacode
    message TEXT NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS media (            -- вложения и ссылки писем для раздела «Вложения и ссылки»
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    kind TEXT NOT NULL,                       -- photo | file | archive | link
    att_index INTEGER,                        -- номер вложения в письме
    name TEXT NOT NULL DEFAULT '',
    url TEXT,
    domain TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    content_type TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',         -- фраза письма вокруг ссылки или про вложение
    description TEXT NOT NULL DEFAULT '',     -- описание от модели при разборе письма
    search TEXT NOT NULL DEFAULT '',          -- всё для поиска в нижнем регистре
    nkey TEXT,                                -- файл: имя и размер; ссылка: адрес
    dkey TEXT,                                -- ключ склейки повторов: nkey или группа с тем же содержимым
    fhash TEXT,                               -- SHA-256 содержимого, считается при первом скачивании
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_kind ON media(kind, received_at DESC);
CREATE INDEX IF NOT EXISTS media_email ON media(email_id);
CREATE INDEX IF NOT EXISTS media_url ON media(url);
"""
MEDIA_VERSION = 1   # поменять, если разбор писем изменится: всё переиндексируется
FTS_BODY_CHARS = 30000

DEFAULT_CLASSIFY_PROMPT = (
    "Ты разбираешь входящую почту. Определи категорию письма, напиши короткое "
    "саммари на русском (1-3 предложения) и реши, нужно ли предложить пользователю действие."
)

DEFAULT_CATEGORIES = [
    {"name": "RELEASE", "title": "Релизы",
     "prompt": "Письмо про релиз или приёмку задачи, обычно с Jira-ключом вида ABC-123. "
               "Извлеки ключ в jira_key. Рекомендуй действие, если ключ найден.",
     "action_title": "Проверить релиз",
     "action_template": "Запусти скилл проверки релиза {jira_key} по чек-листу и сохрани отчёт в файл.\n\n"
                        "Тема письма: {subject}\nСаммари: {summary}"},
    {"name": "SPAM", "title": "Спам", "prompt": "Реклама, рассылки и нежелательные письма.",
     "action_title": "", "action_template": ""},
    {"name": "AUTO", "title": "Авто", "prompt": "Автоматические уведомления систем: мониторинг, CI, Jira, календарь.",
     "action_title": "", "action_template": ""},
    {"name": "UNINFO", "title": "Инфо", "prompt": "Информационные письма, которые не требуют действий.",
     "action_title": "", "action_template": ""},
    {"name": "MEETING", "title": "Встречи",
     "prompt": "Приглашения на встречи, их переносы и отмены.", "action_title": "", "action_template": "",
     "invite_mode": "", "invite_rules": ""},
    {"name": "UNKNOWN", "title": "Неясно", "prompt": "Выбирай, если письмо не подходит ни под одну категорию или ты не уверен.",
     "action_title": "", "action_template": ""},
]

DEFAULT_SETTINGS = {
    "ews_server": "",
    "ews_email": "",
    "ews_auth": "NTLM",
    "ca_bundle": "",
    "allowed_senders": [],
    "blocked_senders": [],
    "polling_enabled": True,
    "poll_seconds": 15,
    "initial_days": 3,
    "gigacode_url": "http://127.0.0.1:8765",
    "skill_cwd": "",
    "skill_permission": "allow",
    "max_body_chars": 12000,
    "classify_prompt": DEFAULT_CLASSIFY_PROMPT,
    "entities": [],
    "calendar_auto": True,
    "work_start": "10:00",
    "work_end": "19:00",
    "free_slots_count": 3,
    "invite_prompt": DEFAULT_INVITE_PROMPT,
    "media_exclude_domains": [],
    "backfill_days": 180,
    "media_muted_senders": [],
}

CATEGORY_ENV_FIELDS = ("cwd", "files", "include_dirs", "approval_mode", "permission", "model")
CATEGORY_INVITE_FIELDS = ("invite_mode", "invite_rules")
CATEGORY_TEXT_FIELDS = ("title", "prompt", "action_title", "action_template") + CATEGORY_ENV_FIELDS + CATEGORY_INVITE_FIELDS
EMAIL_JSON_FIELDS = ("invite", "invite_decision", "invite_response", "attachments")
MAX_ATTEMPTS = 5
GONE = "?"  # письма нет там, где мы его видели
KEEP_SERVICE_ERRORS = 200


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_PREFIX_KINDS = [
    ("accept", ("accepted", "принято", "принят")),
    ("decline", ("declined", "отклонено", "отклонен", "отклонён")),
    ("tentative", ("tentative", "tentatively accepted", "под вопросом", "предварительно", "предварительно принято")),
    ("cancel", ("canceled", "cancelled", "отменено", "отменена", "отмена")),
    ("propose", ("new time proposed", "предложено новое время", "новое время предложено")),
    ("invite", ("приглашение", "invitation", "updated", "обновлено", "обновлена")),
    ("reply", ("re", "fw", "fwd", "ответ", "пересл", "отв")),
]
_PREFIX_RE = re.compile(r"^\s*(" + "|".join(re.escape(w) for _, ws in _PREFIX_KINDS for w in sorted(ws, key=len, reverse=True))
                        + r")\s*:\s*", re.I)


def meeting_topic(subject: str) -> str:
    """Тема без «RE:», «Принято:», «Приглашение:» и подобных приставок."""
    text = (subject or "").strip()
    while True:
        m = _PREFIX_RE.match(text)
        if not m:
            return text.strip()
        text = text[m.end():]


def meeting_kind(subject: str) -> str:
    m = _PREFIX_RE.match(subject or "")
    if not m:
        return ""
    word = m.group(1).lower()
    return next(k for k, ws in _PREFIX_KINDS if word in ws)


class Database:
    def __init__(self, path: str):
        self.path = path
        fresh = not os.path.exists(path)
        if fresh:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            columns = {r["name"] for r in c.execute("PRAGMA table_info(emails)")}
            if "entities" not in columns:
                c.execute("ALTER TABLE emails ADD COLUMN entities TEXT NOT NULL DEFAULT '{}'")
            if "category_manual" not in columns:
                c.execute("ALTER TABLE emails ADD COLUMN category_manual INTEGER NOT NULL DEFAULT 0")
            for column in EMAIL_JSON_FIELDS + ("invite_state", "item_id", "cc", "body_html",  # встречи, HTML, вложения
                                               "folder", "conversation_id", "conversation_topic"):  # папки и цепочки
                if column not in columns:
                    c.execute("ALTER TABLE emails ADD COLUMN {} TEXT".format(column))
            if "is_read" not in columns:  # старые письма считаем прочитанными, чтобы не всплыли разом
                c.execute("ALTER TABLE emails ADD COLUMN is_read INTEGER NOT NULL DEFAULT 1")
            if "flag" not in columns:  # 0 — нет, 1 — выполнено, 2 — с флажком, как в Exchange
                c.execute("ALTER TABLE emails ADD COLUMN flag INTEGER NOT NULL DEFAULT 0")
            if "media_notes" not in columns:  # описания вложений и ссылок от модели: {ключ: описание}
                c.execute("ALTER TABLE emails ADD COLUMN media_notes TEXT")
            media_cols = {r["name"] for r in c.execute("PRAGMA table_info(media)")}
            if "description" not in media_cols:
                c.execute("ALTER TABLE media ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            for column in ("nkey", "dkey", "fhash"):  # склейка одинаковых файлов из разных писем
                if column not in media_cols:
                    c.execute("ALTER TABLE media ADD COLUMN {} TEXT".format(column))
            c.execute("CREATE INDEX IF NOT EXISTS media_dkey ON media(dkey)")
            c.execute("CREATE INDEX IF NOT EXISTS media_nkey ON media(nkey)")
            c.execute("CREATE INDEX IF NOT EXISTS media_fhash ON media(fhash)")
            for r in c.execute("SELECT id, kind, name, size, url FROM media WHERE nkey IS NULL").fetchall():
                key = media_nkey(dict(r))
                c.execute("UPDATE media SET nkey=?, dkey=COALESCE((SELECT dkey FROM media WHERE nkey=? AND dkey IS NOT NULL "
                          "LIMIT 1), ?) WHERE id=?", (key, key, key, r["id"]))
            if "media_v" not in columns:  # версия разбора вложений и ссылок, 0 — ещё не разобрано
                c.execute("ALTER TABLE emails ADD COLUMN media_v INTEGER NOT NULL DEFAULT 0")
            c.execute("CREATE INDEX IF NOT EXISTS emails_media ON emails(media_v)")
            try:  # полнотекстовый поиск; в сборках SQLite без FTS5 ищем по-старому, через LIKE
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(subject, people, body, files, "
                          "tokenize='unicode61 remove_diacritics 2')")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            c.execute("CREATE INDEX IF NOT EXISTS emails_item ON emails(item_id)")
            c.execute("CREATE INDEX IF NOT EXISTS emails_folder ON emails(folder, received_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS emails_conv ON emails(conversation_id)")
            known = {r["name"] for r in c.execute("PRAGMA table_info(categories)")}
            for column in CATEGORY_ENV_FIELDS + CATEGORY_INVITE_FIELDS:  # базы, созданные до этих полей
                if column not in known:
                    c.execute("ALTER TABLE categories ADD COLUMN {} TEXT NOT NULL DEFAULT ''".format(column))
            if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
                self._write_categories(c, DEFAULT_CATEGORIES)
            elif not c.execute("SELECT 1 FROM settings WHERE key='_meeting_category'").fetchone():
                # в старых базах категории встреч не было, добавляем один раз: удалённую не возвращаем
                if not c.execute("SELECT 1 FROM categories WHERE name='MEETING'").fetchone():
                    meeting = next(x for x in DEFAULT_CATEGORIES if x["name"] == "MEETING")
                    position = c.execute("SELECT COALESCE(MAX(position), -1) + 1 p FROM categories").fetchone()["p"]
                    self._write_categories(c, [meeting], position)
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('_meeting_category', 'true')")
            c.execute("UPDATE emails SET invite_state='error', invite_response=? WHERE invite_state='sending'",
                      (_dump({"error": "Сервис перезапущен во время отправки ответа. Проверьте календарь в Outlook"}),))
            c.execute("UPDATE drafts SET status='error', error='Сервис перезапущен во время отправки. "
                      "Проверьте «Отправленные» в Outlook, прежде чем отправлять снова' WHERE status='sending'")
            # письмо, зависшее в обработке при падении, возвращаем в очередь
            c.execute("UPDATE emails SET status='new' WHERE status='classifying'")
            c.execute("UPDATE runs SET status='error', error='Сервис перезапущен во время выполнения', "
                      "finished_at=? WHERE status IN ('starting','running')", (now_iso(),))

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    # --- настройки --------------------------------------------------------
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        with self.conn() as c:
            for row in c.execute("SELECT key, value FROM settings"):
                out[row["key"]] = json.loads(row["value"])
        return out

    def set_settings(self, values: dict) -> None:
        with self.conn() as c:
            c.executemany("INSERT INTO settings(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])

    def invite_categories(self, limit: int = 3000) -> tuple:
        """Категории писем-приглашений: по id встречи в календаре и по теме. Свежие письма важнее."""
        by_id, by_topic = {}, {}
        with self.conn() as c:
            rows = c.execute("SELECT subject, category, invite FROM emails WHERE invite IS NOT NULL AND category IS NOT NULL "
                             "AND status='classified' ORDER BY received_at ASC LIMIT ?", (limit,)).fetchall()
        for r in rows:
            try:
                cal_id = (json.loads(r["invite"]) or {}).get("calendar_id")
            except ValueError:
                cal_id = None
            if cal_id:
                by_id[cal_id] = r["category"]
            topic = meeting_topic(r["subject"]).lower()
            if topic:
                by_topic[topic] = r["category"]
        return by_id, by_topic

    def meeting_mail(self, conversation_id: Optional[str], subject: str, limit: int = 30) -> List[dict]:
        """Письма по встрече: приглашение, ответы участников, переписка. По цепочке Exchange или по теме."""
        topic = meeting_topic(subject)
        if not topic and not conversation_id:
            return []
        like = "%" + topic.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, subject, sender, sender_name, received_at, substr(body, 1, 400) body, invite, is_read, "
                "conversation_id FROM emails WHERE (conversation_id = ? AND ? <> '') OR conversation_topic = ? "
                "OR subject LIKE ? ESCAPE '\\' ORDER BY received_at DESC LIMIT 200",
                (conversation_id or "", conversation_id or "", topic, like)).fetchall()
        out = []
        for r in rows:
            same_conv = conversation_id and r["conversation_id"] == conversation_id
            if not same_conv and meeting_topic(r["subject"]).lower() != topic.lower():
                continue
            kind = meeting_kind(r["subject"]) or ("invite" if r["invite"] else "mail")
            out.append({"id": r["id"], "subject": r["subject"], "sender": r["sender"], "sender_name": r["sender_name"],
                        "received_at": r["received_at"], "snippet": " ".join((r["body"] or "").split())[:200],
                        "is_read": bool(r["is_read"]), "kind": kind})
            if len(out) >= limit:
                break
        return out

    def state(self, key: str, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", ("_" + key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.set_settings({"_" + key: value})

    # --- категории --------------------------------------------------------
    def categories(self) -> List[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY position, name")]

    def category(self, name: Optional[str]) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def save_category(self, original: Optional[str], item: dict) -> None:
        """Создаёт или обновляет категорию. При смене кода письма переезжают вместе с ней."""
        values = [str(item.get(f) or "") for f in CATEGORY_TEXT_FIELDS]
        values[0] = values[0] or item["name"]
        with self.conn() as c:
            if original:
                sets = ", ".join("{}=?".format(f) for f in ("name",) + CATEGORY_TEXT_FIELDS)
                c.execute("UPDATE categories SET {} WHERE name=?".format(sets), [item["name"]] + values + [original])
                if original != item["name"]:
                    c.execute("UPDATE emails SET category=? WHERE category=?", (item["name"], original))
            else:
                position = c.execute("SELECT COALESCE(MAX(position), -1) + 1 p FROM categories").fetchone()["p"]
                columns = ", ".join(("name", "position") + CATEGORY_TEXT_FIELDS)
                marks = ", ".join("?" * (len(CATEGORY_TEXT_FIELDS) + 2))
                c.execute("INSERT INTO categories({}) VALUES({})".format(columns, marks),
                          [item["name"], position] + values)

    def delete_category(self, name: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM categories WHERE name=?", (name,))

    def order_categories(self, names: List[str]) -> None:
        with self.conn() as c:
            c.executemany("UPDATE categories SET position=? WHERE name=?", [(i, n) for i, n in enumerate(names)])

    def recent_emails(self, limit: int = 30) -> List[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, subject, sender, category FROM emails ORDER BY received_at DESC, id DESC LIMIT ?", (limit,))]

    @staticmethod
    def _write_categories(c: sqlite3.Connection, items: List[dict], start: int = 0) -> None:
        for i, it in enumerate(items, start):
            c.execute("INSERT INTO categories(name, title, prompt, action_title, action_template, position) "
                      "VALUES(?, ?, ?, ?, ?, ?)",
                      (it["name"], it.get("title") or it["name"], it.get("prompt", ""),
                       it.get("action_title", ""), it.get("action_template", ""), i))

    # --- письма -----------------------------------------------------------
    def add_email(self, m: dict, status: str = "new") -> Optional[int]:
        """Возвращает id нового письма или None, если такое уже есть.
        status=stored — письмо из папки или поиска: модель его не разбирает, пока не попросят."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO emails(message_id, subject, sender, sender_name, recipients, received_at, "
                "body, has_attachments, invite, item_id, cc, body_html, attachments, folder, is_read, conversation_id, "
                "conversation_topic, status, created_at, flag) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m["message_id"], m.get("subject", ""), m.get("sender", ""), m.get("sender_name", ""),
                 m.get("recipients", ""), m["received_at"], m.get("body", ""),
                 int(bool(m.get("has_attachments"))), _dump(m.get("invite")), m.get("item_id"), m.get("cc", ""),
                 m.get("body_html") or None, _dump(m.get("attachments")), m.get("folder"),
                 int(bool(m.get("is_read"))), m.get("conversation_id"), m.get("conversation_topic") or "",
                 status, now_iso(), int(m.get("flag") or 0)))
            if cur.rowcount == 0:
                return None
            self._log(c, cur.lastrowid, "received", m.get("sender", ""))
            return cur.lastrowid

    def list_emails(self, category: Optional[str] = None, status: Optional[str] = None, q: str = "",
                    limit: int = 100, offset: int = 0, folder: Optional[str] = None, inbox: Optional[str] = None,
                    hidden: Sequence[str] = (), ids: Optional[Sequence[int]] = None, unread: bool = False,
                    oldest_first: bool = False, flagged: bool = False) -> dict:
        where, args = [], []
        if ids is not None:
            where.append("id IN ({})".format(",".join("?" * len(ids)) or "NULL"))
            args += list(ids)
        elif folder is not None:
            where.append(_folder_clause(folder, inbox))
            args += [folder]
        else:  # категории и очередь: письма из удалённых и пропавшие не показываем
            gone = [GONE] + [h for h in hidden if h]
            where.append("COALESCE(folder, '') NOT IN ({})".format(",".join("?" * len(gone))))
            args += gone
        if unread:
            where.append("is_read = 0")
        if flagged:
            where.append("flag = 2")
        if category == MEETING_CATEGORY:  # «Встречи» — все приглашения, у них своя тематическая категория
            where.append("(category = ? OR invite IS NOT NULL)")
            args.append(category)
        elif category:
            where.append("category = ?")
            args.append(category)
        if status:
            where.append("status = ?")
            args.append(status)
        if q:
            match = fts_query(q) if self.fts else ""
            where.append("(subject LIKE ? OR sender LIKE ? OR summary LIKE ? OR jira_key LIKE ?{})".format(
                " OR id IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)" if match else ""))
            args += ["%{}%".format(q)] * 4 + ([match] if match else [])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM emails" + clause, args).fetchone()["n"]
            rows = c.execute(
                "SELECT id, subject, sender, sender_name, received_at, has_attachments, status, category, confidence, "
                "folder, is_read, conversation_id, flag, "
                "summary, jira_key, recommend_action, error, attempts, invite, invite_decision, invite_state, "
                "substr(replace(replace(body, char(10), ' '), char(13), ' '), 1, 160) AS preview, "
                "(SELECT status FROM runs r WHERE r.email_id = emails.id ORDER BY r.id DESC LIMIT 1) AS run_status "
                "FROM emails" + clause + " ORDER BY received_at {0}, id {0} LIMIT ? OFFSET ?".format(
                    "ASC" if oldest_first else "DESC"),
                args + [limit, offset]).fetchall()
        emails = []
        for r in rows:
            e = _load(dict(r))
            invite, decision = e.pop("invite"), e.pop("invite_decision")
            # в списке хватает вида приглашения и того, что с ним сейчас
            e["invite"] = {"kind": invite["kind"], "start": invite.get("start"),
                           "conflicts": len(invite.get("conflicts") or []),
                           "suggested": (decision or {}).get("action")} if invite else None
            emails.append(e)
        return {"emails": emails, "total": total}

    def counts(self, hidden: Sequence[str] = ()) -> dict:
        gone = [GONE] + [h for h in hidden if h]
        not_gone = "COALESCE(folder, '') NOT IN ({})".format(",".join("?" * len(gone)))
        with self.conn() as c:
            by_cat = {r["category"] or "": r["n"] for r in c.execute(
                "SELECT category, COUNT(*) n FROM emails WHERE status='classified' AND " + not_gone +
                " GROUP BY category", gone)}
            extra = c.execute("SELECT COUNT(*) n FROM emails WHERE status='classified' AND invite IS NOT NULL AND "
                              "COALESCE(category, '') <> ? AND " + not_gone, [MEETING_CATEGORY] + gone).fetchone()["n"]
            if extra:
                by_cat[MEETING_CATEGORY] = by_cat.get(MEETING_CATEGORY, 0) + extra
            by_status = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
        return {"categories": by_cat, "statuses": by_status}

    def get_email(self, eid: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            if not row:
                return None
            email = _load(dict(row))
            email["runs"] = [dict(r) for r in c.execute("SELECT * FROM runs WHERE email_id=? ORDER BY id DESC", (eid,))]
            email["log"] = [dict(r) for r in c.execute("SELECT * FROM status_log WHERE email_id=? ORDER BY id", (eid,))]
        return email

    def claim_next(self) -> Optional[dict]:
        """Забирает следующее письмо на классификацию (старые раньше новых)."""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM emails WHERE status='new' AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY received_at LIMIT 1", (now_iso(),)).fetchone()
            if not row:
                return None
            c.execute("UPDATE emails SET status='classifying', attempts=attempts+1 WHERE id=?", (row["id"],))
            return _load(dict(row))

    def save_classification(self, eid: int, res: dict, session_id: Optional[str]) -> None:
        with self.conn() as c:
            # Категорию, выбранную вручную, модель не меняет. Саммари и сущности обновляются.
            c.execute("UPDATE emails SET status='classified', category=CASE WHEN category_manual=1 THEN category ELSE ? END, "
                      "confidence=?, summary=?, reason=?, jira_key=?, "
                      "recommend_action=?, action_hint=?, entities=?, invite_decision=?, session_id=?, classified_at=?, "
                      "media_notes=COALESCE(?, media_notes), error=NULL, next_retry_at=NULL WHERE id=?",
                      (res["category"], res["confidence"], res["summary"], res["reason"], res["jira_key"],
                       int(res["recommend_action"]), res["action_hint"], json.dumps(res.get("entities") or {}, ensure_ascii=False),
                       _dump(res.get("invite")), session_id, now_iso(), _dump(res.get("media") or None), eid))
            for key, note in (res.get("media") or {}).items():  # уже разобранные строки раздела обновляем сразу
                self._set_media_note(c, eid, key, note)
            self._log(c, eid, "classified", "{} ({})".format(res["category"], res["confidence"]))

    def classification_failed(self, eid: int, error: str, attempts: int) -> None:
        final = attempts >= MAX_ATTEMPTS
        retry_at = None if final else (datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** (attempts - 1))
                                       ).isoformat(timespec="seconds")
        with self.conn() as c:
            c.execute("UPDATE emails SET status=?, error=?, next_retry_at=? WHERE id=?",
                      ("failed" if final else "new", error[:4000], retry_at, eid))
            self._log(c, eid, "failed" if final else "retry", error[:500])

    def classification_deferred(self, eid: int, attempts: int, seconds: int, error: str) -> None:
        """Сервис недоступен: письмо не виновато, поэтому попытка не считается."""
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with self.conn() as c:
            c.execute("UPDATE emails SET status='new', attempts=?, next_retry_at=?, error=? WHERE id=?",
                      (attempts, retry_at, error[:4000], eid))

    def set_category(self, eid: int, name: str) -> None:
        with self.conn() as c:
            old = c.execute("SELECT category FROM emails WHERE id=?", (eid,)).fetchone()["category"]
            c.execute("UPDATE emails SET category=?, category_manual=1, "
                      "error=CASE WHEN status='failed' THEN NULL ELSE error END, "
                      "status=CASE WHEN status='failed' THEN 'classified' ELSE status END WHERE id=?", (name, eid))
            self._log(c, eid, "category", "{} → {}, вручную".format(old or "нет", name))

    def reset_for_classification(self, eid: int) -> None:
        """Кнопка у письма: решение снова за моделью, даже если категорию меняли вручную."""
        with self.conn() as c:
            c.execute("UPDATE emails SET status='new', attempts=0, next_retry_at=NULL, error=NULL, category_manual=0 "
                      "WHERE id=?", (eid,))
            self._log(c, eid, "reclassify", None)

    def reclassify_all(self) -> int:
        with self.conn() as c:
            cur = c.execute("UPDATE emails SET status='new', attempts=0, next_retry_at=NULL, error=NULL "
                            "WHERE status IN ('classified', 'failed')")
            return cur.rowcount

    # --- ящик ---------------------------------------------------------------
    def unread_count(self, folder: str, inbox: Optional[str]) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM emails WHERE is_read=0 AND " + _folder_clause(folder, inbox),
                             (folder,)).fetchone()["n"]

    def upsert_mail(self, m: dict, inbox: Optional[str] = None, watermark: Optional[str] = None) -> Optional[int]:
        """Письмо из папки, поиска или цепочки. Существующее обновляем (папка, id, прочитано),
        новое добавляем без разбора моделью. Новые письма входящих оставляем обычному опросу: их надо разобрать."""
        folder = m.get("folder")
        with self.conn() as c:
            row = None
            if m.get("item_id"):
                row = c.execute("SELECT id FROM emails WHERE item_id=?", (m["item_id"],)).fetchone()
            if row is None:
                legacy = " OR folder IS NULL" if folder and folder == inbox else ""
                row = c.execute("SELECT id FROM emails WHERE message_id=? AND (folder IN (?, ?)" + legacy + ")",
                                (m["message_id"], folder or "", GONE)).fetchone()
            if row is not None:
                c.execute("UPDATE emails SET folder=?, item_id=?, is_read=?, conversation_id=COALESCE(?, conversation_id), "
                          "conversation_topic=COALESCE(NULLIF(?, ''), conversation_topic), flag=COALESCE(?, flag) WHERE id=?",
                          (folder, m.get("item_id"), int(bool(m.get("is_read"))), m.get("conversation_id"),
                           m.get("conversation_topic") or "", m.get("flag"), row["id"]))
                return row["id"]
            if folder and folder == inbox and watermark and m["received_at"] >= watermark:
                return None
            taken = c.execute("SELECT 1 FROM emails WHERE message_id=?", (m["message_id"],)).fetchone()
        if taken:  # то же письмо в другой папке, например копия у себя в «Отправленных»
            m = dict(m, message_id="{}#{}".format(m["message_id"], (m.get("item_id") or "")[-24:]))
        return self.add_email(m, status="stored")

    def item_ids(self, folder: str, inbox: Optional[str], newest: Optional[str], oldest: str) -> dict:
        with self.conn() as c:
            rows = c.execute("SELECT id, item_id FROM emails WHERE " + _folder_clause(folder, inbox) +
                             " AND received_at >= ? AND (? IS NULL OR received_at < ?)",
                             (folder, oldest, newest, newest)).fetchall()
        return {r["item_id"]: r["id"] for r in rows}

    def set_flag(self, eid: int, flag: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET flag=? WHERE id=?", (int(flag), eid))

    def flagged_count(self, hidden: Sequence[str] = ()) -> int:
        gone = [GONE] + [h for h in hidden if h]
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM emails WHERE flag=2 AND COALESCE(folder, '') NOT IN ({})".format(
                ",".join("?" * len(gone))), gone).fetchone()["n"]

    def new_mail_since(self, after: int, inbox: Optional[str], limit: int = 10) -> List[dict]:
        """Новые непрочитанные письма входящих для уведомлений браузера: только пришедшие опросом, не из папок."""
        with self.conn() as c:
            rows = c.execute("SELECT id, subject, sender, sender_name, summary, category FROM emails "
                             "WHERE id > ? AND is_read = 0 AND status != 'stored' AND "
                             "(folder IS NULL OR folder = ?) ORDER BY id DESC LIMIT ?",
                             (after, inbox or "", limit)).fetchall()
        return [dict(r) for r in rows]

    def last_email_id(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) n FROM emails").fetchone()["n"]

    # --- подписи ---------------------------------------------------------
    def signatures(self) -> List[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM signatures ORDER BY name COLLATE NOCASE, id")]

    def save_signature(self, sid: Optional[int], name: str, body_html: str, for_new: bool, for_reply: bool) -> int:
        with self.conn() as c:
            if for_new:  # подпись по умолчанию одна, как в Outlook
                c.execute("UPDATE signatures SET for_new=0")
            if for_reply:
                c.execute("UPDATE signatures SET for_reply=0")
            if sid:
                cur = c.execute("UPDATE signatures SET name=?, body_html=?, for_new=?, for_reply=?, updated_at=? "
                                "WHERE id=?", (name, body_html, int(for_new), int(for_reply), now_iso(), sid))
                if cur.rowcount == 0:
                    raise ValueError("Подпись не найдена")
                return sid
            return c.execute("INSERT INTO signatures(name, body_html, for_new, for_reply, updated_at) "
                             "VALUES(?, ?, ?, ?, ?)", (name, body_html, int(for_new), int(for_reply), now_iso())).lastrowid

    def delete_signature(self, sid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM signatures WHERE id=?", (sid,))

    def default_signature(self, reply: bool) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM signatures WHERE {}=1".format("for_reply" if reply else "for_new")).fetchone()
        return dict(row) if row else None

    def set_read(self, eid: int, read: bool) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET is_read=? WHERE id=?", (int(read), eid))

    def set_read_by_item(self, item_id: str, read: bool) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET is_read=? WHERE item_id=?", (int(read), item_id))

    def set_folder(self, eid: int, folder: str, item_id: Optional[str], event: Optional[str] = None,
                   detail: Optional[str] = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET folder=?, item_id=? WHERE id=?", (folder, item_id, eid))
            if event:
                self._log(c, eid, event, detail)

    def mark_gone(self, ids: Sequence[int]) -> None:
        """Письма, которых больше нет в папке: удалили или переместили в Outlook. Найдутся, если открыть их папку."""
        with self.conn() as c:
            c.executemany("UPDATE emails SET folder=? WHERE id=?", [(GONE, i) for i in ids])

    def mark_gone_by_item(self, item_id: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET folder=? WHERE item_id=?", (GONE, item_id))

    def forget(self, eid: int) -> None:
        """Письмо удалено навсегда: в базе оставляем журнал и запуски, письмо прячем."""
        with self.conn() as c:
            c.execute("UPDATE emails SET folder=?, item_id=NULL WHERE id=?", (GONE, eid))
            self._log(c, eid, "deleted", "навсегда")

    # --- вложения, ссылки и полнотекстовый индекс ------------------------------
    def index_pending(self, limit: int = 200) -> int:
        """Разбирает письма, которые ещё не попали в раздел вложений и в поиск. Возвращает, сколько разобрано."""
        from .mail_source import sender_matches
        muted = self.settings().get("media_muted_senders") or []
        with self.conn() as c:
            rows = c.execute("SELECT id, subject, sender, sender_name, recipients, cc, received_at, body, body_html, "
                             "attachments, media_notes FROM emails WHERE media_v < ? LIMIT ?",
                             (MEDIA_VERSION, limit)).fetchall()
        for r in rows:
            e = _load(dict(r))
            try:  # серый список: письмо ищется, но его вложения и ссылки в раздел не попадают
                items = [] if sender_matches(e["sender"], muted) else extract_media(e)
            except Exception:  # noqa: BLE001 — одно странное письмо не должно останавливать разбор остальных
                items = []
            head = " ".join([e["subject"] or "", e["sender_name"] or "", e["sender"] or ""]).lower()
            notes = json.loads(e.get("media_notes") or "{}")
            with self.conn() as c:
                hashes = {r["att_index"]: r["fhash"] for r in c.execute(  # при повторном разборе хэши не теряем
                    "SELECT att_index, fhash FROM media WHERE email_id=? AND fhash IS NOT NULL", (e["id"],))}
                c.execute("DELETE FROM media WHERE email_id=?", (e["id"],))
                for it in items:
                    note = notes.get(item_key(it), "")
                    search = " ".join([it["name"], it["url"] or "", it["context"], note, head]).lower()
                    nkey, fhash = media_nkey(it), hashes.get(it["att_index"])
                    same = c.execute("SELECT dkey FROM media WHERE fhash=? LIMIT 1", (fhash,)).fetchone() if fhash else None
                    same = same or c.execute("SELECT dkey FROM media WHERE nkey=? LIMIT 1", (nkey,)).fetchone()
                    c.execute("INSERT INTO media(email_id, kind, att_index, name, url, domain, size, content_type, "
                              "context, description, search, received_at, nkey, dkey, fhash) "
                              "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (e["id"], it["kind"], it["att_index"], it["name"], it["url"], it["domain"], it["size"],
                               it["content_type"], it["context"], note, search, e["received_at"], nkey,
                               same["dkey"] if same else nkey, fhash))
                if self.fts:
                    files = " ".join(it["name"] or it["domain"] for it in items)
                    c.execute("DELETE FROM emails_fts WHERE rowid=?", (e["id"],))
                    c.execute("INSERT INTO emails_fts(rowid, subject, people, body, files) VALUES(?, ?, ?, ?, ?)",
                              (e["id"], e["subject"] or "",
                               " ".join([e["sender_name"] or "", e["sender"] or "", e["recipients"] or "", e["cc"] or ""]),
                               (e["body"] or "")[:FTS_BODY_CHARS], files))
                c.execute("UPDATE emails SET media_v=? WHERE id=?", (MEDIA_VERSION, e["id"]))
        return len(rows)

    @staticmethod
    def _set_media_note(c, eid: int, key: str, note: str) -> None:
        if key.startswith("a"):
            where, value = "att_index = ?", int(key[1:])
        else:
            where, value = "url = ?", key[1:]
        c.execute("UPDATE media SET description=?, search=search || ' ' || ? WHERE email_id=? AND " + where,
                  (note, note.lower(), eid, value))

    def apply_muted(self, old: Sequence[str], new: Sequence[str]) -> dict:
        """Серый список изменился: вложения новых «серых» отправителей убираем из раздела, вычеркнутых — разбираем заново."""
        from .mail_source import sender_matches
        with self.conn() as c:
            senders = [r["s"] for r in c.execute("SELECT DISTINCT lower(sender) s FROM emails")]
        muted = [s for s in senders if sender_matches(s, new) and not sender_matches(s, old)]
        freed = [s for s in senders if sender_matches(s, old) and not sender_matches(s, new)]
        removed = 0
        with self.conn() as c:
            for i in range(0, len(muted), 500):
                part = muted[i:i + 500]
                removed += c.execute("DELETE FROM media WHERE email_id IN (SELECT id FROM emails WHERE lower(sender) IN "
                                     "({}))".format(",".join("?" * len(part))), part).rowcount
            for i in range(0, len(freed), 500):
                part = freed[i:i + 500]
                c.execute("UPDATE emails SET media_v=0 WHERE lower(sender) IN ({})".format(",".join("?" * len(part))), part)
        return {"removed": removed, "restored_senders": len(freed)}

    def set_media_hash(self, eid: int, index: int, fhash: str) -> None:
        """Хэш скачанного вложения. Если такое же содержимое уже было под другим именем — склеиваем группы."""
        with self.conn() as c:
            row = c.execute("SELECT id, dkey, fhash FROM media WHERE email_id=? AND att_index=?", (eid, index)).fetchone()
            if not row or row["fhash"] == fhash:
                return
            c.execute("UPDATE media SET fhash=? WHERE id=?", (fhash, row["id"]))
            other = c.execute("SELECT dkey FROM media WHERE fhash=? AND dkey<>? LIMIT 1", (fhash, row["dkey"])).fetchone()
            if other:
                c.execute("UPDATE media SET dkey=? WHERE dkey=?", (other["dkey"], row["dkey"]))

    def media_copies(self, media_id: int, hidden: Sequence[str] = ()) -> List[dict]:
        """Все письма с тем же файлом или ссылкой, от первого к последнему."""
        gone = [GONE] + [h for h in hidden if h]
        with self.conn() as c:
            row = c.execute("SELECT dkey FROM media WHERE id=?", (media_id,)).fetchone()
            if not row:
                return []
            rows = c.execute(
                "SELECT m.id, m.email_id, m.att_index, m.name, m.size, m.content_type, m.received_at, e.subject, e.sender, "
                "e.sender_name, e.folder FROM media m JOIN emails e ON e.id = m.email_id WHERE m.dkey=? AND "
                "COALESCE(e.folder, '') NOT IN ({}) ORDER BY m.received_at, m.id LIMIT 200".format(",".join("?" * len(gone))),
                [row["dkey"]] + gone).fetchall()
        out, seen = [], set()
        for r in rows:  # одно письмо — одна строка, даже если файл приложен в нём дважды
            if r["email_id"] not in seen:
                seen.add(r["email_id"])
                out.append(dict(r))
        return out

    def list_media(self, kind: str, q: str = "", sender: str = "", conversation_id: str = "", email_id: int = 0,
                   excluded: Sequence[str] = (), hidden: Sequence[str] = (), limit: int = 60, offset: int = 0) -> dict:
        """Вложения или ссылки, новые сверху. Повторы (тот же файл в ответах и пересылках, та же ссылка) — одной
        строкой по первому письму, copies — в скольких письмах встречается."""
        gone = [GONE] + [h for h in hidden if h]
        where = ["COALESCE(e.folder, '') NOT IN ({})".format(",".join("?" * len(gone)))]
        args: list = list(gone)
        if q:
            for word in q.lower().split()[:8]:
                where.append("m.search LIKE ?")
                args.append("%{}%".format(word))
        if sender:
            where.append("lower(e.sender) = ?")
            args.append(sender.lower())
        if conversation_id:
            where.append("e.conversation_id = ?")
            args.append(conversation_id)
        if email_id:
            where.append("m.email_id = ?")
            args.append(email_id)
        for d in excluded:  # ссылки на исключённые домены не показываем
            where.append("NOT (m.domain = ? OR m.domain LIKE ?)")
            args += [d, "%." + d]
        base = " FROM media m JOIN emails e ON e.id = m.email_id WHERE " + " AND ".join(where)
        with self.conn() as c:
            counts = {r["kind"]: r["n"] for r in c.execute(
                "SELECT m.kind, COUNT(DISTINCT m.dkey) n" + base +
                " GROUP BY m.kind", args)}
            cols = ("m.id, m.kind, m.email_id, m.att_index, m.name, m.url, m.domain, m.size, m.content_type, m.context, "
                    "m.description, "
                    "e.subject, e.sender, e.sender_name, e.summary, e.category, e.conversation_id")
            # одна строка на группу: столбцы берутся из самого раннего письма (MIN — единственный min/max в запросе)
            rows = c.execute("SELECT " + cols + ", MIN(m.received_at) AS received_at, COUNT(DISTINCT m.email_id) AS copies, "
                             "(SELECT x.description FROM media x WHERE x.dkey = m.dkey AND x.description <> '' LIMIT 1) "
                             "AS any_description" + base + " AND m.kind=? GROUP BY m.dkey "
                             "ORDER BY received_at DESC LIMIT ? OFFSET ?", args + [kind, limit, offset]).fetchall()
        items = []
        for r in rows:
            it = dict(r)
            it["description"] = it["description"] or it.pop("any_description") or ""
            it.pop("any_description", None)
            items.append(it)
        return {"items": items, "total": counts.get(kind, 0),
                "counts": {k: counts.get(k, 0) for k in MEDIA_KINDS}}

    def media_pending(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM emails WHERE media_v < ?", (MEDIA_VERSION,)).fetchone()["n"]

    def thread_ids(self, conversation_id: Optional[str], topic: str) -> List[int]:
        with self.conn() as c:
            if conversation_id:
                rows = c.execute("SELECT id FROM emails WHERE conversation_id=? AND COALESCE(folder, '') != ? "
                                 "ORDER BY received_at", (conversation_id, GONE)).fetchall()
            else:
                rows = []
        return [r["id"] for r in rows]

    # --- приглашения ------------------------------------------------------
    def set_invite(self, eid: int, invite: dict) -> None:
        """Перед классификацией: пересечения и свободные окна по свежему календарю."""
        with self.conn() as c:
            c.execute("UPDATE emails SET invite=? WHERE id=?", (_dump(invite), eid))

    def claim_invite(self, eid: int, response: dict) -> bool:
        """Помечает, что ответ отправляется. False, если его уже отправляют: две кнопки или кнопка и авто-режим."""
        with self.conn() as c:
            cur = c.execute("UPDATE emails SET invite_state='sending', invite_response=? "
                            "WHERE id=? AND COALESCE(invite_state, '') != 'sending'", (_dump(response), eid))
            return cur.rowcount == 1

    def finish_invite(self, eid: int, response: dict, ok: bool, detail: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET invite_state=?, invite_response=? WHERE id=?",
                      ("sent" if ok else "error", _dump(response), eid))
            self._log(c, eid, "invite_sent" if ok else "invite_failed", detail[:500])

    # --- черновики --------------------------------------------------------
    def add_draft(self, kind: str, ref: Optional[int], fields: dict) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO drafts(kind, ref_email_id, created_at, updated_at) VALUES(?, ?, ?, ?)",
                            (kind, ref, now_iso(), now_iso()))
            did = cur.lastrowid
        self.update_draft(did, fields)
        return did

    def update_draft(self, did: int, fields: dict) -> None:
        cols, args = [], []
        for key, column in DRAFT_FIELDS.items():
            if key in fields:
                cols.append(column + "=?")
                value = fields[key]
                args.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
        if not cols:
            return
        with self.conn() as c:
            c.execute("UPDATE drafts SET {}, updated_at=? WHERE id=?".format(", ".join(cols)), args + [now_iso(), did])

    def get_draft(self, did: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        return _draft(dict(row)) if row else None

    def list_drafts(self, sent: bool, limit: int = 200) -> List[dict]:
        where = "status='sent'" if sent else "status!='sent'"
        order = "sent_at" if sent else "updated_at"
        with self.conn() as c:
            rows = c.execute("SELECT id, kind, ref_email_id, to_list, cc_list, subject, status, error, updated_at, sent_at, "
                             "attachments, substr(body_html, 1, 2000) AS body_html FROM drafts WHERE {} "
                             "ORDER BY {} DESC LIMIT ?".format(where, order), (limit,)).fetchall()
        return [_draft(dict(r)) for r in rows]

    def claim_draft(self, did: int) -> bool:
        """Помечает черновик отправляемым. False, если его уже отправляют или он отправлен."""
        with self.conn() as c:
            cur = c.execute("UPDATE drafts SET status='sending', error=NULL, updated_at=? "
                            "WHERE id=? AND status IN ('draft', 'error')", (now_iso(), did))
            return cur.rowcount == 1

    def finish_draft(self, did: int, ok: bool, error: Optional[str] = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE drafts SET status=?, error=?, sent_at=?, updated_at=? WHERE id=?",
                      ("sent" if ok else "error", error, now_iso() if ok else None, now_iso(), did))
            row = c.execute("SELECT ref_email_id, kind FROM drafts WHERE id=?", (did,)).fetchone()
            if ok and row and row["ref_email_id"]:
                self._log(c, row["ref_email_id"], "replied", row["kind"])

    def delete_draft(self, did: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM drafts WHERE id=?", (did,))

    def contact_rows(self, limit: int = 3000) -> List[dict]:
        """Адреса из последних писем и отправленных черновиков: для подсказки адресатов."""
        with self.conn() as c:
            mails = [dict(r) for r in c.execute(
                "SELECT sender, sender_name, recipients, cc, received_at FROM emails "
                "ORDER BY received_at DESC LIMIT ?", (limit,))]
            sent = [dict(r) for r in c.execute(
                "SELECT to_list, cc_list, bcc_list, sent_at FROM drafts WHERE status='sent' "
                "ORDER BY sent_at DESC LIMIT ?", (limit,))]
        return mails + sent

    # --- ошибки сервисов --------------------------------------------------
    def record_service_error(self, source: str, message: str) -> None:
        """Одинаковая ошибка подряд увеличивает счётчик, а не плодит строки."""
        message = message[:2000]
        with self.conn() as c:
            row = c.execute("SELECT id, message FROM service_errors WHERE source=? AND resolved_at IS NULL "
                            "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
            if row and row["message"] == message:
                c.execute("UPDATE service_errors SET count=count+1, last_at=? WHERE id=?", (now_iso(), row["id"]))
                return
            if row:
                c.execute("UPDATE service_errors SET resolved_at=? WHERE id=?", (now_iso(), row["id"]))
            c.execute("INSERT INTO service_errors(source, message, first_at, last_at) VALUES(?, ?, ?, ?)",
                      (source, message, now_iso(), now_iso()))
            c.execute("DELETE FROM service_errors WHERE id NOT IN "
                      "(SELECT id FROM service_errors ORDER BY id DESC LIMIT ?)", (KEEP_SERVICE_ERRORS,))

    def resolve_service_errors(self, source: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE service_errors SET resolved_at=? WHERE source=? AND resolved_at IS NULL",
                      (now_iso(), source))

    def service_errors(self, limit: int = 50) -> List[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM service_errors ORDER BY id DESC LIMIT ?", (limit,))]

    def queue_stats(self) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT MIN(received_at) oldest FROM emails WHERE status IN ('new', 'classifying')").fetchone()
        return {"oldest_waiting": row["oldest"]}

    # --- запуски скиллов --------------------------------------------------
    def add_run(self, eid: int, title: str, prompt: str) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO runs(email_id, title, prompt, created_at) VALUES(?, ?, ?, ?)",
                            (eid, title, prompt, now_iso()))
            self._log(c, eid, "run_started", title)
            return cur.lastrowid

    def update_run(self, rid: int, **fields) -> None:
        cols = ", ".join("{}=?".format(k) for k in fields)
        with self.conn() as c:
            c.execute("UPDATE runs SET {} WHERE id=?".format(cols), list(fields.values()) + [rid])

    def finish_run(self, rid: int, eid: int, status: str, result: str = "", error: Optional[str] = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                      (status, result, error, now_iso(), rid))
            self._log(c, eid, "run_" + status, error)

    def log(self, eid: int, event: str, detail: Optional[str]) -> None:
        with self.conn() as c:
            self._log(c, eid, event, detail)

    @staticmethod
    def _log(c: sqlite3.Connection, eid: int, event: str, detail: Optional[str]) -> None:
        c.execute("INSERT INTO status_log(email_id, ts, event, detail) VALUES(?, ?, ?, ?)",
                  (eid, now_iso(), event, detail))


DRAFT_FIELDS = {"to": "to_list", "cc": "cc_list", "bcc": "bcc_list", "subject": "subject",
                "body_html": "body_html", "attachments": "attachments"}


def _draft(row: dict) -> dict:
    for key, column in DRAFT_FIELDS.items():
        if column in row:
            value = row.pop(column)
            row[key] = json.loads(value) if column.endswith("_list") or column == "attachments" else value
    return row


def _folder_clause(folder: str, inbox: Optional[str]) -> str:
    """Письма, полученные до появления папок, лежат во входящих без пометки папки."""
    return "(folder = ? OR folder IS NULL)" if folder and folder == inbox else "folder = ?"


def _dump(value) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _load(row: dict) -> dict:
    for key in EMAIL_JSON_FIELDS:
        if key in row:
            row[key] = json.loads(row[key]) if row[key] else None
    return row
