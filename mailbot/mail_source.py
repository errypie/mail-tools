"""Источники писем: Exchange через EWS и тестовый генератор."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from typing import List, Optional, Sequence

from .calendar_ops import occurrences
from .office import ExchangeOffice, FakeOffice, register_flag
from .meetings import local_tz, overlaps, parse_iso


class MailError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    SKIP = ("script", "style", "head")
    BREAK = ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 — кривой HTML не должен терять письмо
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def sender_allowed(address: str, allowed: List[str], blocked: Sequence[str] = ()) -> bool:
    """Чёрный список важнее белого. Пустой белый список пропускает всех, кроме чёрного."""
    if sender_matches(address, blocked):
        return False
    return not allowed or sender_matches(address, allowed)


def sender_matches(address: str, rules: Sequence[str]) -> bool:
    """rules: адреса (a@b.ru) и домены (@b.ru или b.ru)."""
    address = (address or "").strip().lower()
    for rule in rules:
        rule = rule.strip().lower()
        if not rule:
            continue
        if "@" in rule and not rule.startswith("@"):
            if address == rule:
                return True
        elif address.endswith("@" + rule.lstrip("@")):
            return True
    return False


class ExchangeSource(ExchangeOffice):
    """Читает Inbox по EWS. Состояние «прочитано» не используется: письмо, открытое
    человеком в Outlook, тоже будет обработано. Дубли отсекает уникальный Message-ID в БД."""

    FIELDS = ("message_id", "subject", "sender", "to_recipients", "cc_recipients", "datetime_received", "text_body",
              "body", "has_attachments", "attachments", "is_read", "conversation_id", "conversation_topic",
              "parent_folder_id", "flag_status")
    PAGE = 50

    def __init__(self, server: str, email: str, username: str, password: str, auth: str = "NTLM",
                 ca_bundle: str = ""):
        from exchangelib import BASIC, DELEGATE, NTLM, Account, Configuration, Credentials
        from exchangelib.protocol import BaseProtocol

        if not (server and email and username and password):
            raise MailError("Не заполнены сервер, адрес, логин или пароль Exchange")
        register_flag()  # флажок в выборках писем — расширенное свойство, его надо объявить заранее
        if ca_bundle:
            BaseProtocol.HTTP_ADAPTER_CLS = _ca_adapter(ca_bundle)
        auth_type = {"NTLM": NTLM, "BASIC": BASIC}.get(auth.upper())
        if auth_type is None:
            raise MailError("Неизвестный тип авторизации: {}".format(auth))
        config = Configuration(server=server, credentials=Credentials(username, password), auth_type=auth_type)
        self.account = Account(primary_smtp_address=email, config=config, autodiscover=False, access_type=DELEGATE)

    def fetch(self, since: datetime, limit: int = 100) -> List[dict]:
        from exchangelib import EWSDateTime
        from exchangelib.items import MeetingCancellation, MeetingRequest, Message

        query = (self.account.inbox.filter(datetime_received__gte=EWSDateTime.from_datetime(since))
                 .only(*self.FIELDS).order_by("datetime_received"))
        out = []
        for item in query[:limit]:
            if isinstance(item, (MeetingRequest, MeetingCancellation)):
                out.append(self._meeting_to_dict(item))
            elif isinstance(item, Message):  # отчёты о доставке и ответы на встречи пропускаем
                out.append(self._to_dict(item))
        return out

    # --- папки и действия с письмами ------------------------------------------
    def folders(self) -> List[dict]:
        """Почтовые папки ящика с числом писем. Календарь, контакты и задачи не показываем."""
        from exchangelib.folders import DeletedItems, Drafts, Inbox, JunkEmail, Outbox, SentItems

        root = self.account.msg_folder_root
        kinds = ((Inbox, "inbox"), (Drafts, "drafts"), (SentItems, "sent"), (DeletedItems, "trash"),
                 (JunkEmail, "junk"), (Outbox, "outbox"))
        self._folder_cache = {}
        out = []
        for f in root.walk():
            if f.folder_class and not f.folder_class.startswith("IPF.Note"):
                continue
            kind = next((k for cls, k in kinds if isinstance(f, cls)), "")
            if not kind and (f.name or "").lower() in ("архив", "archive"):
                kind = "archive"
            parent = f.parent_folder_id.id if f.parent_folder_id else None
            self._folder_cache[f.id] = f
            out.append({"id": f.id, "name": f.name or "", "kind": kind,
                        "parent": None if parent == root.id else parent,
                        "total": f.total_count or 0, "unread": f.unread_count or 0})
        return out

    def _folder(self, folder_id: str):
        cache = getattr(self, "_folder_cache", {})
        if folder_id not in cache:
            self.folders()
            cache = self._folder_cache
        if folder_id not in cache:
            raise MailError("Папка не найдена в Exchange: возможно, её удалили или переименовали")
        return cache[folder_id]

    def _mail_folders(self):
        from exchangelib.folders import FolderCollection

        if not getattr(self, "_folder_cache", None):
            self.folders()
        return FolderCollection(account=self.account, folders=list(self._folder_cache.values()))

    def _dicts(self, items) -> List[dict]:
        from exchangelib.items import MeetingCancellation, MeetingRequest, Message

        out = []
        for item in items:
            if isinstance(item, Exception):
                continue
            if isinstance(item, (MeetingRequest, MeetingCancellation)):
                out.append(self._meeting_to_dict(item))
            elif isinstance(item, Message):
                out.append(self._to_dict(item))
        return out

    def list_folder(self, folder_id: str, before: Optional[datetime] = None, limit: int = PAGE) -> tuple:
        """Страница писем папки, от новых к старым. (письма, есть ли ещё)."""
        from exchangelib import EWSDateTime

        qs = self._folder(folder_id).all()
        if before:
            qs = qs.filter(datetime_received__lt=EWSDateTime.from_datetime(before))
        items = list(qs.only(*self.FIELDS).order_by("-datetime_received")[:limit + 1])
        return self._dicts(items[:limit]), len(items) > limit

    def _light(self, item_id: str):
        found = list(self.account.fetch(ids=[(item_id, None)], only_fields=["is_read", "parent_folder_id"]))
        if not found or isinstance(found[0], Exception):
            raise MailError("Письмо не найдено в Exchange: возможно, его уже удалили или переместили в Outlook")
        return found[0]

    def set_read(self, item_id: str, read: bool) -> None:
        item = self._light(item_id)
        item.is_read = read
        item.save(update_fields=["is_read"])

    def move(self, item_id: str, folder_id: str) -> str:
        """Перемещает письмо и возвращает его новый id: при перемещении Exchange выдаёт новый."""
        item = self._light(item_id)
        item.move(self._folder(folder_id))
        if not item.id:
            raise MailError("Exchange не вернул id перемещённого письма")
        return item.id

    def trash(self, item_id: str) -> tuple:
        """Удаление как в Outlook: в «Удалённые». Возвращает (папка, новый id)."""
        item = self._light(item_id)
        item.move(self.account.trash)
        return self.account.trash.id, item.id

    def delete_forever(self, item_id: str) -> None:
        self._light(item_id).delete()

    def search(self, query: str, limit: int = PAGE) -> List[dict]:
        """Поиск по всем почтовым папкам на сервере. Сначала как в Outlook (AQS), на старых Exchange — по теме и тексту."""
        from exchangelib import Q

        folders = self._mail_folders()
        try:
            items = list(folders.filter(query).only(*self.FIELDS).order_by("-datetime_received")[:limit])
        except Exception:  # noqa: BLE001 — AQS есть только с Exchange 2013
            q = Q(subject__contains=query) | Q(body__contains=query)
            items = list(folders.filter(q).only(*self.FIELDS).order_by("-datetime_received")[:limit])
        return self._dicts(items)

    def thread(self, conversation_id: Optional[str], topic: str, limit: int = 100) -> List[dict]:
        """Все письма цепочки во всех папках."""
        from exchangelib.properties import ConversationId

        folders = self._mail_folders()
        items = []
        if conversation_id:
            try:
                items = list(folders.filter(conversation_id=ConversationId(id=conversation_id))
                             .only(*self.FIELDS).order_by("datetime_received")[:limit])
            except Exception:  # noqa: BLE001 — не все версии Exchange ищут по ConversationId
                items = []
        if not items and topic:
            items = list(folders.filter(conversation_topic=topic).only(*self.FIELDS)
                         .order_by("datetime_received")[:limit])
        return self._dicts(items)

    def sync_inbox(self, state: Optional[str]) -> tuple:
        """Изменения во входящих с прошлого раза: прочитано, удалено или перемещено, появилось.
        Возвращает (изменения, новое состояние). Первый вызов только запоминает состояние."""
        inbox = self.account.inbox
        changes, created = [], []
        for kind, item in inbox.sync_items(sync_state=state or None, only_fields=["is_read"]):
            if kind == "read_flag_change":
                changes.append({"kind": "read", "item_id": item[0].id, "is_read": bool(item[1])})
            elif kind == "delete":
                changes.append({"kind": "gone", "item_id": item.id})
            elif kind == "update":
                changes.append({"kind": "read", "item_id": item.id, "is_read": bool(item.is_read)})
            elif kind == "create" and state:
                created.append(item.id)
        if created:
            full = self.account.fetch(ids=[(i, None) for i in created[:200]], only_fields=list(self.FIELDS))
            changes += [{"kind": "create", "mail": m} for m in self._dicts(full)]
        return changes, inbox.item_sync_state

    def _fresh(self, item_id: Optional[str]):
        """Элемент с актуальным changekey: после обработки в Outlook старый уже не годится."""
        if not item_id:
            return None
        found = list(self.account.fetch(ids=[(item_id, None)]))
        return found[0] if found and not isinstance(found[0], Exception) else None

    def _meeting_to_dict(self, item) -> dict:
        from exchangelib.items import MeetingCancellation

        full = self._fresh(item.id) or item  # в выборке только поля писем, у приглашения их больше
        m = self._to_dict(full)
        invite = {"kind": "cancel" if isinstance(full, MeetingCancellation) else "request", "item_id": full.id}
        if invite["kind"] == "request":
            org = full.organizer
            cal = full.associated_calendar_item_id
            invite.update(start=_utc_iso(full.start), end=_utc_iso(full.end), all_day=bool(full.is_all_day),
                          location=full.location or "", organizer=_address(org) or m["sender"],
                          organizer_name=(org.name if org else "") or m["sender_name"],
                          calendar_id=cal.id if cal else None, my_response=full.my_response_type or "",
                          allow_new_time=full.allow_new_time_proposal is not False)
        m["invite"] = invite
        return m

    EVENT_FIELDS = ("subject", "start", "end", "is_all_day", "location", "organizer", "my_response_type",
                    "legacy_free_busy_status", "is_cancelled", "is_meeting", "type", "reminder_is_set",
                    "reminder_minutes_before_start")

    def events(self, start: datetime, end: datetime) -> List[dict]:
        from exchangelib import EWSDateTime

        view = self.account.calendar.view(start=EWSDateTime.from_datetime(start), end=EWSDateTime.from_datetime(end))
        return sorted((self._event_dict(e) for e in view.only(*self.EVENT_FIELDS)), key=lambda e: e["start"])

    @staticmethod
    def _event_dict(e) -> dict:
        kind = e.type or "Single"
        return {"id": e.id, "subject": e.subject or "", "start": _utc_iso(e.start),
                "end": _utc_iso(e.end + timedelta(days=1) if e.is_all_day and not isinstance(e.end, datetime)
                                else e.end),
                "all_day": bool(e.is_all_day), "location": e.location or "", "organizer": _address(e.organizer),
                "organizer_name": (e.organizer.name if e.organizer else "") or "",
                "my_response": e.my_response_type or "", "free_busy": e.legacy_free_busy_status or "",
                "cancelled": bool(e.is_cancelled), "is_meeting": bool(e.is_meeting), "type": kind,
                "recurring": kind in ("Occurrence", "Exception", "RecurringMaster"),
                "reminder": e.reminder_minutes_before_start if e.reminder_is_set else None}

    def get_event(self, event_id: str) -> dict:
        """Встреча целиком: участники с ответами, переговорные, текст и правило повтора серии."""
        item = self._fresh(event_id)
        if item is None:
            raise MailError("Встреча не найдена: возможно, её удалили в Outlook")
        out = self._event_dict(item)
        people = lambda xs: [{"email": _address(a.mailbox) or (a.mailbox.email_address or ""),  # noqa: E731
                              "name": a.mailbox.name or "", "response": a.response_type or ""} for a in xs or []]
        out.update(body=item.text_body or html_to_text(str(item.body or "")), required=people(item.required_attendees),
                   optional=people(item.optional_attendees), rooms=people(item.resources), recurrence=None)
        master = item
        if out["type"] in ("Occurrence", "Exception"):
            master = item.recurring_master()
            master.refresh()
            out.update(series_id=master.id, series_start=_utc_iso(master.start), series_end=_utc_iso(master.end))
        if out["recurring"] and master.recurrence:
            out["recurrence"] = _rule_from_ews(master.recurrence)
        return out

    def _apply(self, item, spec: dict) -> List[str]:
        """Переносит поля формы в CalendarItem, возвращает список изменённых полей."""
        from exchangelib import Body, EWSDate, EWSDateTime
        from exchangelib.properties import Attendee, Mailbox

        fields = []
        if "subject" in spec:
            item.subject = spec["subject"]
            fields.append("subject")
        if "start" in spec:
            all_day = spec.get("all_day", bool(item.is_all_day))
            if all_day:
                item.start = EWSDate.from_date(spec["start"].date())
                item.end = EWSDate.from_date((spec["end"] - timedelta(days=1)).date())  # exchangelib прибавит день
            else:
                tz = self.account.default_timezone
                item.start = EWSDateTime.from_datetime(spec["start"].astimezone(timezone.utc)).astimezone(tz)
                item.end = EWSDateTime.from_datetime(spec["end"].astimezone(timezone.utc)).astimezone(tz)
            item.is_all_day = all_day
            fields += ["start", "end", "is_all_day"]
        for key, attr in (("location", "location"), ("free_busy", "legacy_free_busy_status")):
            if key in spec:
                setattr(item, attr, spec[key] or None)
                fields.append(attr)
        if "body" in spec:
            item.body = Body(spec["body"]) if spec["body"] else None
            fields.append("body")
        for key, attr in (("required", "required_attendees"), ("optional", "optional_attendees"),
                          ("rooms", "resources")):
            if key in spec:
                setattr(item, attr, [Attendee(mailbox=Mailbox(email_address=a), response_type="Unknown")
                                     for a in spec[key]] or None)
                fields.append(attr)
        if "reminder" in spec:
            item.reminder_is_set = spec["reminder"] is not None
            if spec["reminder"] is not None:
                item.reminder_minutes_before_start = spec["reminder"]
                fields.append("reminder_minutes_before_start")
            fields.append("reminder_is_set")
        if "recurrence" in spec:
            first = spec.get("start") or item.start
            item.recurrence = _rule_to_ews(spec["recurrence"], first) if spec["recurrence"] else None
            fields.append("recurrence")
        return fields

    def respond(self, item_ids: Sequence[Optional[str]], action: str, reply: str = "",
                proposed: Optional[dict] = None) -> dict:
        """Ответ организатору. Сначала по самому приглашению, потом по событию в календаре:
        после ответа в Outlook Exchange удаляет приглашение из входящих."""
        from exchangelib import Body, EWSDateTime
        from exchangelib.items import AcceptItem, DeclineItem, TentativelyAcceptItem
        from exchangelib.properties import ReferenceItemId

        cls = {"accept": AcceptItem, "tentative": TentativelyAcceptItem, "decline": DeclineItem}[action]
        for item_id in item_ids:
            target = self._fresh(item_id)
            if target is None:
                continue
            kwargs = {"body": Body(reply)} if reply else {}
            ref = ReferenceItemId(id=target.id, changekey=target.changekey)
            if proposed:
                try:
                    cls(account=self.account, reference_item_id=ref,
                        proposed_start=EWSDateTime.from_datetime(parse_iso(proposed["start"])),
                        proposed_end=EWSDateTime.from_datetime(parse_iso(proposed["end"])), **kwargs).send()
                    return {"proposed": True}
                except Exception:  # noqa: BLE001 — Exchange до 2013 не умеет предлагать время, отвечаем без этого
                    pass
            cls(account=self.account, reference_item_id=ref, **kwargs).send()
            return {"proposed": False}
        raise MailError("Приглашение не найдено в Exchange: возможно, его удалили или на него уже ответили в Outlook")

    def create_event(self, spec: dict) -> str:
        from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE, CalendarItem

        item = CalendarItem(account=self.account, folder=self.account.calendar)
        self._apply(item, spec)
        invite = bool(spec.get("required") or spec.get("optional") or spec.get("rooms"))
        item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY if invite else SEND_TO_NONE)
        return item.id

    def _target(self, event_id: str, scope: str):
        item = self._fresh(event_id)
        if item is None:
            raise MailError("Встреча не найдена: возможно, её удалили в Outlook")
        if scope == "series" and item.type in ("Occurrence", "Exception"):
            master = item.recurring_master()
            master.refresh()
            return item, master
        return item, item

    def update_event(self, event_id: str, spec: dict, scope: str = "occurrence") -> str:
        """Правка и перенос. Для серии сдвигаем всю серию на столько же, на сколько перенесли этот повтор."""
        from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE

        occurrence, item = self._target(event_id, scope)
        organizer = (item.my_response_type or "") == "Organizer" or not item.is_meeting
        if not organizer and set(spec) - {"reminder"}:
            raise MailError("Это чужая встреча: менять её может только организатор. Можно поменять напоминание")
        spec = dict(spec)
        if item is not occurrence and "start" in spec:
            shift = spec["start"] - occurrence.start
            length = spec["end"] - spec["start"]
            spec["start"] = item.start + shift
            spec["end"] = spec["start"] + length
        if item.type in ("Occurrence", "Exception"):
            spec.pop("recurrence", None)  # у отдельного повтора правила нет
        fields = self._apply(item, spec)
        notify = organizer and item.is_meeting and set(spec) - {"reminder"}
        item.save(update_fields=fields, send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY if notify else SEND_TO_NONE)
        return item.id

    def cancel_event(self, event_id: str, scope: str = "occurrence", message: str = "") -> str:
        """Организатор отменяет с письмом участникам, своя встреча без участников просто удаляется,
        чужую — отклоняем, и Exchange убирает её из календаря."""
        from exchangelib import Body
        from exchangelib.items import SEND_TO_NONE, DeclineItem
        from exchangelib.properties import ReferenceItemId

        _, item = self._target(event_id, scope)
        mine = (item.my_response_type or "") == "Organizer"
        if mine and item.is_meeting:
            item.cancel(**({"body": Body(message)} if message else {}))
            return "cancelled"
        if mine or not item.is_meeting:
            item.move_to_trash(send_meeting_cancellations=SEND_TO_NONE)
            return "deleted"
        DeclineItem(account=self.account, reference_item_id=ReferenceItemId(id=item.id, changekey=item.changekey),
                    **({"body": Body(message)} if message else {})).send()
        return "declined"

    def free_busy(self, emails: Sequence[str], start: datetime, end: datetime) -> dict:
        """Занятость людей и переговорных: {адрес: {"blocks": [...]}} или {"error": текст}."""
        from exchangelib import EWSDateTime

        tz = self.account.default_timezone
        s = EWSDateTime.from_datetime(start.astimezone(timezone.utc)).astimezone(tz)
        e = EWSDateTime.from_datetime(end.astimezone(timezone.utc)).astimezone(tz)
        views = self.account.protocol.get_free_busy_info([(a, "Required", False) for a in emails], s, e,
                                                         merged_free_busy_interval=30, requested_view="Detailed")
        out = {}
        for email, view in zip(emails, views):
            if isinstance(view, Exception):
                out[email] = {"error": str(view) or type(view).__name__}
                continue
            blocks = []
            for ev in view.calendar_events or []:
                bs, be = (v if v.tzinfo else v.replace(tzinfo=tz) for v in (ev.start, ev.end))
                details = ev.details
                blocks.append({"start": _utc_iso(bs), "end": _utc_iso(be), "busy_type": ev.busy_type or "Busy",
                               "subject": (details.subject if details else "") or "",
                               "location": (details.location if details else "") or ""})
            out[email] = {"blocks": blocks}
        return out

    def rooms(self) -> List[dict]:
        """Списки переговорных из адресной книги Exchange (их заводит администратор)."""
        out = []
        for rl in self.account.protocol.get_roomlists():
            if isinstance(rl, Exception):
                continue
            try:
                rooms = [{"email": r.email_address, "name": r.name or r.email_address}
                         for r in self.account.protocol.get_rooms(rl.email_address) if not isinstance(r, Exception)]
            except Exception:  # noqa: BLE001 — один недоступный список не мешает остальным
                rooms = []
            out.append({"email": rl.email_address, "name": rl.name or rl.email_address,
                        "rooms": sorted(rooms, key=lambda r: r["name"])})
        return out

    def test(self) -> str:
        return "Подключено. Писем во входящих: {}".format(self.account.inbox.total_count)

    def send(self, kind: str, ref_item_id: Optional[str], to: Sequence[str], cc: Sequence[str], bcc: Sequence[str],
             subject: str, body_html: str, files: Sequence[tuple]) -> None:
        """Отправка с копией в «Отправленные». Для ответа и пересылки исходное письмо цитирует сам Exchange,
        а при пересылке он же прикладывает исходные вложения. files: [(имя, байты)]."""
        from exchangelib import HTMLBody
        from exchangelib.attachments import FileAttachment
        from exchangelib.items import Message
        from exchangelib.properties import Mailbox

        boxes = lambda xs: [_mailbox(Mailbox, x) for x in xs] or None  # noqa: E731
        body = HTMLBody(body_html or "<p></p>")
        attachments = [FileAttachment(name=name, content=data) for name, data in files]
        if kind == "new":
            msg = Message(account=self.account, folder=self.account.sent, subject=subject, body=body,
                          to_recipients=boxes(to), cc_recipients=boxes(cc), bcc_recipients=boxes(bcc))
            if attachments:
                msg.attach(attachments)  # у несохранённого письма вложения уходят в том же CreateItem
            msg.send_and_save()
            return
        original = self._fresh(ref_item_id)
        if original is None:
            raise MailError("Исходное письмо не найдено в Exchange: возможно, его удалили или переместили")
        if kind == "forward":
            reply = original.create_forward(subject, body, boxes(to), boxes(cc), boxes(bcc))
        else:
            # получателей задаём сами и для «ответить всем»: пользователь мог их поправить
            reply = original.create_reply(subject, body, boxes(to), boxes(cc), boxes(bcc))
        if not attachments:
            reply.send()
            return
        saved = reply.save(self.account.drafts)
        draft = self._fresh(getattr(saved, "id", None))
        if draft is None:
            raise MailError("Exchange не вернул сохранённый черновик ответа")
        draft.attach(attachments)
        draft.send(save_copy=True, copy_to_folder=self.account.sent)

    def resolve(self, query: str, limit: int = 10) -> List[dict]:
        """Поиск в адресной книге организации и личных контактах (ResolveNames)."""
        try:
            found = self.account.protocol.resolve_names([query], return_full_contact_data=False)
        except Exception:  # noqa: BLE001 — нет совпадений или поиск запрещён: подсказки просто из истории
            return []
        out = []
        for m in found or []:
            if isinstance(m, Exception):
                continue
            address = _address(m)
            if address:
                out.append({"name": m.name or "", "email": address, "source": "directory"})
        return out[:limit]

    def attachment(self, item_id: str, attachment_id: str) -> bytes:
        """Содержимое вложения качается по запросу: в базе лежат только имя, тип и размер."""
        from exchangelib.attachments import FileAttachment

        item = self._fresh(item_id)
        if item is None:
            raise MailError("Письмо не найдено в Exchange: возможно, его удалили или переместили")
        for a in item.attachments or []:
            if a.attachment_id and a.attachment_id.id == attachment_id:
                if not isinstance(a, FileAttachment):
                    raise MailError("Вложенное письмо или событие открывается только в Outlook")
                return a.content
        raise MailError("Вложение не найдено в письме")

    @staticmethod
    def _to_dict(item) -> dict:
        from exchangelib.attachments import FileAttachment
        from exchangelib.properties import HTMLBody

        sender = item.sender
        people = lambda xs: ", ".join(_person(r) for r in (xs or []))  # noqa: E731
        body = item.text_body or html_to_text(str(item.body or ""))
        attachments = [{"id": a.attachment_id.id if a.attachment_id else "", "name": a.name or "без имени",
                        "content_type": a.content_type or "application/octet-stream", "size": a.size or 0,
                        "inline": bool(a.is_inline), "content_id": (a.content_id or "").strip("<>").lower(),
                        "file": isinstance(a, FileAttachment)}
                       for a in (item.attachments or [])]
        topic = getattr(item, "conversation_topic", None)
        return {
            "item_id": item.id,
            "folder": item.parent_folder_id.id if item.parent_folder_id else None,
            "is_read": bool(item.is_read),
            "flag": int(getattr(item, "flag_status", None) or 0),
            "conversation_id": item.conversation_id.id if item.conversation_id else None,
            "conversation_topic": topic or "",
            "body_html": str(item.body) if isinstance(item.body, HTMLBody) else "",
            "cc": people(item.cc_recipients),
            "attachments": attachments,
            "message_id": item.message_id or item.id,
            "subject": item.subject or "",
            "sender": (sender.email_address if sender else "") or "",
            "sender_name": (sender.name if sender else "") or "",
            "recipients": people(item.to_recipients),
            # через timestamp: EWSDateTime.astimezone принимает только EWSTimeZone
            "received_at": datetime.fromtimestamp(item.datetime_received.timestamp(), timezone.utc)
            .isoformat(timespec="seconds"),
            "body": body,
            "has_attachments": bool(item.has_attachments),
        }


def _utc_iso(value) -> str:
    """У событий на весь день вместо времени дата: считаем её полночью по местному времени."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, time(0), local_tz()) if isinstance(value, date) else datetime.now(timezone.utc)
    return datetime.fromtimestamp(value.timestamp(), timezone.utc).isoformat(timespec="seconds")


