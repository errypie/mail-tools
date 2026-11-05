"""SQLite: письма, запуски скиллов, категории, настройки и журнал обработки."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional

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
"""

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
}

CATEGORY_ENV_FIELDS = ("cwd", "files", "include_dirs", "approval_mode", "permission", "model")
CATEGORY_TEXT_FIELDS = ("title", "prompt", "action_title", "action_template") + CATEGORY_ENV_FIELDS
MAX_ATTEMPTS = 5
KEEP_SERVICE_ERRORS = 200


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            known = {r["name"] for r in c.execute("PRAGMA table_info(categories)")}
            for column in CATEGORY_ENV_FIELDS:  # базы, созданные до появления окружения категории
                if column not in known:
                    c.execute("ALTER TABLE categories ADD COLUMN {} TEXT NOT NULL DEFAULT ''".format(column))
            if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
                self._write_categories(c, DEFAULT_CATEGORIES)
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
    def _write_categories(c: sqlite3.Connection, items: List[dict]) -> None:
        for i, it in enumerate(items):
            c.execute("INSERT INTO categories(name, title, prompt, action_title, action_template, position) "
                      "VALUES(?, ?, ?, ?, ?, ?)",
                      (it["name"], it.get("title") or it["name"], it.get("prompt", ""),
                       it.get("action_title", ""), it.get("action_template", ""), i))

    # --- письма -----------------------------------------------------------
    def add_email(self, m: dict) -> Optional[int]:
        """Возвращает id нового письма или None, если такое уже есть."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO emails(message_id, subject, sender, sender_name, recipients, received_at, "
                "body, has_attachments, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m["message_id"], m.get("subject", ""), m.get("sender", ""), m.get("sender_name", ""),
                 m.get("recipients", ""), m["received_at"], m.get("body", ""),
                 int(bool(m.get("has_attachments"))), now_iso()))
            if cur.rowcount == 0:
                return None
            self._log(c, cur.lastrowid, "received", m.get("sender", ""))
            return cur.lastrowid

    def list_emails(self, category: Optional[str] = None, status: Optional[str] = None, q: str = "",
                    limit: int = 100, offset: int = 0) -> dict:
        where, args = [], []
        if category:
            where.append("category = ?")
            args.append(category)
        if status:
            where.append("status = ?")
            args.append(status)
        if q:
            where.append("(subject LIKE ? OR sender LIKE ? OR summary LIKE ? OR jira_key LIKE ?)")
            args += ["%{}%".format(q)] * 4
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM emails" + clause, args).fetchone()["n"]
            rows = c.execute(
                "SELECT id, subject, sender, sender_name, received_at, has_attachments, status, category, confidence, "
                "summary, jira_key, recommend_action, error, attempts, "
                "(SELECT status FROM runs r WHERE r.email_id = emails.id ORDER BY r.id DESC LIMIT 1) AS run_status "
                "FROM emails" + clause + " ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        return {"emails": [dict(r) for r in rows], "total": total}

    def counts(self) -> dict:
        with self.conn() as c:
            by_cat = {r["category"] or "": r["n"] for r in c.execute(
                "SELECT category, COUNT(*) n FROM emails WHERE status='classified' GROUP BY category")}
            by_status = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
        return {"categories": by_cat, "statuses": by_status}

    def get_email(self, eid: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            if not row:
                return None
            email = dict(row)
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
            return dict(row)

    def save_classification(self, eid: int, res: dict, session_id: Optional[str]) -> None:
        with self.conn() as c:
            # Категорию, выбранную вручную, модель не меняет. Саммари и сущности обновляются.
            c.execute("UPDATE emails SET status='classified', category=CASE WHEN category_manual=1 THEN category ELSE ? END, "
                      "confidence=?, summary=?, reason=?, jira_key=?, "
                      "recommend_action=?, action_hint=?, entities=?, session_id=?, classified_at=?, error=NULL, next_retry_at=NULL "
                      "WHERE id=?",
                      (res["category"], res["confidence"], res["summary"], res["reason"], res["jira_key"],
                       int(res["recommend_action"]), res["action_hint"], json.dumps(res.get("entities") or {}, ensure_ascii=False),
                       session_id, now_iso(), eid))
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

    @staticmethod
    def _log(c: sqlite3.Connection, eid: int, event: str, detail: Optional[str]) -> None:
        c.execute("INSERT INTO status_log(email_id, ts, event, detail) VALUES(?, ?, ?, ?)",
                  (eid, now_iso(), event, detail))
