"""Фоновая работа: опрос почты, классификация через GigaCode и запуск скиллов."""
from __future__ import annotations

import base64
import os
import shutil
import threading
import uuid
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from urllib.error import URLError

from .classifier import ClassifyError, build_prompt, lines, parse_response, substitute, variables
from .db import Database, meeting_topic, now_iso
from .meetings import MEETING_CATEGORY, ACTION_TEXT, ACTIONS, AUTO_MIN_CONFIDENCE, MAX_REPLY_CHARS, enrich_invite, fmt_range, parse_iso, \
    search_window
from .gigacode_client import GigaCodeClient
from .health import hint_for, timed
from . import calendar_ops, compose, media
from .html_view import clean_fragment, render as render_html, text_document
from .mail_source import MailError, fetch_since, sender_allowed
from .vault import Vault

BATCH = 500
GIGACODE_CHECK_TTL = 5  # с; статус опрашивают часто, а сам сервис проверяем не чаще этого
SERVICE_DOWN_RETRY = 30  # с; как часто пробовать письмо, пока GigaCode недоступен
MAX_SKIPPED_IDS = 5000
ATTACHMENT_CACHE_BYTES = 64 * 1024 * 1024
ROOMS_TTL = 600  # с; список переговорных меняется редко
FOLDERS_TTL = 20  # с; список папок с числом непрочитанных
BACKFILL_PAGE = 50       # писем за один запрос к Exchange при подгрузке старой почты
BACKFILL_PAUSE = 8       # с между запросами, чтобы не нагружать сервер
BACKFILL_IDLE = 600      # с до проверки, не появилось ли новых папок, когда всё подгружено
BACKFILL_SKIP = ("trash", "junk", "outbox", "drafts")
CONTACTS_TTL = 60  # с; подсказки адресатов из истории пересчитываем не чаще
OUTGOING_STYLE = 'font-family: Calibri, Arial, sans-serif; font-size: 11pt; color: #1f1f1f'
INLINE_IMAGE_BYTES = 5 * 1024 * 1024


