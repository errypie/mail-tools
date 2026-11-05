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
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .classifier import ClassifyError, build_prompt, render_action
from .db import CATEGORY_ENV_FIELDS, DEFAULT_CLASSIFY_PROMPT, Database
from .health import hint_for
from .mail_source import MailError
from .vault import Vault, VaultError
from .worker import Worker

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
COOKIE = "mailbot_session"
SESSION_TTL = 12 * 3600
MAX_FAILURES, LOCK_SECONDS = 5, 60
MAX_BODY = 2 * 1024 * 1024
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SETTING_KEYS = {"ews_server": str, "ews_email": str, "ews_auth": str, "ca_bundle": str, "allowed_senders": list,
                "blocked_senders": list,
                "polling_enabled": bool, "poll_seconds": int, "initial_days": int, "gigacode_url": str,
                "skill_cwd": str, "skill_permission": str, "max_body_chars": int, "classify_prompt": str}
SECRET_KEYS = ("ews_username", "ews_password", "gigacode_token")
CATEGORY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


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
        data = self.ctx.db.list_emails(q.get("category"), q.get("status"), q.get("q", ""), limit, offset)
        data["counts"] = self.ctx.db.counts()
        data["worker"] = self.ctx.worker.status
        data["categories"] = [{"name": c["name"], "title": c["title"]} for c in self.ctx.db.categories()]
        self._json(200, data)

    @route("GET", "/api/emails/{eid}")
    def email(self, eid):
        email = self._email_or_404(eid)
        cat = next((c for c in self.ctx.db.categories() if c["name"] == email["category"]), None)
        email["action"] = None
        if cat and cat["action_template"]:
            email["action"] = {"title": cat["action_title"] or "Запустить",
                               "prompt": render_action(cat["action_template"], email, cat["files"])}
        email["environment"] = {k: cat[k] for k in CATEGORY_ENV_FIELDS if cat[k]} if cat else {}
        email["gigacode_url"] = self.ctx.db.settings()["gigacode_url"]
        self._json(200, email)

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
        rid = self.ctx.worker.start_run(email["id"], str(body.get("title") or "Запуск")[:200], prompt,
                                        self.ctx.db.category(email["category"]))
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
        self._json(200, {"settings": s})

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
        if int(values.get("poll_seconds", 15)) < 5:
            raise HttpError(400, "Интервал опроса — не меньше 5 секунд")
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
        self._json(200, {"categories": self.ctx.db.categories(),
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
        self._json(200, {"classify_prompt": build_prompt(email, categories, s["classify_prompt"], int(s["max_body_chars"])),
                         "run_prompt": render_action(draft.get("action_template") or "", email, draft.get("files"))})

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