def _rule_to_ews(rule: dict, first):
    """Правило из формы → Recurrence exchangelib. Дни недели у нас 0–6 с понедельника, в EWS — 1–7."""
    from exchangelib import EWSDate
    from exchangelib.recurrence import (AbsoluteMonthlyPattern, AbsoluteYearlyPattern, DailyPattern, Recurrence,
                                        WeeklyPattern)

    day = first.astimezone(local_tz()).date() if isinstance(first, datetime) else first
    pattern = {"daily": lambda: DailyPattern(interval=rule["interval"]),
               "weekly": lambda: WeeklyPattern(interval=rule["interval"], weekdays=[d + 1 for d in rule["days"]],
                                               first_day_of_week=1),
               "monthly": lambda: AbsoluteMonthlyPattern(interval=rule["interval"], day_of_month=day.day),
               "yearly": lambda: AbsoluteYearlyPattern(day_of_month=day.day, month=day.month)}[rule["freq"]]()
    start = EWSDate.from_date(day)
    if rule.get("end") == "date":
        return Recurrence(pattern=pattern, start=start, end=EWSDate.from_date(date.fromisoformat(rule["until"])))
    if rule.get("end") == "count":
        return Recurrence(pattern=pattern, start=start, number=rule["count"])
    return Recurrence(pattern=pattern, start=start)


def _rule_from_ews(rec) -> dict:
    from exchangelib.recurrence import (AbsoluteMonthlyPattern, AbsoluteYearlyPattern, DailyPattern, EndDatePattern,
                                        NumberedPattern, WeeklyPattern)

    p = rec.pattern
    if isinstance(p, DailyPattern):
        rule = {"freq": "daily", "interval": p.interval}
    elif isinstance(p, WeeklyPattern):
        days = p.weekdays if isinstance(p.weekdays, (list, tuple)) else [p.weekdays]
        rule = {"freq": "weekly", "interval": p.interval, "days": sorted(int(d) - 1 for d in days)}
    elif isinstance(p, AbsoluteMonthlyPattern):
        rule = {"freq": "monthly", "interval": p.interval}
    elif isinstance(p, AbsoluteYearlyPattern):
        rule = {"freq": "yearly", "interval": 1}
    else:  # «каждый второй вторник» и подобное — показываем, но править только в Outlook
        return {"freq": "custom", "text": str(p)}
    b = rec.boundary
    if isinstance(b, EndDatePattern):
        rule.update(end="date", until=str(b.end)[:10])
    elif isinstance(b, NumberedPattern):
        rule.update(end="count", count=b.number)
    else:
        rule["end"] = "never"
    return rule


