"""Фоновая работа: опрос почты, классификация через GigaCode и запуск скиллов."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Optional
from urllib.error import URLError

from client import GigaCodeClient

from .classifier import ClassifyError, build_prompt, lines, parse_response
from .db import Database, now_iso
from .health import hint_for, timed
from .mail_source import MailError, fetch_since, sender_allowed
from .vault import Vault

BATCH = 500
GIGACODE_CHECK_TTL = 5  # с; статус опрашивают часто, а сам сервис проверяем не чаще этого
SERVICE_DOWN_RETRY = 30  # с; как часто пробовать письмо, пока GigaCode недоступен
MAX_SKIPPED_IDS = 5000


class Worker:
    def __init__(self, db: Database, vault: Vault, source_factory: Optional[Callable] = None):
        self.db = db
        self.vault = vault
        self.fake_source = source_factory is not None
        self.source_factory = source_factory or self._exchange_source
        self.source = None
        self.wake = threading.Event()
        self.status = {"last_poll_at": None, "last_ok_at": None, "last_error": None, "last_new": 0,
                       "skipped_total": 0, "classifier_error": None, "classifier_ok_at": None}
        self._skipped_ids = set()
        self._gigacode_cache = (0.0, None)

    def start(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._classify_loop, daemon=True).start()

    def reset_source(self):
        self.source = None
        self.wake.set()

    def gigacode(self) -> GigaCodeClient:
        s = self.db.settings()
        return GigaCodeClient(s["gigacode_url"], token=self.vault.get("gigacode_token") or None)

    def _exchange_source(self):
        from .mail_source import ExchangeSource
        s = self.db.settings()
        return ExchangeSource(s["ews_server"], s["ews_email"], self.vault.get("ews_username", ""),
                              self.vault.get("ews_password", ""), s["ews_auth"], s["ca_bundle"])

    # --- опрос почты ------------------------------------------------------
    def _poll_loop(self):
        while True:
            s = self.db.settings()
            if self.vault.unlocked and s["polling_enabled"]:
                self.poll_once(s)
            self.wake.wait(max(5, int(s["poll_seconds"])))
            self.wake.clear()

    def poll_once(self, s: dict) -> None:
        self.status["last_poll_at"] = now_iso()
        try:
            if self.source is None:
                self.source = self.source_factory()
            since = fetch_since(self.db.state("watermark"), int(s["initial_days"]))
            messages = self.source.fetch(since, BATCH)
            added = 0
            newest = None
            for m in messages:
                newest = max(newest or m["received_at"], m["received_at"])
                if not sender_allowed(m["sender"], s["allowed_senders"], s["blocked_senders"]):
                    self._note_skipped(m["message_id"])
                elif self.db.add_email(m):
                    added += 1
            if newest:
                self.db.set_state("watermark", newest)
            self.status.update(last_ok_at=now_iso(), last_error=None, last_new=added)
            self.db.resolve_service_errors("outlook")
        except MailError as e:
            self.status["last_error"] = str(e)  # не настроено: это не сбой, в журнал не пишем
        except Exception as e:  # noqa: BLE001 — сбой почты показываем в вебе, сервис продолжает работать
            self.source = None  # пересоздадим подключение на следующем опросе
            self.status["last_error"] = "{}: {}".format(type(e).__name__, e)
            self.db.record_service_error("outlook", self.status["last_error"])

    def _note_skipped(self, message_id: str) -> None:
        # опросы перекрываются на 5 минут, поэтому одно письмо приходит повторно
        if message_id not in self._skipped_ids:
            if len(self._skipped_ids) >= MAX_SKIPPED_IDS:
                self._skipped_ids.clear()
            self._skipped_ids.add(message_id)
            self.status["skipped_total"] += 1

    # --- классификация ----------------------------------------------------
    def _classify_loop(self):
        while True:
            email = self.db.claim_next() if self.vault.unlocked else None
            if email is None:
                time.sleep(2)
                continue
            self.classify(email)

    def classify(self, email: dict) -> None:
        s = self.db.settings()
        categories = self.db.categories()
        prompt = build_prompt(email, categories, s["classify_prompt"], int(s["max_body_chars"]))
        try:
            res = self.gigacode()._json("POST", "/api/chat", {"prompt": prompt})
            parsed = parse_response(res.get("response") or "", email, categories)
            self.db.save_classification(email["id"], parsed, res.get("sessionId"))
            self.status.update(classifier_error=None, classifier_ok_at=now_iso())
            self.db.resolve_service_errors("gigacode")
        except (ClassifyError, RuntimeError, OSError, ValueError) as e:
            self.status["classifier_error"] = str(e)[:500]
            self.db.record_service_error("gigacode", str(e))
            if isinstance(e, URLError) or "Агент не запустился" in str(e):
                self.db.classification_deferred(email["id"], email["attempts"], SERVICE_DOWN_RETRY, str(e))
            else:
                self.db.classification_failed(email["id"], str(e), email["attempts"] + 1)

    def classify_test(self, email: dict, categories: list) -> dict:
        """Пробный разбор с несохранёнными промптами: в письмо ничего не записывается."""
        s = self.db.settings()
        res = self.gigacode()._json("POST", "/api/chat", {
            "prompt": build_prompt(email, categories, s["classify_prompt"], int(s["max_body_chars"]))})
        return dict(parse_response(res.get("response") or "", email, categories), raw=res.get("response"),
                    sessionId=res.get("sessionId"))

    # --- статус -----------------------------------------------------------
    def check_gigacode(self) -> dict:
        at, cached = self._gigacode_cache
        if cached and time.time() - at < GIGACODE_CHECK_TTL:
            return cached
        s = self.db.settings()
        gc = GigaCodeClient(s["gigacode_url"], token=self.vault.get("gigacode_token") or None, timeout=3)
        out = {"url": s["gigacode_url"], "reachable": False, "error": None, "latency_ms": None, "details": None}
        try:
            out["details"], out["latency_ms"] = timed(lambda: gc._json("GET", "/health?deep=1"))
            gc._json("GET", "/capabilities")  # этот маршрут требует токен, /health — нет
            out["reachable"] = True
        except (RuntimeError, OSError, ValueError) as e:
            out["error"] = "{}: {}".format(type(e).__name__, e)
        self._gigacode_cache = (time.time(), out)
        return out

    def check_agent(self) -> dict:
        """Полная проверка: поднимает настоящий процесс агента и сразу закрывает сессию."""
        s = self.db.settings()
        gc = self.gigacode()
        try:
            sid, ms = timed(lambda: gc.create_session(s["skill_cwd"] or None, permission_policy="reject"))
            info = gc.status(sid).get("agentInfo") or {}
            gc.close(sid)
            self.db.resolve_service_errors("gigacode")
            self.status["classifier_error"] = None
            return {"ok": True, "latency_ms": ms, "message": "Агент запущен: {} {}".format(
                info.get("name", "gigacode"), info.get("version", "")).strip()}
        except (RuntimeError, OSError, ValueError) as e:
            self.db.record_service_error("gigacode", str(e))
            return {"ok": False, "message": str(e), "hint": hint_for("gigacode", str(e))}

    def health(self) -> dict:
        s = self.db.settings()
        st = dict(self.status)
        configured = self.fake_source or bool(s["ews_server"] and s["ews_email"] and self.vault.get("ews_username")
                                              and self.vault.get("ews_password"))
        if not s["polling_enabled"]:
            state = "disabled"
        elif not configured:
            state = "not_configured"
        elif st["last_error"]:
            state = "error"
        elif not st["last_ok_at"]:
            state = "waiting"
        else:
            age = time.time() - datetime.fromisoformat(st["last_ok_at"]).timestamp()
            state = "stale" if age > max(60, int(s["poll_seconds"]) * 4) else "ok"
        outlook_error = st["last_error"] if configured else None
        gc = self.check_gigacode()
        gc_state = "error" if not gc["reachable"] else "warning" if st["classifier_error"] else "ok"
        gc_error = gc["error"] or st["classifier_error"]
        counts = self.db.counts()["statuses"]
        return {
            "outlook": {"state": state, "server": s["ews_server"], "mailbox": s["ews_email"], "fake": self.fake_source,
                        "poll_seconds": s["poll_seconds"], "last_poll_at": st["last_poll_at"], "last_ok_at": st["last_ok_at"],
                        "last_new": st["last_new"], "skipped_total": st["skipped_total"], "error": outlook_error,
                        "hint": hint_for("outlook", outlook_error),
                        "allowed": len(s["allowed_senders"]), "blocked": len(s["blocked_senders"])},
            "gigacode": dict(gc, state=gc_state, error=gc_error, hint=hint_for("gigacode", gc_error),
                             classifier_ok_at=st["classifier_ok_at"]),
            "queue": dict(self.db.queue_stats(), waiting=counts.get("new", 0) + counts.get("classifying", 0),
                          failed=counts.get("failed", 0), classified=counts.get("classified", 0)),
            "errors": [dict(e, hint=hint_for(e["source"], e["message"])) for e in self.db.service_errors()],
        }

    # --- скиллы -----------------------------------------------------------
    def start_run(self, email_id: int, title: str, prompt: str, category: Optional[dict] = None) -> int:
        rid = self.db.add_run(email_id, title, prompt)
        threading.Thread(target=self._run_skill, args=(rid, email_id, prompt, category or {}), daemon=True).start()
        return rid

    def _run_skill(self, rid: int, email_id: int, prompt: str, category: dict) -> None:
        s = self.db.settings()
        try:
            gc = self.gigacode()
            sid = gc.create_session(category.get("cwd") or s["skill_cwd"] or None,
                                    permission_policy=category.get("permission") or s["skill_permission"],
                                    include_directories=lines(category.get("include_dirs")))
            self.db.update_run(rid, session_id=sid, status="running")
            if category.get("approval_mode"):
                gc.set_approval_mode(sid, category["approval_mode"])
            if category.get("model"):
                gc.set_model(sid, category["model"])
            gc.prompt(sid, prompt)
            parts = []
            for ev in gc.events(sid, last_event_id=0, until_turn_end=True):
                data = ev.get("data") or {}
                update = data.get("update") or {}
                if update.get("sessionUpdate") == "agent_message_chunk":
                    parts.append((update.get("content") or {}).get("text", ""))
                elif ev.get("type") in ("turn_error", "session_died", "session_closed"):
                    raise RuntimeError(data.get("message") or data.get("error") or data.get("reason") or ev["type"])
            self.db.finish_run(rid, email_id, "done", "".join(parts).strip())
        except (RuntimeError, OSError, ValueError) as e:
            self.db.finish_run(rid, email_id, "error", error=str(e)[:4000])
