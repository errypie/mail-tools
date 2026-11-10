"""Веб-приложение: вход по паролю, письма с классификацией, настройки и запуск скиллов."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from datetime import date, datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import calendar_ops, compose, office
from .html_view import clean_fragment
from .classifier import BUILTIN_VARIABLES, ClassifyError, build_prompt, render_action, substitute, variables
from .db import CATEGORY_ENV_FIELDS, DEFAULT_CLASSIFY_PROMPT, Database
from .meetings import DEFAULT_INVITE_PROMPT
from .health import hint_for
from .mail_source import MailError
from .meetings import ACTIONS, HHMM_RE, INVITE_MODES, MAX_REPLY_CHARS, describe, local_tz
from .vault import Vault, VaultError
from .worker import Worker

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
# второй файл интерфейса: подписи, флажки, автоответ, правила, контакты, задачи, уведомления.
# CSP разрешает только встроенные скрипты, поэтому вставляем его в страницу при отдаче
STATIC_EXTRA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "office.js")
COOKIE = "mailbot_session"
SESSION_TTL = 12 * 3600
MAX_FAILURES, LOCK_SECONDS = 5, 60
MAX_BODY = 2 * 1024 * 1024
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data: https: http:; font-src data:; frame-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
# что можно открыть во вкладке браузера, остальное только скачивается: HTML или SVG из вложения выполнили бы скрипты
PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "application/pdf", "text/plain"}
SETTING_KEYS = {"ews_server": str, "ews_email": str, "ews_auth": str, "ca_bundle": str, "allowed_senders": list,
                "blocked_senders": list,
                "polling_enabled": bool, "poll_seconds": int, "initial_days": int, "gigacode_url": str,
                "skill_cwd": str, "skill_permission": str, "max_body_chars": int, "classify_prompt": str,
                "entities": list, "calendar_auto": bool, "work_start": str, "work_end": str,
                "free_slots_count": int, "invite_prompt": str}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
MAX_CALENDAR_DAYS = 62
SECRET_KEYS = ("ews_username", "ews_password", "gigacode_token")
CATEGORY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ENTITY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Auth:
    def __init__(self):
        self.tokens = {}
        self.failures = 0
        self.locked_until = 0.0
        self.lock = threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = time.time() + SESSION_TTL
            self.failures = 0
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self.lock:
            for known, expires in list(self.tokens.items()):
                if expires < time.time():
                    del self.tokens[known]
                elif hmac.compare_digest(known, token):
                    return True
        return False

    def revoke(self, token: Optional[str]) -> None:
        with self.lock:
            self.tokens.pop(token, None)

    def check_throttle(self) -> None:
        left = self.locked_until - time.time()
        if left > 0:
            raise HttpError(429, "Слишком много попыток. Подождите {} с".format(int(left) + 1))

    def failed(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                self.locked_until = time.time() + LOCK_SECONDS
                self.failures = 0


def clean_entities(items: list) -> list:
    out, seen = [], {name for name, _ in BUILTIN_VARIABLES}
    for item in items:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        if not ENTITY_NAME_RE.match(name):
            raise HttpError(400, "Код сущности: латиница, цифры и _, начинается с буквы, до 32 символов")
        if name in seen:
            raise HttpError(400, "Переменная {{{}}} уже есть".format(name))
        seen.add(name)
        pattern = str(item.get("pattern") or "").strip()
        try:
            re.compile(pattern)
        except re.error as e:
            raise HttpError(400, "Регулярное выражение {{{}}}: {}".format(name, e))
        out.append({"name": name, "title": str(item.get("title") or name).strip(),
                    "prompt": str(item.get("prompt") or "").strip(), "pattern": pattern})
    return out


class Context:
    def __init__(self, db: Database, vault: Vault, worker: Worker):
        self.db = db
        self.vault = vault
        self.worker = worker
        self.auth = Auth()


ROUTES = []


def route(method: str, pattern: str, public: bool = False):
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn, public))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "mailbot/1.0"
    protocol_version = "HTTP/1.1"
    ctx: Context  # выставляется при запуске

    def log_message(self, fmt, *args):
        print("[{}] {}".format(time.strftime("%H:%M:%S"), fmt % args), flush=True)

    # --- утилиты ----------------------------------------------------------
    def _json(self, status: int, obj, cookie: Optional[str] = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "Слишком большой запрос")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "Некорректный JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "Ожидался JSON-объект")
        return data

    def _token(self) -> Optional[str]:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return jar[COOKIE].value if COOKIE in jar else None

    @staticmethod
    def _session_cookie(token: str, max_age: int) -> str:
        return "{}={}; Path=/; HttpOnly; SameSite=Strict; Max-Age={}".format(COOKIE, token, max_age)

    def _variable_titles(self) -> list:
        return list(BUILTIN_VARIABLES) + [(e["name"], e["title"]) for e in self.ctx.db.settings()["entities"]]

    def _email_or_404(self, eid: str) -> dict:
        email = self.ctx.db.get_email(int(eid)) if eid.isdigit() else None
        if not email:
            raise HttpError(404, "Письмо не найдено")
        return email

    # --- диспетчер --------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        parts = urlsplit(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        for m, rx, fn, public in ROUTES:
            match = rx.match(parts.path)
            if m == method and match:
                break
        else:
            return self._json(404, {"error": "Not found"})
        try:
            # свой заголовок нельзя выставить с чужого сайта без CORS — это защита от CSRF
            if method != "GET" and self.headers.get("X-Mailbot") != "1":
                raise HttpError(403, "Нет заголовка X-Mailbot")
            if not public and not self.ctx.auth.valid(self._token()):
                raise HttpError(401, "Нужно войти")
            fn(self, **match.groupdict())
        except HttpError as e:
            self._json(e.status, {"error": str(e)})
        except VaultError as e:
            self._json(400, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # === вход ============================================================
    @route("GET", "/", public=True)
    def index(self):
        with open(STATIC, "rb") as f:
            body = f.read()
        with open(STATIC_EXTRA, "rb") as f:
            extra = f.read().replace(b"</script", b"<\\/script")
        body = body.replace(b"</body>", b"<script>\n" + extra + b"\n</script>\n</body>", 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    @route("GET", "/health", public=True)
    def health(self):
        self._json(200, {"status": "ok", "unlocked": self.ctx.vault.unlocked})

    @route("GET", "/api/state", public=True)
    def state(self):
        self._json(200, {"setup_done": self.ctx.vault.exists, "authenticated": self.ctx.auth.valid(self._token()),
                         "unlocked": self.ctx.vault.unlocked})

    @route("POST", "/api/setup", public=True)
    def create_password(self):
        if self.ctx.vault.exists:
            raise HttpError(409, "Пароль уже задан")
        self.ctx.vault.create(str(self._body().get("password") or ""))
        self._json(200, {"ok": True}, self._session_cookie(self.ctx.auth.issue(), SESSION_TTL))

    @route("POST", "/api/login", public=True)
    def login(self):
        auth = self.ctx.auth
        auth.check_throttle()
        if not self.ctx.vault.exists:
            raise HttpError(409, "Сначала задайте пароль")
        if not self.ctx.vault.unlock(str(self._body().get("password") or "")):
            auth.failed()
            raise HttpError(401, "Неверный пароль")
        self.ctx.worker.wake.set()
        self._json(200, {"ok": True}, self._session_cookie(auth.issue(), SESSION_TTL))

    @route("POST", "/api/logout")
    def logout(self):
        self.ctx.auth.revoke(self._token())
        self._json(200, {"ok": True}, self._session_cookie("", 0))

    @route("POST", "/api/password")
    def change_password(self):
        body = self._body()
        if not self.ctx.vault.change_password(str(body.get("old") or ""), str(body.get("new") or "")):
            raise HttpError(400, "Текущий пароль неверен")
        self._json(200, {"ok": True})

    # === письма ==========================================================
    @route("GET", "/api/emails")
    def emails(self):
        q = self.query
        try:
            limit = max(1, min(int(q.get("limit", 100)), 500))
            offset = max(0, int(q.get("offset", 0)))
        except ValueError:
            raise HttpError(400, "limit и offset — целые числа")
        worker = self.ctx.worker
        inbox = worker.inbox_id or "inbox"
        folder = q.get("folder")
        if folder == "inbox":
            folder = inbox
        data = self.ctx.db.list_emails(q.get("category"), q.get("status"), q.get("q", ""), limit, offset,
                                       folder=folder, inbox=inbox, hidden=worker.hidden_folders(),
                                       unread=q.get("unread") == "1", flagged=q.get("flagged") == "1")
        data["counts"] = self.ctx.db.counts(worker.hidden_folders())
        data["counts"]["flagged"] = self.ctx.db.flagged_count(worker.hidden_folders())
        data["counts"]["inbox_unread"] = self.ctx.db.unread_count(inbox, inbox)
        data["worker"] = self.ctx.worker.status
        data["categories"] = [{"name": c["name"], "title": c["title"]} for c in self.ctx.db.categories()]
        self._json(200, data)

    @route("GET", "/api/emails/{eid}")
    def email(self, eid):
        email = self._email_or_404(eid)
        cat = next((c for c in self.ctx.db.categories() if c["name"] == email["category"]), None)
        entities = self.ctx.db.settings()["entities"]
        values = variables(email, cat["files"] if cat else None, entities)
        email["variables"] = [{"name": n, "title": t, "value": values[n]} for n, t in self._variable_titles()]
        email["action"] = None
        if cat and cat["action_template"]:
            email["action"] = {"title": cat["action_title"] or "Запустить",
                               "prompt": render_action(cat["action_template"], email, cat["files"], entities)}
        email["environment"] = {k: cat[k] for k in CATEGORY_ENV_FIELDS if cat[k]} if cat else {}
        email["gigacode_url"] = self.ctx.db.settings()["gigacode_url"]
        email["categories"] = [{"name": c["name"], "title": c["title"]} for c in self.ctx.db.categories()]
        if email.get("invite"):
            email["invite_view"] = describe(email["invite"])
            email["invite_mode"] = (cat or {}).get("invite_mode", "")
        self._json(200, email)

    # === ящик ============================================================
    def _box(self, action):
        try:
            return action()
        except ValueError as e:
            raise HttpError(400, str(e))
        except MailError as e:
            raise HttpError(409, str(e))
        except Exception as e:  # noqa: BLE001 — сбой Exchange показываем у кнопки
            raise HttpError(502, "Exchange: {}: {}".format(type(e).__name__, e))

    @route("GET", "/api/folders")
    def folders(self):
        w = self.ctx.worker
        folders = self._box(lambda: w.folders(self.query.get("fresh") == "1"))
        self._json(200, {"folders": folders, "inbox": w.inbox_id, "trash": self.ctx.db.state("trash_folder")})

    @route("POST", "/api/folders/load")
    def load_folder(self):
        body = self._body()
        folder = str(body.get("folder") or "")
        if folder == "inbox":
            folder = self.ctx.worker.inbox_id or ""
        if not folder:
            raise HttpError(400, "Не указана папка")
        self._json(200, self._box(lambda: self.ctx.worker.load_folder(folder, bool(body.get("older")))))

    @route("GET", "/api/search")
    def search(self):
        query = self.query.get("q", "").strip()[:200]
        if len(query) < 2:
            raise HttpError(400, "Введите хотя бы два символа")
        ids = self._box(lambda: self.ctx.worker.search(query))
        data = self.ctx.db.list_emails(ids=ids, limit=200) if ids else {"emails": [], "total": 0}
        self._json(200, data)

    @route("GET", "/api/emails/{eid}/thread")
    def email_thread(self, eid):
        email = self._email_or_404(eid)
        ids = self._box(lambda: self.ctx.worker.thread(email["id"]))
        rows = self.ctx.db.list_emails(ids=ids, limit=200, oldest_first=True)["emails"]
        self._json(200, {"emails": rows})

    @route("POST", "/api/emails/{eid}/read")
    def email_read(self, eid):
        email = self._email_or_404(eid)
        read = bool(self._body().get("read", True))
        self._box(lambda: self.ctx.worker.set_read(email["id"], read))
        self._json(200, {"ok": True, "is_read": read})

    @route("POST", "/api/emails/{eid}/flag")
    def email_flag(self, eid):
        email = self._email_or_404(eid)
        flag = self._body().get("flag")
        if flag not in (office.FLAG_NONE, office.FLAG_DONE, office.FLAG_ON):
            raise HttpError(400, "flag: 0 — снять, 1 — выполнено, 2 — поставить")
        self._json(200, self._box(lambda: self.ctx.worker.set_flag(email["id"], flag)))

    @route("POST", "/api/emails/{eid}/task")
    def email_task(self, eid):
        email = self._email_or_404(eid)
        self._json(200, {"id": self._box(lambda: self.ctx.worker.task_from_email(email["id"]))})

    @route("GET", "/api/notify")
    def notify(self):
        try:
            after = int(self.query.get("after", 0))
        except ValueError:
            raise HttpError(400, "after — число")
        self._json(200, self.ctx.worker.new_mail(after))

    # === подписи =========================================================
    @route("GET", "/api/signatures")
    def signatures(self):
        self._json(200, {"signatures": self.ctx.db.signatures()})

    @route("POST", "/api/signatures")
    def save_signature(self):
        body = self._body()
        name = str(body.get("name") or "").strip()[:100]
        if not name:
            raise HttpError(400, "Нужно название подписи")
        html_body = clean_fragment(str(body.get("body_html") or "")[:50000])
        try:
            sid = int(body["id"]) if body.get("id") else None
            sid = self.ctx.db.save_signature(sid, name, html_body, bool(body.get("for_new")), bool(body.get("for_reply")))
        except ValueError as e:
            raise HttpError(400, str(e))
        self._json(200, {"id": sid, "signatures": self.ctx.db.signatures()})

    @route("DELETE", "/api/signatures/{sid}")
    def delete_signature(self, sid):
        try:
            self.ctx.db.delete_signature(int(sid))
        except ValueError:
            raise HttpError(404, "Нет такой подписи")
        self._json(200, {"signatures": self.ctx.db.signatures()})

    # === автоответ, правила, контакты, задачи =============================
    def _office(self, fn, what: str):
        try:
            return fn(self.ctx.worker.calendar())
        except (ValueError, office.OfficeError, MailError) as e:
            raise HttpError(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HttpError(502, "{}: {}: {}".format(what, type(e).__name__, e))

    @route("GET", "/api/oof")
    def get_oof(self):
        self._json(200, self._office(lambda s: s.get_oof(), "Автоответ недоступен"))

    @route("PUT", "/api/oof")
    def set_oof(self):
        oof = self._office(lambda s: office.clean_oof(self._body()), "Автоответ")
        self._office(lambda s: s.set_oof(oof), "Не удалось сохранить автоответ")
        self._json(200, self._office(lambda s: s.get_oof(), "Автоответ недоступен"))

    @route("GET", "/api/rules")
    def rules(self):
        self._json(200, {"rules": self._office(lambda s: s.rules(), "Правила недоступны")})

    @route("POST", "/api/rules")
    def create_rule(self):
        rule = self._office(lambda s: office.clean_rule(self._body()), "Правило")
        self._json(200, {"id": self._office(lambda s: s.create_rule(rule), "Не удалось создать правило")})

    @route("POST", "/api/rules/update")
    def update_rule(self):
        body = self._body()
        rid = str(body.get("id") or "")
        if not rid:
            raise HttpError(400, "Нужен id правила")
        if set(body) <= {"id", "enabled"}:
            rule = {"only_enabled": bool(body.get("enabled"))}
        else:
            rule = self._office(lambda s: office.clean_rule(body), "Правило")
        self._office(lambda s: s.update_rule(rid, rule), "Не удалось изменить правило")
        self._json(200, {"ok": True})

    @route("POST", "/api/rules/delete")
    def delete_rule(self):
        rid = str(self._body().get("id") or "")
        if not rid:
            raise HttpError(400, "Нужен id правила")
        self._office(lambda s: s.delete_rule(rid), "Не удалось удалить правило")
        self._json(200, {"ok": True})

    @route("GET", "/api/people")
    def people(self):
        if self.query.get("id"):
            self._json(200, self._office(lambda s: s.contact_get(self.query["id"]), "Контакт недоступен"))
            return
        self._json(200, {"contacts": self._office(lambda s: s.contacts_list(), "Контакты недоступны")})

    @route("POST", "/api/people")
    def save_person(self):
        body = self._body()
        data = self._office(lambda s: office.clean_contact(body), "Контакт")
        cid = self._office(lambda s: s.contact_save(str(body["id"]) if body.get("id") else None, data),
                           "Не удалось сохранить контакт")
        self.ctx.worker.forget_contacts()
        self._json(200, {"id": cid})

    @route("POST", "/api/people/delete")
    def delete_person(self):
        cid = str(self._body().get("id") or "")
        if not cid:
            raise HttpError(400, "Нужен id контакта")
        self._office(lambda s: s.contact_delete(cid), "Не удалось удалить контакт")
        self.ctx.worker.forget_contacts()
        self._json(200, {"ok": True})

    @route("GET", "/api/tasks")
    def tasks(self):
        if self.query.get("id"):
            self._json(200, self._office(lambda s: s.task_get(self.query["id"]), "Задача недоступна"))
            return
        self._json(200, {"tasks": self._office(lambda s: s.tasks_list(), "Задачи недоступны")})

    @route("POST", "/api/tasks")
    def save_task(self):
        body = self._body()
        tid = str(body["id"]) if body.get("id") else None
        data = self._office(lambda s: office.clean_task(body, partial=bool(tid)), "Задача")
        self._json(200, {"id": self._office(lambda s: s.task_save(tid, data), "Не удалось сохранить задачу")})

    @route("POST", "/api/tasks/delete")
    def delete_task(self):
        tid = str(self._body().get("id") or "")
        if not tid:
            raise HttpError(400, "Нужен id задачи")
        self._office(lambda s: s.task_delete(tid), "Не удалось удалить задачу")
        self._json(200, {"ok": True})

    @route("POST", "/api/emails/{eid}/move")
    def email_move(self, eid):
        email = self._email_or_404(eid)
        folder = str(self._body().get("folder") or "")
        if not folder:
            raise HttpError(400, "Не указана папка")
        self._box(lambda: self.ctx.worker.move(email["id"], folder))
        self._json(200, {"ok": True})

    @route("POST", "/api/emails/{eid}/delete")
    def email_delete(self, eid):
        email = self._email_or_404(eid)
        permanent = bool(self._body().get("permanent"))
        self._json(200, {"result": self._box(lambda: self.ctx.worker.delete(email["id"], permanent))})

    @route("GET", "/api/emails/{eid}/html")
    def email_html(self, eid):
        email = self._email_or_404(eid)
        doc, blocked = self.ctx.worker.email_document(email, self.query.get("remote") == "1")
        self._json(200, {"html": doc, "blocked": blocked})

    @route("GET", "/api/emails/{eid}/attachments/{idx}")
    def email_attachment(self, eid, idx):
        email = self._email_or_404(eid)
        try:
            meta, data = self.ctx.worker.attachment(email, int(idx))
        except ValueError as e:
            raise HttpError(404, str(e))
        except MailError as e:
            raise HttpError(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HttpError(502, "Не удалось получить вложение: {}: {}".format(type(e).__name__, e))
        self._file(meta, data)

    def _file(self, meta: dict, data: bytes):
        """Вложение: в браузере открываются только безопасные типы, остальное скачивается."""
        ctype = (meta.get("content_type") or "").split(";")[0].strip().lower()
        inline = self.query.get("inline") == "1" and ctype in PREVIEW_TYPES
        self.send_response(200)
        self.send_header("Content-Type", (ctype + "; charset=utf-8" if ctype == "text/plain" else ctype)
                         if inline else "application/octet-stream")
        self.send_header("Content-Disposition", "{}; filename*=UTF-8''{}".format(
            "inline" if inline else "attachment", quote(meta.get("name") or "file", safe="")))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=600")
        if ctype != "application/pdf":  # встроенный просмотрщик PDF не работает в песочнице
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox")
        self.end_headers()
        self.wfile.write(data)

    @route("POST", "/api/emails/{eid}/invite")
    def answer_invite(self, eid):
        email = self._email_or_404(eid)
        body = self._body()
        try:
            propose = int(body["propose"]) if body.get("propose") not in (None, "") else None
        except (TypeError, ValueError):
            raise HttpError(400, "Номер окна — целое число")
        try:
            response = self.ctx.worker.respond_invite(email["id"], str(body.get("action") or ""),
                                                      str(body.get("reply") or "")[:MAX_REPLY_CHARS], propose)
        except ValueError as e:
            raise HttpError(400, str(e))
        except (RuntimeError, MailError) as e:
            raise HttpError(502, str(e))
        self._json(200, {"response": response})

    # === написание писем =================================================
    def _draft_id(self, did: str) -> int:
        draft = self.ctx.db.get_draft(int(did)) if did.isdigit() else None
        if not draft:
            raise HttpError(404, "Черновик не найден")
        return draft["id"]

    @route("GET", "/api/drafts")
    def drafts(self):
        self._json(200, {"drafts": self.ctx.db.list_drafts(self.query.get("sent") == "1")})

    @route("POST", "/api/drafts")
    def create_draft(self):
        body = self._body()
        ref = body.get("ref")
        try:
            draft = self.ctx.worker.new_draft(str(body.get("kind") or "new"), int(ref) if ref else None)
        except (TypeError, ValueError) as e:
            raise HttpError(400, str(e))
        self._json(200, {"draft": draft})

    @route("GET", "/api/drafts/{did}")
    def get_draft(self, did):
        self._json(200, {"draft": self.ctx.db.get_draft(self._draft_id(did))})

    @route("PUT", "/api/drafts/{did}")
    def update_draft(self, did):
        try:
            draft = self.ctx.worker.save_draft(self._draft_id(did), self._body())
        except ValueError as e:
            raise HttpError(409, str(e))
        self._json(200, {"draft": draft})

    @route("DELETE", "/api/drafts/{did}")
    def delete_draft(self, did):
        try:
            self.ctx.worker.delete_draft(self._draft_id(did))
        except ValueError as e:
            raise HttpError(409, str(e))
        self._json(200, {"ok": True})

    @route("POST", "/api/drafts/{did}/attachments")
    def upload_attachment(self, did):
        """Тело запроса — сам файл, имя в заголовке X-Filename (URL-кодированное)."""
        did = self._draft_id(did)
        length = int(self.headers.get("Content-Length") or 0)
        if length > compose.MAX_UPLOAD:
            self.close_connection = True  # тело не читаем, соединение дальше использовать нельзя
            raise HttpError(413, "Файл больше {} МБ".format(compose.MAX_UPLOAD >> 20))
        data = self.rfile.read(length) if length else b""
        name = unquote(self.headers.get("X-Filename") or "файл")
        try:
            draft = self.ctx.worker.add_upload(did, name, self.headers.get("Content-Type") or "", data)
        except ValueError as e:
            raise HttpError(413, str(e))
        self._json(200, {"draft": draft})

    @route("GET", "/api/drafts/{did}/attachments/{idx}")
    def draft_attachment(self, did, idx):
        try:
            meta, data = self.ctx.worker.upload_data(self._draft_id(did), int(idx))
        except (ValueError, OSError) as e:
            raise HttpError(404, str(e))
        self._file(meta, data)

    @route("DELETE", "/api/drafts/{did}/attachments/{idx}")
    def remove_attachment(self, did, idx):
        try:
            draft = self.ctx.worker.remove_attachment(self._draft_id(did), int(idx))
        except ValueError as e:
            raise HttpError(400, str(e))
        self._json(200, {"draft": draft})

    @route("POST", "/api/drafts/{did}/send")
    def send_draft(self, did):
        did = self._draft_id(did)
        body = self._body()
        try:
            if body:
                self.ctx.worker.save_draft(did, body)  # последние правки, которые автосохранение не успело
            draft = self.ctx.worker.send_draft(did)
        except ValueError as e:
            raise HttpError(400, str(e))
        except (RuntimeError, MailError) as e:
            raise HttpError(502, str(e))
        self._json(200, {"draft": draft})

    @route("POST", "/api/drafts/{did}/ai")
    def ai_draft(self, did):
        body = self._body()
        try:
            out = self.ctx.worker.ai_draft(self._draft_id(did), str(body.get("instruction") or "")[:2000],
                                           str(body.get("current") or "")[:compose.MAX_AI_CHARS])
        except ValueError as e:
            raise HttpError(400, str(e))
        except (RuntimeError, OSError) as e:
            raise HttpError(502, "GigaCode не ответил: {}".format(e))
        self._json(200, out)

    @route("GET", "/api/contacts")
    def contacts(self):
        self._json(200, {"contacts": self.ctx.worker.contacts(self.query.get("q", "")[:100])})

    # === календарь =======================================================
    @route("GET", "/api/calendar")
    def calendar(self):
        try:
            first = date.fromisoformat(self.query.get("start") or date.today().isoformat())
            days = max(1, min(int(self.query.get("days", 7)), MAX_CALENDAR_DAYS))
        except ValueError:
            raise HttpError(400, "start — дата ГГГГ-ММ-ДД, days — число дней")
        begin = datetime.combine(first, dtime(0), local_tz())
        try:
            events = self.ctx.worker.events(begin, begin + timedelta(days=days))
        except MailError as e:
            raise HttpError(400, str(e))
        except Exception as e:  # noqa: BLE001 — показываем причину в интерфейсе
            raise HttpError(502, "Календарь недоступен: {}: {}".format(type(e).__name__, e))
        s = self.ctx.db.settings()
        self._json(200, {"events": events, "start": first.isoformat(), "days": days,
                         "work_start": s["work_start"], "work_end": s["work_end"]})

    def _cal(self, fn, what: str):
        try:
            return fn()
        except (MailError, ValueError) as e:
            raise HttpError(400, str(e))
        except Exception as e:  # noqa: BLE001 — показываем причину в интерфейсе
            raise HttpError(502, "{}: {}: {}".format(what, type(e).__name__, e))

    @route("POST", "/api/calendar/events")
    def create_event(self):
        body = self._body()
        if "attendees" in body and "required" not in body:
            body["required"] = body["attendees"]
        spec = self._cal(lambda: calendar_ops.clean_spec(body), "Встреча")
        eid = self._cal(lambda: self.ctx.worker.create_event(spec), "Не удалось создать встречу")
        self._json(200, {"id": eid})

    @staticmethod
    def _event_ref(body: dict) -> tuple:
        if not body.get("id"):
            raise HttpError(400, "Нужен id встречи")
        scope = str(body.get("scope") or "occurrence")
        if scope not in calendar_ops.SCOPES:
            raise HttpError(400, "scope: occurrence или series")
        return str(body["id"]), scope

    @route("GET", "/api/calendar/event")
    def get_event(self):
        if not self.query.get("id"):
            raise HttpError(400, "Нужен id встречи")
        self._json(200, self._cal(lambda: self.ctx.worker.event(self.query["id"]), "Встреча недоступна"))

    @route("POST", "/api/calendar/event/update")
    def update_event(self):
        body = self._body()
        eid, scope = self._event_ref(body)
        fields = {k: v for k, v in body.items() if k not in ("id", "scope")}
        spec = self._cal(lambda: calendar_ops.clean_spec(fields, partial=True), "Встреча")
        if not spec:
            raise HttpError(400, "Нечего менять")
        new_id = self._cal(lambda: self.ctx.worker.update_event(eid, spec, scope), "Не удалось изменить встречу")
        self._json(200, {"id": new_id})

    @route("POST", "/api/calendar/event/cancel")
    def cancel_event(self):
        body = self._body()
        eid, scope = self._event_ref(body)
        message = str(body.get("message") or "")[:MAX_REPLY_CHARS]
        result = self._cal(lambda: self.ctx.worker.cancel_event(eid, scope, message), "Не удалось отменить встречу")
        self._json(200, {"result": result})

    @route("GET", "/api/calendar/freebusy")
    def free_busy(self):
        try:
            emails = [e.strip() for e in self.query.get("emails", "").split(",") if e.strip()]
            first = _local_dt(self.query.get("start") or date.today().isoformat())
            days = max(1, min(int(self.query.get("days", 1)), calendar_ops.MAX_FREEBUSY_DAYS))
            duration = max(5, min(int(self.query.get("duration", 30)), 24 * 60))
        except ValueError:
            raise HttpError(400, "start — дата, days и duration — числа")
        bad = [e for e in emails if not EMAIL_RE.match(e)]
        if bad or len(emails) > calendar_ops.MAX_FREEBUSY_PEOPLE:
            raise HttpError(400, "Неверные адреса или их слишком много")
        self._json(200, self._cal(lambda: self.ctx.worker.free_busy(emails, first, days, duration),
                                  "Занятость недоступна"))

    @route("GET", "/api/calendar/rooms")
    def rooms(self):
        self._json(200, {"lists": self._cal(self.ctx.worker.rooms, "Список переговорных недоступен")})

    @route("POST", "/api/calendar/respond")
    def calendar_respond(self):
        body = self._body()
        action = str(body.get("action") or "")
        if action not in ACTIONS or not body.get("id"):
            raise HttpError(400, "Нужны id события и ответ: accept, tentative или decline")
        try:
            self.ctx.worker.calendar().respond([str(body["id"])], action, str(body.get("reply") or "")[:MAX_REPLY_CHARS])
        except MailError as e:
            raise HttpError(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HttpError(502, "Не удалось ответить: {}: {}".format(type(e).__name__, e))
        self._json(200, {"ok": True})

    @route("POST", "/api/emails/{eid}/category")
    def set_category(self, eid):
        email = self._email_or_404(eid)
        name = str(self._body().get("category") or "")
        if not self.ctx.db.category(name):
            raise HttpError(400, "Нет такой категории")
        self.ctx.db.set_category(email["id"], name)
        self._json(200, {"ok": True})

    @route("POST", "/api/emails/{eid}/reclassify")
    def reclassify(self, eid):
        self.ctx.db.reset_for_classification(self._email_or_404(eid)["id"])
        self._json(200, {"ok": True})

    @route("POST", "/api/emails/{eid}/run")
    def run(self, eid):
        email = self._email_or_404(eid)
        body = self._body()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HttpError(400, "Пустой запрос к агенту")
        category = self.ctx.db.category(email["category"])
        prompt = substitute(prompt, variables(email, category["files"] if category else None,
                                              self.ctx.db.settings()["entities"]))
        rid = self.ctx.worker.start_run(email["id"], str(body.get("title") or "Запуск")[:200], prompt, category)
        self._json(202, {"runId": rid})

    @route("POST", "/api/emails/reclassify-all")
    def reclassify_all(self):
        self._json(200, {"count": self.ctx.db.reclassify_all()})

    @route("POST", "/api/emails/{eid}/block-sender")
    def block_sender(self, eid):
        sender = self._email_or_404(eid)["sender"].strip().lower()
        if "@" not in sender:
            raise HttpError(400, "У письма нет адреса отправителя")
        rule = "@" + sender.split("@", 1)[1] if self._body().get("scope") == "domain" else sender
        blocked = self.ctx.db.settings()["blocked_senders"]
        if rule not in [b.strip().lower() for b in blocked]:
            self.ctx.db.set_settings({"blocked_senders": blocked + [rule]})
        self._json(200, {"blocked": rule})

    # === статус ==========================================================
    @route("GET", "/api/status")
    def status(self):
        self._json(200, self.ctx.worker.health())

    @route("POST", "/api/status/agent-check")
    def agent_check(self):
        self._json(200, self.ctx.worker.check_agent())

    # === настройки =======================================================
    @route("GET", "/api/settings")
    def get_settings(self):
        s = {k: v for k, v in self.ctx.db.settings().items() if not k.startswith("_")}
        s["ews_username"] = self.ctx.vault.get("ews_username", "")
        s["has_ews_password"] = bool(self.ctx.vault.get("ews_password"))
        s["has_gigacode_token"] = bool(self.ctx.vault.get("gigacode_token"))
        s["default_classify_prompt"] = DEFAULT_CLASSIFY_PROMPT
        s["default_invite_prompt"] = DEFAULT_INVITE_PROMPT
        self._json(200, {"settings": s, "builtin_variables": BUILTIN_VARIABLES})

    @route("PUT", "/api/settings")
    def put_settings(self):
        body = self._body()
        values = {}
        for key, typ in SETTING_KEYS.items():
            if key in body:
                try:
                    values[key] = body[key] if typ in (list, bool) else typ(body[key])
                except (TypeError, ValueError):
                    raise HttpError(400, "Неверное значение {}".format(key))
                if typ in (list, bool) and not isinstance(values[key], typ):
                    raise HttpError(400, "Неверное значение {}".format(key))
        if values.get("skill_permission", "allow") not in ("allow", "ask", "reject"):
            raise HttpError(400, "skill_permission: allow, ask или reject")
        if "entities" in values:
            values["entities"] = clean_entities(values["entities"])
        if int(values.get("poll_seconds", 15)) < 5:
            raise HttpError(400, "Интервал опроса — не меньше 5 секунд")
        current = self.ctx.db.settings()
        work = [values.get(k, current[k]) for k in ("work_start", "work_end")]
        if not all(HHMM_RE.match(w) for w in work) or work[0] >= work[1]:
            raise HttpError(400, "Рабочие часы — ЧЧ:ММ, начало раньше конца")
        if not 1 <= int(values.get("free_slots_count", current["free_slots_count"])) <= 10:
            raise HttpError(400, "Свободных окон — от 1 до 10")
        self.ctx.db.set_settings(values)
        # пустой пароль в форме означает «не менять»: сохранённый секрет наружу не отдаётся
        secrets_update = {k: str(body[k]) for k in SECRET_KEYS
                          if k in body and (body[k] or k == "ews_username")}
        if secrets_update:
            self.ctx.vault.update(**secrets_update)
        self.ctx.worker.reset_source()
        self._json(200, {"ok": True})

    # === категории =======================================================
    @route("GET", "/api/categories")
    def get_categories(self):
        s = self.ctx.db.settings()
        self._json(200, {"categories": self.ctx.db.categories(), "variables": self._variable_titles(),
                         "default_cwd": s["skill_cwd"], "default_permission": s["skill_permission"],
                         "emails": self.ctx.db.recent_emails()})

    def _draft(self, body: dict) -> dict:
        item = body.get("category")
        if not isinstance(item, dict):
            raise HttpError(400, "Нужна категория")
        name = str(item.get("name") or "").strip()
        if not CATEGORY_NAME_RE.match(name):
            raise HttpError(400, "Код категории: латиница, цифры, _ и -, до 32 символов")
        if item.get("permission") not in ("", None, "allow", "ask", "reject"):
            raise HttpError(400, "Разрешения: allow, ask или reject")
        if item.get("approval_mode") not in ("", None, "plan", "default", "auto-edit"):
            raise HttpError(400, "Режим: plan, default или auto-edit")
        if item.get("invite_mode") not in INVITE_MODES + (None,):
            raise HttpError(400, "Режим приглашений: пусто или auto")
        return dict(item, name=name, title=str(item.get("title") or name), prompt=str(item.get("prompt") or ""))

    @route("POST", "/api/categories")
    def save_category(self):
        body = self._body()
        item = self._draft(body)
        original = body.get("original") or None
        if original and not self.ctx.db.category(original):
            raise HttpError(404, "Категория не найдена")
        if item["name"] != original and self.ctx.db.category(item["name"]):
            raise HttpError(409, "Категория с кодом {} уже есть".format(item["name"]))
        self.ctx.db.save_category(original, item)
        self._json(200, {"category": self.ctx.db.category(item["name"])})

    @route("DELETE", "/api/categories/{name}")
    def delete_category(self, name):
        if len(self.ctx.db.categories()) <= 1:
            raise HttpError(400, "Нужна хотя бы одна категория")
        self.ctx.db.delete_category(name)
        self._json(200, {"ok": True})

    @route("POST", "/api/categories/order")
    def order_categories(self):
        names = self._body().get("names")
        if not isinstance(names, list):
            raise HttpError(400, "Нужен список кодов")
        self.ctx.db.order_categories([str(n) for n in names])
        self._json(200, {"ok": True})

    def _trial(self, body: dict):
        """Письмо и список категорий, где сохранённая версия заменена черновиком."""
        draft = self._draft(body)
        email = self._email_or_404(str(body.get("email_id") or ""))
        original = body.get("original") or draft["name"]
        categories = [c for c in self.ctx.db.categories() if c["name"] != original]
        return email, draft, categories + [draft]

    @route("POST", "/api/categories/preview")
    def preview_category(self):
        email, draft, categories = self._trial(self._body())
        s = self.ctx.db.settings()
        self._json(200, {"classify_prompt": build_prompt(email, categories, s["classify_prompt"], int(s["max_body_chars"]),
                                                         s["entities"], s["invite_prompt"]),
                         "run_prompt": render_action(draft.get("action_template") or "", email, draft.get("files"),
                                                     s["entities"])})

    @route("POST", "/api/categories/test")
    def test_category(self):
        email, _, categories = self._trial(self._body())
        try:
            self._json(200, {"ok": True, "result": self.ctx.worker.classify_test(email, categories)})
        except (ClassifyError, RuntimeError, OSError, ValueError) as e:
            self._json(200, {"ok": False, "message": str(e)})

    @route("POST", "/api/settings/test")
    def test_connection(self):
        try:
            self._json(200, {"ok": True, "message": self.ctx.worker.source_factory().test()})
        except MailError as e:
            self._json(200, {"ok": False, "message": str(e)})
        except Exception as e:  # noqa: BLE001 — показываем причину в настройках
            message = "{}: {}".format(type(e).__name__, e)
            self._json(200, {"ok": False, "message": message, "hint": hint_for("outlook", message)})

    @route("POST", "/api/poll")
    def poll_now(self):
        self.ctx.worker.wake.set()
        self._json(200, {"ok": True})


def _local_dt(value: str) -> datetime:
    """Время из формы без часового пояса — местное время сервиса."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())