def _person(mailbox) -> str:
    """«Имя <адрес>», как в заголовках письма."""
    name, address = mailbox.name or "", mailbox.email_address or ""
    return "{} <{}>".format(name, address) if name and address and name != address else address or name


def _mailbox(cls, text: str):
    name, address = (text.rsplit("<", 1) + [""])[:2] if "<" in text else ("", text)
    address = address.strip().rstrip(">").strip() or name.strip()
    return cls(name=name.strip().strip('"') or None, email_address=address)


def _address(mailbox) -> str:
    """Внутренние адресаты Exchange бывают без SMTP-адреса, тогда пусто."""
    address = (mailbox.email_address if mailbox else "") or ""
    return address if "@" in address else ""


def _ca_adapter(path: str):
    """Корпоративный корневой сертификат для requests внутри exchangelib."""
    import requests.adapters

    class RootCAAdapter(requests.adapters.HTTPAdapter):
        def cert_verify(self, conn, url, verify, cert):
            super().cert_verify(conn=conn, url=url, verify=path, cert=cert)

    return RootCAAdapter


class FakeSource(FakeOffice):
    """Тестовые письма и календарь без Exchange: по одному новому письму на каждый опрос, пока не кончатся.
    Ответы на приглашения никуда не уходят, они меняют только тестовый календарь."""

    SAMPLES = [
        ("release.manager@example.com", "Релиз ABC-123 готов к приёмке", "Коллеги, просьба проверить релиз ABC-123 до пятницы.", None),
        ("psi.coordinator@example.com", "Приглашение: ПСИ системы «Демо-Портал»",
         "Приглашаем на ПСИ системы «Демо-Портал», релиз 2.4.", (1, "14:00", "15:00", "Переговорная 3")),
        ("noreply@jira.example.ru", "[JIRA] Задача обновлена", "Статус задачи изменён автоматически.", None),
        ("head@example.com", "Приглашение: Встреча отдела", "Еженедельная встреча отдела, быть всем.",
         (1, "16:00", "17:00", "Большая переговорная")),
        ("promo@shop.example.com", "Скидки 90% только сегодня!", "Купите прямо сейчас, предложение ограничено.", None),
        ("events@example.com", "Приглашение: Митап «ИИ в разработке»",
         "Для информации: открытый митап, участие по желанию.", (2, "18:00", "19:00", "Онлайн")),
        ("colleague@example.com", "Перенос встречи", "Встреча по проекту переносится на четверг.", None),
    ]

    def __init__(self):
        self.sent = 0
        self.files: dict = {}
        self.replies: List[dict] = []
        self.outbox: List[dict] = []
        self.box: dict = {}          # item_id → письмо с полями folder и is_read
        self.changes: List[dict] = []  # журнал изменений «из Outlook» для sync_inbox
        self._seed()
        self.calendar: List[dict] = [
            self._event("cal-1", "Созвон по релизу", 1, "14:00", "15:00", "Organizer"),
            self._event("cal-2", "Планёрка", 1, "10:00", "10:30", "Accept", "lead@example.com"),
            self._event("cal-3", "Ревью архитектуры", 2, "11:00", "12:30", "Accept", "architect@example.com"),
            self._event("cal-4", "Стендап команды", 0, "10:00", "10:15", "Accept", "lead@example.com", "Teams"),
            self._event("cal-5", "Обед с командой", 0, "13:00", "14:00", "Tentative", "hr@example.com", "Кафе"),
            self._event("cal-6", "Фокус-время", 2, "15:00", "17:00", "Organizer"),
            dict(self._event("cal-7", "Дежурство по релизам", 3, "00:00", "00:00", "Organizer"), all_day=True,
                 end=_utc_iso(self._at(3, "00:00") + timedelta(days=1))),
            dict(self._event("cal-8", "Синк с командой", 0, "12:00", "12:30", "Organizer", location="Переговорная 2"),
                 is_meeting=True, recurrence={"freq": "weekly", "interval": 1, "days": [1, 3], "end": "never"},
                 required=[self._person(a, "Accept") for a in ("boris.dev@example.com", "vera.analyst@example.com")],
                 rooms=[self._person("room-2@example.com", "Accept")], body="Синхронизация по задачам спринта."),
        ]
        self.calendar[3]["recurrence"] = {"freq": "weekly", "interval": 1, "days": [0, 1, 2, 3, 4], "end": "never"}
        self.meeting_mail: List[dict] = []  # приглашения, изменения и отмены, которые «ушли» участникам

    @staticmethod
    def _at(workday: int, hhmm: str) -> datetime:
        day = datetime.now(local_tz()).date()
        while workday:
            day += timedelta(days=1)
            workday -= day.weekday() < 5
        hours, minutes = hhmm.split(":")
        return datetime.combine(day, time(int(hours), int(minutes)), local_tz())

    def _event(self, eid: str, subject: str, workday: int, start: str, end: str, response: str,
               organizer: str = "me@example.com", location: str = "") -> dict:
        return {"id": eid, "subject": subject, "start": _utc_iso(self._at(workday, start)),
                "end": _utc_iso(self._at(workday, end)), "all_day": False, "location": location,
                "organizer": organizer, "organizer_name": organizer.split("@")[0], "my_response": response,
                "free_busy": "Tentative" if response in ("NoResponseReceived", "Tentative") else "Busy",
                "cancelled": False, "is_meeting": response != "Organizer" or bool(location), "reminder": 15,
                "body": "", "required": [] if response == "Organizer" else [self._person("me@example.com", response)],
                "optional": [], "rooms": [], "recurrence": None, "exceptions": {}, "deleted": []}

    def _person(self, email: str, response: str = "Unknown") -> dict:
        name = next((n for n, e in self.DIRECTORY + self.ROOM_NAMES if e == email), email.split("@")[0])
        return {"email": email, "name": name, "response": response}

    def fetch(self, since: datetime, limit: int = 100) -> List[dict]:
        if self.sent >= len(self.SAMPLES):
            return []
        sender, subject, body, meeting = self.SAMPLES[self.sent]
        self.sent += 1
        m = {"message_id": "<fake-{}@test>".format(self.sent), "subject": subject, "sender": sender,
             "sender_name": sender.split("@")[0], "recipients": "Я <me@example.com>",
             "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": body,
             "has_attachments": False, "item_id": "item-{}".format(self.sent)}
        m.update(folder="f-inbox", is_read=False, conversation_id="conv-{}".format(self.sent),
                 conversation_topic=subject)
        if self.sent == 1:
            self._release_extras(m)
            m.update(conversation_id="conv-release", conversation_topic="ABC-123")
        if meeting:
            workday, start, end, location = meeting
            ev = self._event("cal-inv-{}".format(self.sent), subject.split(": ", 1)[-1], workday, start, end,
                             "NoResponseReceived", sender, location)
            self.calendar.append(ev)  # Exchange сам кладёт приглашение в календарь как предварительное
            m["invite"] = {"kind": "request", "item_id": "inv-{}".format(self.sent), "start": ev["start"],
                           "end": ev["end"], "all_day": False, "location": location, "organizer": sender,
                           "organizer_name": m["sender_name"], "calendar_id": ev["id"],
                           "my_response": "NoResponseReceived", "allow_new_time": True}
        self.box[m["item_id"]] = dict(m)
        return [m]

    def _release_extras(self, m: dict) -> None:
        """Первое тестовое письмо — HTML со стилями, встроенным логотипом, картинкой из интернета и вложениями."""
        files = [("logo", "logo.png", "image/png", _fake_png(), True),
                 ("report", "Отчёт ПСИ ABC-123.pdf", "application/pdf", _fake_pdf("Report ABC-123"), False),
                 ("notes", "Замечания.txt", "text/plain", "1. Проверить логи\n2. Обновить чек-лист\n".encode(), False),
                 ("plan", "План релиза.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  b"PK\x03\x04 fake xlsx", False)]
        m["attachments"] = []
        for aid, name, ctype, data, inline in files:
            self.files[(m["item_id"], aid)] = data
            m["attachments"].append({"id": aid, "name": name, "content_type": ctype, "size": len(data),
                                     "inline": inline, "content_id": "logo@test" if inline else "", "file": True})
        m["has_attachments"] = True
        m["cc"] = "Анна Тестова <qa.lead@example.com>, dev-team@example.com"
        m["body_html"] = FAKE_HTML

    def attachment(self, item_id: str, attachment_id: str) -> bytes:
        try:
            return self.files[(item_id, attachment_id)]
        except KeyError:
            raise MailError("Вложение не найдено")

    # --- календарь -------------------------------------------------------
    PRIVATE = ("body", "required", "optional", "rooms", "recurrence", "exceptions", "deleted")
    ROOM_LISTS = [("rooms-msk@example.com", "Москва, офис на Лесной",
                   [("Переговорная 1 · 4 места", "room-1@example.com"), ("Переговорная 2 · 8 мест", "room-2@example.com"),
                    ("Большая переговорная · 20 мест", "room-big@example.com"), ("Фокус-румка · 2 места", "room-focus@example.com")]),
                  ("rooms-spb@example.com", "Санкт-Петербург",
                   [("Нева · 10 мест", "room-neva@example.com"), ("Фонтанка · 6 мест", "room-fontanka@example.com")])]
    ROOM_NAMES = [r for _, _, rooms in ROOM_LISTS for r in rooms]

    def _public(self, e: dict, **extra) -> dict:
        out = {k: v for k, v in e.items() if k not in self.PRIVATE}
        out.update(type="RecurringMaster" if e.get("recurrence") else "Single", recurring=bool(e.get("recurrence")))
        out.update(extra)
        return out

    def _expand(self, e: dict, start: datetime, end: datetime) -> List[dict]:
        """Повторы серии в окне с учётом перенесённых и удалённых, как их отдаёт CalendarView."""
        out = []
        pad = timedelta(days=31)  # перенесённый повтор может прийти из-за края окна
        for idx, s, f in occurrences(e["recurrence"], parse_iso(e["start"]), parse_iso(e["end"]), start - pad, end + pad):
            if idx in e["deleted"]:
                continue
            occ = self._public(e, id="{}::{}".format(e["id"], idx), start=_utc_iso(s), end=_utc_iso(f), type="Occurrence")
            if idx in e["exceptions"]:
                occ.update({k: v for k, v in e["exceptions"][idx].items() if k not in self.PRIVATE}, type="Exception")
            if overlaps(occ, start, end):
                out.append(occ)
        return out

    def events(self, start: datetime, end: datetime) -> List[dict]:
        out = []
        for e in self.calendar:
            if e.get("recurrence"):
                out += self._expand(e, start, end)
            elif overlaps(e, start, end):
                out.append(self._public(e))
        return sorted(out, key=lambda e: e["start"])

    def _find(self, event_id: str):
        base, _, idx = str(event_id).partition("::")
        ev = next((e for e in self.calendar if e["id"] == base), None)
        if ev is None:
            raise MailError("Встреча не найдена в тестовом календаре")
        return ev, int(idx) if idx else None

    def _occurrence(self, ev: dict, idx: int) -> dict:
        found = [o for o in self._expand(ev, datetime(1970, 1, 1, tzinfo=timezone.utc),
                                         datetime(2100, 1, 1, tzinfo=timezone.utc)) if o["id"].endswith("::%d" % idx)]
        if not found:
            raise MailError("Повтор встречи не найден")
        return found[0]

    def get_event(self, event_id: str) -> dict:
        ev, idx = self._find(event_id)
        out = self._public(ev) if idx is None else self._occurrence(ev, idx)
        details = dict(ev, **(ev["exceptions"].get(idx, {}) if idx else {}))
        out.update({k: details[k] for k in ("body", "required", "optional", "rooms")}, recurrence=ev.get("recurrence"))
        if idx is not None:
            out.update(series_id=ev["id"], series_start=ev["start"], series_end=ev["end"])
        return out

    def _fields(self, spec: dict) -> dict:
        out = {}
        for key in ("subject", "location", "body", "reminder", "free_busy", "all_day", "recurrence"):
            if key in spec:
                out[key] = spec[key]
        if "start" in spec:
            out.update(start=_utc_iso(spec["start"]), end=_utc_iso(spec["end"]))
        for key in ("required", "optional", "rooms"):
            if key in spec:
                out[key] = [self._person(a, "Accept" if key == "rooms" else "Unknown") for a in spec[key]]
        return out

    def _mail(self, kind: str, ev: dict, note: str = "") -> None:
        to = [p["email"] for k in ("required", "optional", "rooms") for p in ev.get(k) or []]
        if to:
            self.meeting_mail.append({"kind": kind, "subject": ev["subject"], "to": to, "note": note})

    def create_event(self, spec: dict) -> str:
        self.created = getattr(self, "created", 0) + 1  # не по длине списка: после удаления номер бы повторился
        eid = "cal-new-{}".format(self.created)
        ev = self._event(eid, spec["subject"], 0, "00:00", "00:00", "Organizer")
        ev.update(self._fields(spec))
        ev["is_meeting"] = bool(ev["required"] or ev["optional"] or ev["rooms"])
        self.calendar.append(ev)
        self._mail("invite", ev)
        return eid

    def update_event(self, event_id: str, spec: dict, scope: str = "occurrence") -> str:
        ev, idx = self._find(event_id)
        if ev["my_response"] != "Organizer" and set(spec) - {"reminder"}:
            raise MailError("Это чужая встреча: менять её может только организатор. Можно поменять напоминание")
        fields = self._fields(spec)
        if idx is not None and scope == "occurrence":
            fields.pop("recurrence", None)
            ev["exceptions"][idx] = dict(ev["exceptions"].get(idx, {}), **fields)
        else:
            if idx is not None and "start" in fields:
                occ = self._occurrence(ev, idx)
                shift = parse_iso(fields["start"]) - parse_iso(occ["start"])
                length = parse_iso(fields["end"]) - parse_iso(fields["start"])
                start = parse_iso(ev["start"]) + shift
                fields.update(start=_utc_iso(start), end=_utc_iso(start + length))
            if "recurrence" in fields and fields["recurrence"] != ev.get("recurrence"):
                ev.update(exceptions={}, deleted=[])  # новое правило — как в Outlook, исключения сбрасываются
            ev.update(fields)
        ev["is_meeting"] = ev["my_response"] != "Organizer" or bool(ev["required"] or ev["optional"] or ev["rooms"])
        if set(spec) - {"reminder"}:
            self._mail("update", ev)
        return event_id

    def cancel_event(self, event_id: str, scope: str = "occurrence", message: str = "") -> str:
        ev, idx = self._find(event_id)
        mine = ev["my_response"] == "Organizer"
        if idx is not None and scope == "occurrence":
            ev["deleted"].append(idx)
        else:
            self.calendar.remove(ev)
        if not mine:
            self.replies.append({"to": ev["organizer"], "action": "decline", "reply": message, "proposed": None})
            return "declined"
        if ev["is_meeting"] and (ev["required"] or ev["optional"] or ev["rooms"]):
            self._mail("cancel", ev, message)
            return "cancelled"
        return "deleted"

    def free_busy(self, emails: Sequence[str], start: datetime, end: datetime) -> dict:
        """Своя занятость — из тестового календаря, переговорных — по встречам с ними,
        остальных из адресной книги — выдуманная, но одинаковая при каждом запросе."""
        import zlib

        rooms = {e for _, e in self.ROOM_NAMES}
        known = {e for _, e in self.DIRECTORY} | rooms | {"me@example.com"}
        out = {}
        mine = [e for e in self.events(start, end) if e["my_response"] != "Decline" and not e["cancelled"]]
        for email in emails:
            if email not in known and not email.endswith("@example.com"):
                out[email] = {"error": "Нет сведений о занятости"}
                continue
            blocks = []
            if email == "me@example.com":
                blocks = [{"start": e["start"], "end": e["end"], "busy_type": e["free_busy"] or "Busy",
                           "subject": e["subject"], "location": e["location"]} for e in mine]
            else:
                for e in mine:
                    full = self.get_event(e["id"])
                    if any(p["email"] == email for k in ("required", "optional", "rooms") for p in full[k]):
                        blocks.append({"start": e["start"], "end": e["end"], "busy_type": "Busy",
                                       "subject": e["subject"] if email in rooms else "", "location": e["location"]})
                day = start.astimezone(local_tz()).date()
                while datetime.combine(day, time(0), local_tz()) < end:
                    seed = zlib.crc32("{}{}".format(email, day).encode())
                    if day.weekday() < 5:
                        for k in range(seed % 3 + (0 if email in rooms else 1)):
                            hour = 9 + (seed >> (4 * k + 2)) % 8
                            s = datetime.combine(day, time(hour, 30 * ((seed >> k) & 1)), local_tz())
                            f = s + timedelta(minutes=30 * (1 + (seed >> (k + 7)) % 3))
                            blocks.append({"start": _utc_iso(s), "end": _utc_iso(f),
                                           "busy_type": "Tentative" if (seed >> (k + 11)) & 3 == 0 else "Busy",
                                           "subject": "", "location": ""})
                    day += timedelta(days=1)
            out[email] = {"blocks": sorted((b for b in blocks if overlaps(b, start, end)), key=lambda b: b["start"])}
        return out

    def rooms(self) -> List[dict]:
        return [{"email": e, "name": n, "rooms": [{"email": re, "name": rn} for rn, re in rooms]}
                for e, n, rooms in self.ROOM_LISTS]

    def respond(self, item_ids: Sequence[Optional[str]], action: str, reply: str = "",
                proposed: Optional[dict] = None) -> dict:
        ev = next((e for e in self.calendar for i in item_ids if i and e["id"] == str(i).partition("::")[0]), None)
        if ev is None:
            raise MailError("Приглашение не найдено в тестовом календаре")
        if action == "decline":
            self.calendar.remove(ev)
        else:
            ev.update(my_response="Accept" if action == "accept" else "Tentative",
                      free_busy="Busy" if action == "accept" else "Tentative")
        self.replies.append({"to": ev["organizer"], "action": action, "reply": reply, "proposed": proposed})
        return {"proposed": bool(proposed)}

    def test(self) -> str:
        return "Тестовый источник работает"

    # --- ящик: папки, перемещение, поиск, цепочки ----------------------------
    FOLDERS = [("f-inbox", "Входящие", "inbox", None), ("f-news", "Рассылки", "", "f-inbox"),
               ("f-drafts", "Черновики", "drafts", None), ("f-sent", "Отправленные", "sent", None),
               ("f-trash", "Удалённые", "trash", None), ("f-junk", "Нежелательная почта", "junk", None),
               ("f-archive", "Архив", "archive", None), ("f-projects", "Проекты", "", None),
               ("f-demo", "Демо-Портал", "", "f-projects")]
    OLD = [  # (папка, дней назад, отправитель, тема, текст, цепочка, прочитано)
        ("f-archive", 6, "release.manager@example.com", "ABC-123: план приёмки",
         "Коллеги, прикладываю план приёмки релиза ABC-123. Прошу замечания до среды.", "conv-release", True),
        ("f-sent", 5, "me@example.com", "RE: ABC-123: план приёмки",
         "Посмотрел план, замечаний нет. Готовы принимать после выкладки на стенд.", "conv-release", True),
        ("f-inbox", 5, "lead@example.com", "Итоги планирования спринта",
         "Цели спринта: закрыть ABC-123 и подготовить ПСИ Демо-Портала.", "conv-sprint", True),
        ("f-inbox", 8, "hr@example.com", "Корпоративный портал: новые правила отпусков",
         "С 1 числа заявления на отпуск подаются через портал.", "conv-hr", False),
        ("f-inbox", 12, "noreply@ci.example.com", "Сборка main #1520 прошла успешно",
         "Все тесты зелёные. Время сборки 14 минут.", "conv-ci", True),
        ("f-news", 2, "digest@news.example.com", "Дайджест разработки: неделя 45",
         "Новости платформы, статьи и анонсы митапов.", "conv-digest", False),
        ("f-junk", 1, "winner@lottery.example.net", "Вы выиграли миллион!",
         "Переведите комиссию, чтобы получить выигрыш.", "conv-spam", False),
        ("f-demo", 3, "architect@example.com", "Демо-Портал: схема интеграции",
         "Обновил схему интеграции с платёжным шлюзом, посмотрите раздел 3.", "conv-demo", True),
        ("f-demo", 4, "vera.analyst@example.com", "Демо-Портал: требования к отчётам",
         "Собрала требования к отчётам, жду комментариев до пятницы.", "conv-demo2", False),
    ]

    def _seed(self) -> None:
        for i, (folder, days, sender, subject, body, conv, read) in enumerate(self.OLD, 1):
            at = datetime.now(timezone.utc) - timedelta(days=days, hours=i)
            item_id = "old-{}".format(i)
            self.box[item_id] = {
                "message_id": "<old-{}@test>".format(i), "item_id": item_id, "subject": subject, "sender": sender,
                "sender_name": sender.split("@")[0], "recipients": "Я <me@example.com>" if sender != "me@example.com"
                else "release.manager <release.manager@example.com>", "received_at": at.isoformat(timespec="seconds"),
                "body": body, "has_attachments": False, "folder": folder, "is_read": read,
                "conversation_id": conv, "conversation_topic": subject.split(": ", 1)[-1] if conv != "conv-release" else "ABC-123",
                "flag": 2 if i == 3 else 0}

    def folders(self) -> List[dict]:
        out = []
        for fid, name, kind, parent in self.FOLDERS:
            items = [m for m in self.box.values() if m["folder"] == fid]
            out.append({"id": fid, "name": name, "kind": kind, "parent": parent, "total": len(items),
                        "unread": sum(not m["is_read"] for m in items)})
        return out

    def _item(self, item_id: str) -> dict:
        if item_id not in self.box:
            raise MailError("Письмо не найдено в Exchange: возможно, его уже удалили или переместили в Outlook")
        return self.box[item_id]

    def list_folder(self, folder_id: str, before: Optional[datetime] = None, limit: int = 50) -> tuple:
        if folder_id not in {f[0] for f in self.FOLDERS}:
            raise MailError("Папка не найдена")
        items = sorted((m for m in self.box.values() if m["folder"] == folder_id
                        and (not before or parse_iso(m["received_at"]) < before)),
                       key=lambda m: m["received_at"], reverse=True)
        return [dict(m) for m in items[:limit]], len(items) > limit

    def set_read(self, item_id: str, read: bool) -> None:
        self._item(item_id)["is_read"] = read

    def move(self, item_id: str, folder_id: str) -> str:
        if folder_id not in {f[0] for f in self.FOLDERS}:
            raise MailError("Папка не найдена")
        self._item(item_id)["folder"] = folder_id
        return item_id

    def trash(self, item_id: str) -> tuple:
        return "f-trash", self.move(item_id, "f-trash")

    def delete_forever(self, item_id: str) -> None:
        self.box.pop(self._item(item_id)["item_id"])

    def search(self, query: str, limit: int = 50) -> List[dict]:
        q = query.lower()
        found = [dict(m) for m in self.box.values()
                 if any(q in (m.get(k) or "").lower() for k in ("subject", "body", "sender", "sender_name"))]
        return sorted(found, key=lambda m: m["received_at"], reverse=True)[:limit]

    def thread(self, conversation_id: Optional[str], topic: str, limit: int = 100) -> List[dict]:
        found = [dict(m) for m in self.box.values() if conversation_id and m.get("conversation_id") == conversation_id]
        return sorted(found, key=lambda m: m["received_at"])[:limit]

    def sync_inbox(self, state: Optional[str]) -> tuple:
        start = int(state) if state else len(self.changes)
        return [dict(c) for c in self.changes[start:]], str(len(self.changes))

    def outlook_change(self, kind: str, item_id: str, **extra) -> None:
        """Для тестов: изменение, сделанное как будто в Outlook."""
        m = self._item(item_id)
        if kind == "read":
            m["is_read"] = extra["is_read"]
            self.changes.append({"kind": "read", "item_id": item_id, "is_read": extra["is_read"]})
        elif kind == "move":
            was = m["folder"]
            m["folder"] = extra["folder"]
            if was == "f-inbox":
                self.changes.append({"kind": "gone", "item_id": item_id})
            if extra["folder"] == "f-inbox":
                self.changes.append({"kind": "create", "mail": dict(m)})

    DIRECTORY = [("Анна Тестова", "qa.lead@example.com"), ("Борис Разработчиков", "boris.dev@example.com"),
                 ("Вера Аналитикова", "vera.analyst@example.com"), ("Команда разработки", "dev-team@example.com"),
                 ("Григорий Архитекторов", "architect@example.com"), ("Дарья Кадрова", "hr@example.com")]

    def send(self, kind: str, ref_item_id: Optional[str], to: Sequence[str], cc: Sequence[str], bcc: Sequence[str],
             subject: str, body_html: str, files: Sequence[tuple]) -> None:
        if any("fail" in x for x in list(to) + list(cc)):
            raise MailError("Тестовая ошибка отправки: адрес содержит fail")
        if kind != "new" and not ref_item_id:
            raise MailError("Исходное письмо не найдено")
        self.outbox.append({"kind": kind, "ref": ref_item_id, "to": list(to), "cc": list(cc), "bcc": list(bcc),
                            "subject": subject, "body_html": body_html,
                            "files": [(name, len(data)) for name, data in files]})
        ref = self.box.get(ref_item_id or "") or {}
        item_id = "sent-{}".format(len(self.outbox))
        self.box[item_id] = {
            "message_id": "<{}@test>".format(item_id), "item_id": item_id, "subject": subject, "sender": "me@example.com",
            "sender_name": "Я", "recipients": ", ".join(to), "cc": ", ".join(cc),
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": html_to_text(body_html),
            "body_html": body_html, "has_attachments": bool(files), "folder": "f-sent", "is_read": True,
            "conversation_id": ref.get("conversation_id") or item_id, "conversation_topic": ref.get("conversation_topic") or subject}

    def resolve(self, query: str, limit: int = 10) -> List[dict]:
        q = query.lower()
        return [{"name": n, "email": e, "source": "directory"} for n, e in self.DIRECTORY
                if q in n.lower() or q in e.lower()][:limit]


def fetch_since(watermark: Optional[str], initial_days: int) -> datetime:
    """С какого момента запрашивать письма. Перекрытие 5 минут ловит письма с запоздалой доставкой."""
    if watermark:
        return datetime.fromisoformat(watermark) - timedelta(minutes=5)
    return datetime.now(timezone.utc) - timedelta(days=initial_days)


FAKE_HTML = """<html><head><style>
.hdr { background: #21a038; color: #fff; padding: 18px 24px; border-radius: 8px 8px 0 0; }
.box { border: 1px solid #e1e1e1; border-radius: 0 0 8px 8px; padding: 20px 24px; }
table.t { border-collapse: collapse; width: 100%; margin: 12px 0; }
table.t td, table.t th { border: 1px solid #e1e1e1; padding: 6px 10px; text-align: left; }
table.t th { background: #f5f5f5; }
.ok { color: #107c10; font-weight: 600; } .warn { color: #c50f1f; font-weight: 600; }
</style><script>alert('xss')</script></head><body>
<div class="hdr"><img src="cid:logo@test" alt="logo" width="120" height="40" style="vertical-align: middle">
<span style="font-size: 20px; margin-left: 12px">Релиз ABC-123</span></div>
<div class="box">
<p>Коллеги, добрый день!</p>
<p>Релиз <b>ABC-123</b> готов к приёмке. Просьба проверить <u>до пятницы</u>.</p>
<table class="t"><tr><th>Компонент</th><th>Версия</th><th>Статус</th></tr>
<tr><td>Демо-Портал</td><td>2.4.0</td><td class="ok">готово</td></tr>
<tr><td>Шлюз платежей</td><td>1.9.3</td><td class="warn">ждёт тестов</td></tr></table>
<p><img src="https://example.com/banner.png" alt="баннер" width="300" height="60" onerror="alert(1)"></p>
<p>Подробности в <a href="https://jira.example.com/browse/ABC-123" onclick="alert(2)">Jira</a>
и <a href="javascript:alert(3)">опасной ссылке</a>.</p>
<blockquote>С уважением,<br>релиз-менеджер</blockquote>
</div></body></html>"""


def _fake_png(width: int = 120, height: int = 40) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    # каждая строка: байт фильтра и пиксели в шахматном порядке
    raw = b"".join(b"\x00" + b"".join(bytes((255, 255, 255) if (x // 10 + y // 10) % 2 else (33, 160, 56))
                                      for x in range(width)) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _fake_pdf(text: str) -> bytes:
    stream = "BT /F1 24 Tf 72 720 Td ({}) Tj ET".format(text).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    out += b"".join("{:010d} 00000 n \n".format(o).encode() for o in offsets)
    out += b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    return out