class Worker:
    def __init__(self, db: Database, vault: Vault, source_factory: Optional[Callable] = None):
        self.db = db
        self.vault = vault
        self.fake_source = source_factory is not None
        self.source_factory = source_factory or self._exchange_source
        self.source = None
        self.wake = threading.Event()
        self.backfill_wake = threading.Event()
        self.status = {"last_poll_at": None, "last_ok_at": None, "last_error": None, "last_new": 0,
                       "skipped_total": 0, "classifier_error": None, "classifier_ok_at": None}
        self._skipped_ids = set()
        self._gigacode_cache = (0.0, None)
        self._att_cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()
        self._contacts = (0.0, [])
        self._folders = (0.0, [])
        self._rooms = (0.0, None)
        self._personal = (0.0, [])
        self._box_lock = threading.Lock()  # опрос и действия пользователя не должны мешать синхронизации
        self.uploads = os.path.join(os.path.dirname(os.path.abspath(db.path)), "uploads")

    def start(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._index_loop, daemon=True).start()
        threading.Thread(target=self._backfill_loop, daemon=True).start()
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
            self._sync_inbox()
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

    # --- вложения, ссылки и поиск -------------------------------------------
    def _index_loop(self):
        """Разбирает новые письма для раздела «Вложения и ссылки» и полнотекстового поиска. Старые — порциями."""
        while True:
            try:
                done = self.db.index_pending(200)
            except Exception as e:  # noqa: BLE001 — сбой разбора не должен ронять сервис
                self.status["index_error"] = "{}: {}".format(type(e).__name__, e)
                done = 0
            time.sleep(0.2 if done else 5)

    def _backfill_loop(self):
        """Старая почта с сервера порциями: для раздела «Вложения и ссылки» и поиска. Модель её не разбирает."""
        self.backfill_wake.wait(30)  # сначала обычный опрос
        while True:
            delay = BACKFILL_IDLE
            try:
                if self.vault.unlocked and self.db.settings()["polling_enabled"]:
                    delay = BACKFILL_PAUSE if self.backfill_step() else BACKFILL_IDLE
                else:
                    delay = 60
            except MailError:
                delay = 120  # не настроено или нет входа
            except Exception as e:  # noqa: BLE001 — сбой подгрузки не мешает остальному
                self.status.setdefault("backfill", {})["error"] = "{}: {}".format(type(e).__name__, e)[:300]
                self.source = None
                delay = 300
            self.backfill_wake.wait(delay)
            self.backfill_wake.clear()

    def backfill_step(self) -> bool:
        """Одна страница старых писем. True — работа ещё есть."""
        days = int(self.db.settings().get("backfill_days") or 0)
        st = self.db.state("backfill") or {}
        if days <= 0:
            self.status["backfill"] = {"enabled": False}
            return False
        if days > st.get("days", 0):  # глубину увеличили: пройдём папки дальше с того места, где остановились
            for f in st.get("folders", {}).values():
                f["done"] = False
        st["days"] = days
        folders = st.setdefault("folders", {})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        boxes = [f for f in self.folders() if f["kind"] not in BACKFILL_SKIP]
        todo = next((f for f in boxes if not folders.get(f["id"], {}).get("done")), None)
        report = {"enabled": True, "days": days, "folders": len(boxes), "loaded": st.get("loaded", 0), "error": None,
                  "done_folders": sum(1 for f in boxes if folders.get(f["id"], {}).get("done"))}
        if todo is None:
            self.status["backfill"] = dict(report, running=False)
            self.db.set_state("backfill", st)
            return False
        fs = folders.setdefault(todo["id"], {})
        before = parse_iso(fs["before"]) if fs.get("before") else None
        items, more = self.calendar().list_folder(todo["id"], before, BACKFILL_PAGE)
        inbox, watermark = self.inbox_id, self.db.state("watermark")
        with self._box_lock:
            for m in items:
                if m["received_at"] >= cutoff:
                    m.setdefault("folder", todo["id"])
                    self.db.upsert_mail(m, inbox, watermark)
        oldest = min((m["received_at"] for m in items), default=None)
        fs["before"] = oldest or fs.get("before")
        fs["done"] = not more or not items or oldest < cutoff
        st["loaded"] = st.get("loaded", 0) + len(items)
        self.db.set_state("backfill", st)
        self.status["backfill"] = dict(report, running=True, folder=todo["name"], loaded=st["loaded"], oldest=oldest,
                                       done_folders=report["done_folders"] + int(fs["done"]))
        return True

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
        self._refresh_invite(email, s)
        items = media.prompt_items(email, s.get("media_exclude_domains") or [])
        prompt = self._prompt(email, categories, s, items)
        try:
            res = self.gigacode()._json("POST", "/api/chat", {"prompt": prompt})
            parsed = parse_response(res.get("response") or "", email, categories, s["entities"], items)
            self.db.save_classification(email["id"], parsed, res.get("sessionId"))
            self.status.update(classifier_error=None, classifier_ok_at=now_iso())
            self.db.resolve_service_errors("gigacode")
            self._auto_reply(email["id"], s)
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
        if email.get("invite"):
            email = dict(email)
            self._refresh_invite(email, s, save=False)
        items = media.prompt_items(email, s.get("media_exclude_domains") or [])
        res = self.gigacode()._json("POST", "/api/chat", {"prompt": self._prompt(email, categories, s, items)})
        return dict(parse_response(res.get("response") or "", email, categories, s["entities"], items), raw=res.get("response"),
                    sessionId=res.get("sessionId"))

    @staticmethod
    def _prompt(email: dict, categories: list, s: dict, media_items=()) -> str:
        return build_prompt(email, categories, s["classify_prompt"], int(s["max_body_chars"]), s["entities"],
                            s["invite_prompt"], media_items)

    # --- календарь --------------------------------------------------------
    def calendar(self):
        """Тот же источник, что и для почты: календарь в том же ящике Exchange."""
        if not self.vault.unlocked:
            raise MailError("Войдите в веб-интерфейс, чтобы открыть календарь")
        if self.source is None:
            self.source = self.source_factory()
        return self.source

    def _refresh_invite(self, email: dict, s: dict, save: bool = True) -> None:
        """Пересечения и окна считаем перед каждым разбором: календарь мог измениться с прихода письма."""
        invite = email.get("invite")
        if not invite or invite.get("kind") != "request" or not invite.get("start"):
            return
        try:
            invite = enrich_invite(invite, self.calendar().events(*search_window(invite)), s)
        except Exception as e:  # noqa: BLE001 — без календаря модель всё равно разберёт письмо
            self.db.record_service_error("outlook", "Календарь: {}: {}".format(type(e).__name__, e))
            return
        email["invite"] = invite
        if save:
            self.db.set_invite(email["id"], invite)

    def respond_invite(self, eid: int, action: str, reply: str = "", propose: Optional[int] = None,
                       by: str = "user") -> dict:
        email = self.db.get_email(eid)
        invite = (email or {}).get("invite")
        if not invite or invite.get("kind") != "request":
            raise ValueError("Это письмо не приглашение на встречу")
        if action not in ACTIONS:
            raise ValueError("Неизвестный ответ: {}".format(action))
        slots = invite.get("free_slots") or []
        proposed = slots[propose - 1] if action != "accept" and propose and 1 <= propose <= len(slots) else None
        if proposed and not invite.get("allow_new_time", True):
            proposed = None  # организатор запретил предлагать другое время
        text = substitute(reply or "", variables(email))[:MAX_REPLY_CHARS].strip()
        response = {"action": action, "reply": text, "proposed": proposed, "by": by, "at": now_iso()}
        if not self.db.claim_invite(eid, response):
            raise ValueError("Ответ на это приглашение уже отправляется")
        try:
            result = self.calendar().respond([invite.get("item_id"), invite.get("calendar_id")], action, text, proposed)
        except Exception as e:  # noqa: BLE001 — ошибку показываем у письма, кнопку можно нажать снова
            response["error"] = "{}: {}".format(type(e).__name__, e)[:1000]
            self.db.finish_invite(eid, response, False, response["error"])
            raise RuntimeError(response["error"])
        response["proposed_sent"] = bool(result.get("proposed"))
        detail = "{}{}".format(ACTION_TEXT[action], ", автоматически" if by == "auto" else "")
        if proposed:
            detail += "; предложено {}".format(fmt_range(proposed["start"], proposed["end"])) if result.get("proposed") \
                else "; Exchange не принял новое время, оно осталось только в тексте"
        self.db.finish_invite(eid, response, True, detail)
        return response

    def _auto_reply(self, eid: int, s: dict) -> None:
        """Авто-режим категории: решение модели уходит организатору без кнопки."""
        email = self.db.get_email(eid)
        invite, decision = email.get("invite"), email.get("invite_decision")
        category = self.db.category(email.get("category")) or {}
        # у тематической категории без своего режима действует режим «Встреч»
        mode = category.get("invite_mode") or (self.db.category(MEETING_CATEGORY) or {}).get("invite_mode")
        if not (s["calendar_auto"] and mode == "auto" and invite and decision
                and decision.get("action") in ACTIONS and not email.get("invite_state")
                and not email.get("category_manual") and (email.get("confidence") or 0) >= AUTO_MIN_CONFIDENCE):
            return
        if invite.get("my_response") in ("Accept", "Decline", "Tentative"):
            return  # уже ответили в Outlook
        if parse_iso(invite["start"]) <= datetime.now(timezone.utc):
            return  # встреча уже началась, отвечать поздно
        try:
            self.respond_invite(eid, decision["action"], decision.get("reply", ""), decision.get("propose"), by="auto")
        except (RuntimeError, ValueError) as e:
            self.db.record_service_error("outlook", "Автоответ на приглашение «{}»: {}".format(email["subject"], e))

    def events(self, start: datetime, end: datetime) -> List[dict]:
        events = self.calendar().events(start, end)
        self._mark_categories(events)
        return events

    def _mark_categories(self, events: List[dict]) -> None:
        """Категория встречи — та, что модель (или вы) поставили письму-приглашению."""
        by_id, by_topic = self.db.invite_categories()
        if not by_id and not by_topic:
            return
        titles = {c["name"]: c["title"] for c in self.db.categories()}
        for e in events:
            cat = by_id.get(e["id"]) or by_id.get(e.get("series_id") or "") or by_topic.get(meeting_topic(e.get("subject")).lower())
            if cat:
                e.update(mail_category=cat, mail_category_title=titles.get(cat, cat))

    def event(self, event_id: str, allow_remote: bool = False) -> dict:
        """Встреча с описанием в том же виде, что письмо, и перепиской по ней."""
        ev = self.calendar().get_event(event_id)
        raw, inline = ev.pop("body_html", "") or "", ev.pop("inline", None) or {}
        if raw.strip():
            ev["doc"], ev["blocked"] = render_html(raw, inline, allow_remote)
        self._mark_categories([ev])
        ev["discussion"] = self.db.meeting_mail(ev.get("conversation_id"), ev.get("subject") or "")
        return ev

    def create_event(self, spec: dict) -> str:
        return self.calendar().create_event(spec)

    def update_event(self, event_id: str, spec: dict, scope: str) -> str:
        return self.calendar().update_event(event_id, spec, scope)

    def cancel_event(self, event_id: str, scope: str, message: str) -> str:
        return self.calendar().cancel_event(event_id, scope, message)

    def free_busy(self, emails: List[str], start: datetime, days: int, duration: int) -> dict:
        """Занятость участников и переговорных плюс окна, когда свободны все (и я сам)."""
        me = self.me()
        people = list(dict.fromkeys(([me] if me else []) + emails))
        horizon = max(days, 7)  # общие окна ищем на неделю вперёд, занятость нужна на весь этот срок
        busy = self.calendar().free_busy(people, start, start + timedelta(days=horizon))
        s = self.db.settings()
        blocks = {k: v.get("blocks", []) for k, v in busy.items()}
        slots = calendar_ops.common_slots(blocks, max(start, datetime.now(timezone.utc)), timedelta(minutes=duration),
                                          s["work_start"], s["work_end"], 6, horizon)
        return {"people": busy, "me": me, "slots": slots, "work_start": s["work_start"], "work_end": s["work_end"]}

    def rooms(self) -> List[dict]:
        at, cached = self._rooms
        if cached is not None and time.time() - at < ROOMS_TTL:
            return cached
        rooms = self.calendar().rooms()
        self._rooms = (time.time(), rooms)
        return rooms

    # --- письмо целиком ---------------------------------------------------
    def attachment(self, email: dict, index: int) -> tuple:
        """(описание, байты). Скачанное держим в памяти: картинки письма запрашиваются при каждом открытии."""
        items = email.get("attachments") or []
        if not 0 <= index < len(items):
            raise ValueError("Нет такого вложения")
        meta = items[index]
        key = (email["id"], index)
        with self._cache_lock:
            if key in self._att_cache:
                self._att_cache.move_to_end(key)
                return meta, self._att_cache[key]
        if not email.get("item_id"):
            raise MailError("Письмо получено старой версией сервиса, вложение можно открыть только в Outlook")
        data = self.calendar().attachment(email["item_id"], meta["id"])
        with self._cache_lock:
            self._att_cache[key] = data
            while sum(len(v) for v in self._att_cache.values()) > ATTACHMENT_CACHE_BYTES and len(self._att_cache) > 1:
                self._att_cache.popitem(last=False)
        return meta, data

    def email_document(self, email: dict, allow_remote: bool) -> tuple:
        """HTML для iframe и число скрытых картинок. Встроенные картинки — data: из вложений."""
        if not email.get("body_html"):
            return text_document(email.get("body") or ""), 0
        inline = {}
        for i, a in enumerate(email.get("attachments") or []):
            if a.get("content_id") and a.get("content_type", "").startswith("image/") and "svg" not in a["content_type"] \
                    and a.get("size", 0) <= INLINE_IMAGE_BYTES:
                try:
                    _, data = self.attachment(email, i)
                except Exception:  # noqa: BLE001 — без картинки письмо всё равно показываем
                    continue
                inline[a["content_id"]] = "data:{};base64,{}".format(a["content_type"], base64.b64encode(data).decode())
        return render_html(email["body_html"], inline, allow_remote)

    # --- ящик ---------------------------------------------------------------
    @property
    def inbox_id(self) -> Optional[str]:
        return self.db.state("inbox_folder")

    def folders(self, fresh: bool = False) -> List[dict]:
        at, cached = self._folders
        if cached and not fresh and time.time() - at < FOLDERS_TTL:
            return cached
        folders = self.calendar().folders()
        for f in folders:
            if f["kind"] in ("inbox", "trash", "sent", "drafts", "junk") and self.db.state(f["kind"] + "_folder") != f["id"]:
                self.db.set_state(f["kind"] + "_folder", f["id"])
        self._folders = (time.time(), folders)
        return folders

    def hidden_folders(self) -> List[str]:
        return [self.db.state("trash_folder") or ""]

    def folder_name(self, folder_id: Optional[str]) -> str:
        return next((f["name"] for f in self._folders[1] if f["id"] == folder_id), "")

    def _sync_inbox(self) -> None:
        """Прочитано, удалено и перемещено в Outlook — переносим к себе. Ошибка не мешает получению почты."""
        source = self.source
        if not hasattr(source, "sync_inbox"):
            return
        try:
            with self._box_lock:
                if not self.inbox_id:
                    self.folders(fresh=True)
                changes, state = source.sync_inbox(self.db.state("inbox_sync"))
                for ch in changes:
                    if ch["kind"] == "read":
                        self.db.set_read_by_item(ch["item_id"], ch["is_read"])
                    elif ch["kind"] == "gone":
                        self.db.mark_gone_by_item(ch["item_id"])
                    elif ch["kind"] == "create":
                        self.db.upsert_mail(ch["mail"], self.inbox_id, self.db.state("watermark"))
                self.db.set_state("inbox_sync", state)
            if changes:
                self._folders = (0.0, self._folders[1])
        except Exception as e:  # noqa: BLE001
            self.db.record_service_error("outlook", "Синхронизация входящих: {}: {}".format(type(e).__name__, e))
            self.db.set_state("inbox_sync", None)  # состояние могло устареть, начнём заново

    def load_folder(self, folder_id: str, older: bool = False) -> dict:
        """Подтягивает страницу писем папки с сервера. Письма, которых на сервере больше нет, прячет."""
        inbox = self.inbox_id
        before = None
        if older:
            rows = self.db.list_emails(folder=folder_id, inbox=inbox, limit=1, offset=0, oldest_first=True)["emails"]
            before = parse_iso(rows[0]["received_at"]) if rows else None
        items, more = self.calendar().list_folder(folder_id, before)
        with self._box_lock:
            seen = set()
            for m in items:
                m.setdefault("folder", folder_id)
                self.db.upsert_mail(m, inbox, self.db.state("watermark"))
                seen.add(m.get("item_id"))
            oldest = min(m["received_at"] for m in items) if more else ""  # без more страница покрыла всю папку
            newest = before.isoformat() if before else None
            known = self.db.item_ids(folder_id, inbox, newest, oldest)
            self.db.mark_gone([eid for item_id, eid in known.items() if item_id and item_id not in seen])
        return {"loaded": len(items), "more": more}

    def _email(self, eid: int) -> dict:
        email = self.db.get_email(eid)
        if not email:
            raise ValueError("Письмо не найдено")
        if not email.get("item_id"):
            raise ValueError("Письмо получено старой версией сервиса или удалено: откройте его в Outlook")
        return email

    def set_read(self, eid: int, read: bool) -> None:
        email = self._email(eid)
        self.calendar().set_read(email["item_id"], read)
        self.db.set_read(eid, read)
        self._folders = (0.0, self._folders[1])

    def move(self, eid: int, folder_id: str) -> dict:
        email = self._email(eid)
        if folder_id == email.get("folder"):
            return email
        with self._box_lock:
            new_id = self.calendar().move(email["item_id"], folder_id)
            self.folders()
            self.db.set_folder(eid, folder_id, new_id, "moved", self.folder_name(folder_id) or folder_id)
        self._folders = (0.0, self._folders[1])
        return self.db.get_email(eid)

    def delete(self, eid: int, permanent: bool = False) -> str:
        """В «Удалённые», а из «Удалённых» — навсегда, как в Outlook."""
        email = self._email(eid)
        trash = self.db.state("trash_folder")
        with self._box_lock:
            if permanent or (trash and email.get("folder") == trash):
                self.calendar().delete_forever(email["item_id"])
                self.db.forget(eid)
                result = "deleted"
            else:
                folder, new_id = self.calendar().trash(email["item_id"])
                self.db.set_folder(eid, folder, new_id, "moved", "Удалённые")
                result = "trash"
        self._folders = (0.0, self._folders[1])
        return result

    def search(self, query: str) -> List[int]:
        items = self.calendar().search(query)
        ids = []
        with self._box_lock:
            for m in items:
                eid = self.db.upsert_mail(m, self.inbox_id, self.db.state("watermark"))
                if eid:
                    ids.append(eid)
        return ids

    def thread(self, eid: int) -> List[int]:
        email = self.db.get_email(eid)
        if not email:
            raise ValueError("Письмо не найдено")
        if not email.get("conversation_id"):
            return [eid]
        try:
            items = self.calendar().thread(email["conversation_id"], email.get("conversation_topic") or "")
            with self._box_lock:
                for m in items:
                    self.db.upsert_mail(m, self.inbox_id, self.db.state("watermark"))
        except Exception as e:  # noqa: BLE001 — цепочку покажем по тому, что уже есть в базе
            self.db.record_service_error("outlook", "Цепочка писем: {}: {}".format(type(e).__name__, e))
        return self.db.thread_ids(email["conversation_id"], email.get("conversation_topic") or "") or [eid]

    # --- написание писем ---------------------------------------------------
    def me(self) -> str:
        return self.db.settings()["ews_email"] or ("me@example.com" if self.fake_source else "")

    def new_draft(self, kind: str, ref: Optional[int]) -> dict:
        if kind not in compose.KINDS:
            raise ValueError("Неизвестный вид письма: {}".format(kind))
        email = self.db.get_email(ref) if ref else None
        if kind != "new" and not email:
            raise ValueError("Письмо для ответа не найдено")
        fields = compose.prefill(kind, email, self.me())
        if kind == "forward":
            # исходные вложения Exchange приложит сам; показываем их, чтобы было видно, что уйдёт
            fields["attachments"] = [{"ref": i, "name": a["name"], "size": a.get("size", 0),
                                      "content_type": a.get("content_type", "")}
                                     for i, a in enumerate(email.get("attachments") or []) if not a.get("inline")]
        sig = self.db.default_signature(reply=kind != "new")
        if sig and sig["body_html"].strip():
            fields["body_html"] = '<p><br></p><div data-gm-sig="1">{}</div>'.format(sig["body_html"])
        did = self.db.add_draft(kind, email["id"] if email else None, fields)
        return self.db.get_draft(did)

    def save_draft(self, did: int, data: dict) -> dict:
        self._editable(did)
        fields = {}
        for key in ("to", "cc", "bcc"):
            if key in data:
                fields[key] = [str(x)[:500] for x in (data[key] or [])][:compose.MAX_RECIPIENTS]
        if "subject" in data:
            fields["subject"] = str(data["subject"] or "")[:1000]
        if "body_html" in data:
            fields["body_html"] = str(data["body_html"] or "")[:1024 * 1024]
        self.db.update_draft(did, fields)
        return self.db.get_draft(did)

    def _editable(self, did: int) -> dict:
        draft = self.db.get_draft(did)
        if not draft:
            raise ValueError("Черновик не найден")
        if draft["status"] in ("sending", "sent"):
            raise ValueError("Письмо уже отправлено" if draft["status"] == "sent" else "Письмо отправляется")
        return draft

    def add_upload(self, did: int, name: str, content_type: str, data: bytes) -> dict:
        draft = self._editable(did)
        name = os.path.basename(name.replace("\\", "/")).strip()[:200] or "файл"
        total = sum(a.get("size", 0) for a in draft["attachments"]) + len(data)
        if total > compose.MAX_UPLOAD:
            raise ValueError("Вложения больше {} МБ: Exchange такое письмо не примет".format(compose.MAX_UPLOAD >> 20))
        folder = os.path.join(self.uploads, str(did))
        os.makedirs(folder, mode=0o700, exist_ok=True)
        file_id = uuid.uuid4().hex
        fd = os.open(os.path.join(folder, file_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.db.update_draft(did, {"attachments": draft["attachments"] + [
            {"file": file_id, "name": name, "size": len(data), "content_type": content_type or "application/octet-stream"}]})
        return self.db.get_draft(did)

    def remove_attachment(self, did: int, index: int) -> dict:
        draft = self._editable(did)
        items = draft["attachments"]
        if not 0 <= index < len(items):
            raise ValueError("Нет такого вложения")
        if "ref" in items[index]:
            raise ValueError("Вложения исходного письма Exchange пересылает вместе с ним")
        item = items.pop(index)
        try:
            os.remove(self._upload_path(did, item["file"]))
        except OSError:
            pass
        self.db.update_draft(did, {"attachments": items})
        return self.db.get_draft(did)

    def upload_data(self, did: int, index: int) -> tuple:
        draft = self.db.get_draft(did)
        items = (draft or {}).get("attachments") or []
        if not 0 <= index < len(items) or "file" not in items[index]:
            raise ValueError("Нет такого вложения")
        with open(self._upload_path(did, items[index]["file"]), "rb") as f:
            return items[index], f.read()

    def _upload_path(self, did: int, file_id: str) -> str:
        if not file_id.isalnum():
            raise ValueError("Неверное вложение")
        return os.path.join(self.uploads, str(did), file_id)

    def delete_draft(self, did: int) -> None:
        draft = self.db.get_draft(did)
        if draft and draft["status"] == "sending":
            raise ValueError("Письмо отправляется")
        self.db.delete_draft(did)
        shutil.rmtree(os.path.join(self.uploads, str(did)), ignore_errors=True)

    def send_draft(self, did: int) -> dict:
        draft = self._editable(did)
        to, cc, bcc = (compose.clean_people(draft[k]) for k in ("to", "cc", "bcc"))
        if not (to or cc or bcc):
            raise ValueError("Укажите хотя бы одного адресата")
        email = self.db.get_email(draft["ref_email_id"]) if draft["ref_email_id"] else None
        if draft["kind"] != "new" and not (email and email.get("item_id")):
            raise ValueError("Исходное письмо получено старой версией сервиса: ответьте на него в Outlook")
        files = [(a["name"], self.upload_data(did, i)[1])
                 for i, a in enumerate(draft["attachments"]) if "file" in a]
        cleaned = clean_fragment(draft["body_html"])
        body = '<div style="{}">{}</div>'.format(OUTGOING_STYLE, cleaned)
        self.db.update_draft(did, {"body_html": cleaned})  # в «Отправленных» то же, что ушло
        if not self.db.claim_draft(did):
            raise ValueError("Письмо уже отправляется")
        try:
            self.calendar().send(draft["kind"], email["item_id"] if email else None, to, cc, bcc,
                                 draft["subject"], body, files)
        except Exception as e:  # noqa: BLE001 — черновик остаётся, можно поправить и отправить снова
            error = "{}: {}".format(type(e).__name__, e)[:1000]
            self.db.finish_draft(did, False, error)
            raise RuntimeError(error)
        self.db.finish_draft(did, True)
        shutil.rmtree(os.path.join(self.uploads, str(did)), ignore_errors=True)
        with self._cache_lock:
            self._contacts = (0.0, [])
        return self.db.get_draft(did)

    def ai_draft(self, did: int, instruction: str, current_text: str) -> dict:
        """Черновик от модели. Только текст: в письмо он попадёт, когда пользователь сам нажмёт «Отправить»."""
        draft = self._editable(did)
        s = self.db.settings()
        email = self.db.get_email(draft["ref_email_id"]) if draft["ref_email_id"] else None
        if draft["kind"] == "new" and not instruction.strip() and not current_text.strip():
            raise ValueError("Напишите, о чём письмо: модели не на что опереться")
        prompt = compose.ai_prompt(draft["kind"], email, instruction, current_text, self.me(), int(s["max_body_chars"]))
        res = self.gigacode()._json("POST", "/api/chat", {"prompt": prompt})
        text = compose.clean_ai_text(res.get("response") or "")
        if not text:
            raise RuntimeError("Модель вернула пустой ответ")
        return {"text": text, "html": compose.text_to_html(text)}

    def contacts(self, q: str, limit: int = 12) -> List[dict]:
        """Подсказка адресатов: сначала с кем чаще переписывались, потом адресная книга Exchange."""
        q = (q or "").strip().lower()
        if not q:
            return []
        people = [p for p in self.personal_contacts() + self._history_contacts()
                  if q in p["email"].lower() or q in p["name"].lower()]
        seen, out = set(), []
        for p in people:  # личные контакты первыми, без повторов
            if p["email"].lower() not in seen:
                seen.add(p["email"].lower())
                out.append(p)
        out = out[:limit]
        if len(q) >= 3 and len(out) < limit:
            try:
                known = {p["email"].lower() for p in out}
                out += [p for p in self.calendar().resolve(q, limit) if p["email"].lower() not in known]
            except Exception:  # noqa: BLE001 — без адресной книги подсказки только из истории
                pass
        return out[:limit]

    def personal_contacts(self) -> List[dict]:
        """Личные контакты Exchange для подсказки адресатов. Без входа или при ошибке — пусто."""
        at, cached = self._personal
        if time.time() - at < CONTACTS_TTL:
            return cached
        out = []
        try:
            for c in self.calendar().contacts_list():
                out += [{"name": c["display_name"], "email": e, "source": "contacts"} for e in c["emails"]]
        except Exception:  # noqa: BLE001
            pass
        self._personal = (time.time(), out)
        return out

    def forget_contacts(self) -> None:
        self._personal = (0.0, [])

    # --- флажки и уведомления -----------------------------------------------
    def set_flag(self, eid: int, flag: int) -> dict:
        email = self._email(eid)
        self.calendar().set_flag(email["item_id"], flag)
        self.db.set_flag(eid, flag)
        return {"flag": flag}

    def new_mail(self, after: int) -> dict:
        last, inbox = self.db.last_email_id(), self.inbox_id or "inbox"
        unread = self.db.unread_count(inbox, inbox)
        if after <= 0:  # первая проверка после загрузки страницы: запоминаем, о старом не уведомляем
            return {"last": last, "emails": [], "unread": unread}
        return {"last": last, "emails": self.db.new_mail_since(after, self.inbox_id), "unread": unread}

    def task_from_email(self, eid: int) -> str:
        email = self.db.get_email(eid)
        if not email:
            raise ValueError("Письмо не найдено")
        note = "Из письма от {} <{}>, {}\n\n{}".format(email.get("sender_name") or "", email.get("sender") or "",
                                                       (email.get("received_at") or "")[:16].replace("T", " "),
                                                       (email.get("body") or "")[:3000])
        from .office import clean_task
        return self.calendar().task_save(None, clean_task({"subject": email.get("subject") or "(без темы)",
                                                           "body": note}))

    def _history_contacts(self) -> List[dict]:
        with self._cache_lock:
            at, cached = self._contacts
            if time.time() - at < CONTACTS_TTL:
                return cached
        stats: dict = {}
        me = self.me().lower()

        def add(text: str, weight: int, when: str) -> None:
            name, addr = compose.parse_person(text)
            if not compose.EMAIL_RE.match(addr) or addr.lower() == me:
                return
            p = stats.setdefault(addr.lower(), {"name": "", "email": addr, "count": 0, "last": "", "source": "history"})
            p["name"] = p["name"] or name
            p["count"] += weight
            p["last"] = max(p["last"], when or "")

        for row in self.db.contact_rows():
            if "sender" in row:
                add("{} <{}>".format(row["sender_name"], row["sender"]) if row["sender_name"] else row["sender"], 1,
                    row["received_at"])
                for p in compose.split_people(row["recipients"]) + compose.split_people(row["cc"]):
                    add(p, 1, row["received_at"])
            else:
                for key in ("to_list", "cc_list", "bcc_list"):
                    for p in json_list(row[key]):
                        add(p, 3, row["sent_at"])  # кому писали сами — важнее
        people = sorted(stats.values(), key=lambda p: (p["count"], p["last"]), reverse=True)
        with self._cache_lock:
            self._contacts = (time.time(), people)
        return people

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


def json_list(value) -> list:
    import json
    try:
        out = json.loads(value or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []
