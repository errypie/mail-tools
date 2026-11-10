"""Остальное из Outlook: флажки, автоответ «нет на месте», правила входящих, личные контакты и задачи.

Здесь проверка форм и две реализации: ExchangeOffice работает с ящиком через EWS, FakeOffice — тестовая, в памяти.
Обе подмешиваются к источникам писем в mail_source.py."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from .compose import EMAIL_RE, parse_person

FLAG_NONE, FLAG_DONE, FLAG_ON = 0, 1, 2      # как PidTagFlagStatus в Exchange
OOF_STATES = ("Disabled", "Enabled", "Scheduled")
OOF_AUDIENCE = ("None", "Known", "All")
TASK_STATUS = ("NotStarted", "InProgress", "Completed", "WaitingOnOthers", "Deferred")
IMPORTANCE = ("Low", "Normal", "High")
PHONE_KINDS = {"mobile": "MobilePhone", "work": "BusinessPhone", "home": "HomePhone"}
MAX_LIST = 20                                 # строк в условии правила
MAX_TEXT = 20000


class OfficeError(RuntimeError):
    pass


def _text(value, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _strings(values, what: str) -> List[str]:
    if isinstance(values, str):
        values = re.split(r"[\n;,]+", values)
    out = [str(v).strip()[:255] for v in values or [] if str(v).strip()]
    if len(out) > MAX_LIST:
        raise ValueError("Слишком много значений: {}".format(what))
    return out


def _emails(values, what: str) -> List[str]:
    out = []
    for v in _strings(values, what):
        addr = parse_person(v)[1]
        if not EMAIL_RE.match(addr):
            raise ValueError("Неверный адрес ({}): {}".format(what, v))
        out.append(addr)
    return out


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.astimezone()


def _day(value) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise ValueError("Дата — ГГГГ-ММ-ДД")


# --- проверка форм -------------------------------------------------------
def clean_oof(body: dict) -> dict:
    state = body.get("state")
    if state not in OOF_STATES:
        raise ValueError("Автоответ: выключен, включён или по расписанию")
    audience = body.get("external_audience") or "None"
    if audience not in OOF_AUDIENCE:
        raise ValueError("Внешним: никому, только контактам или всем")
    out = {"state": state, "external_audience": audience, "internal_reply": _text(body.get("internal_reply"), 8000),
           "external_reply": _text(body.get("external_reply"), 8000), "start": None, "end": None}
    try:
        out["start"], out["end"] = _dt(body.get("start")), _dt(body.get("end"))
    except ValueError:
        raise ValueError("Начало и конец — дата и время")
    if state == "Scheduled":
        if not out["start"] or not out["end"] or out["end"] <= out["start"]:
            raise ValueError("Для расписания нужны начало и конец, конец позже начала")
        if out["end"] < datetime.now(timezone.utc):
            raise ValueError("Конец расписания уже прошёл")
    if state != "Disabled" and not out["internal_reply"]:
        raise ValueError("Напишите текст автоответа")
    if state != "Disabled" and not out["external_reply"]:
        out["external_reply"] = out["internal_reply"]
    return out


RULE_CONDITIONS = ("from", "to", "subject", "body", "sent_to_me", "has_attachments")
RULE_ACTIONS = ("move_to", "mark_read", "delete", "forward_to", "stop")


def clean_rule(body: dict) -> dict:
    name = _text(body.get("name"), 120)
    if not name:
        raise ValueError("Нужно название правила")
    c, a = body.get("conditions") or {}, body.get("actions") or {}
    cond = {"from": _strings(c.get("from"), "от кого"), "to": _strings(c.get("to"), "кому"),
            "subject": _strings(c.get("subject"), "в теме"), "body": _strings(c.get("body"), "в тексте"),
            "sent_to_me": bool(c.get("sent_to_me")), "has_attachments": bool(c.get("has_attachments"))}
    act = {"move_to": _text(a.get("move_to"), 1000) or None, "mark_read": bool(a.get("mark_read")),
           "delete": bool(a.get("delete")), "forward_to": _emails(a.get("forward_to"), "переслать"),
           "stop": bool(a.get("stop"))}
    if not any(cond.values()):
        raise ValueError("Задайте хотя бы одно условие: иначе правило сработает на все письма")
    if not (act["move_to"] or act["mark_read"] or act["delete"] or act["forward_to"]):
        raise ValueError("Задайте действие")
    if act["delete"] and act["move_to"]:
        raise ValueError("Либо удалить, либо переместить")
    return {"name": name, "enabled": body.get("enabled", True) is not False, "conditions": cond, "actions": act}


def clean_contact(body: dict) -> dict:
    out = {k: _text(body.get(k), 255) for k in ("given_name", "surname", "company", "job_title", "department")}
    out["display_name"] = _text(body.get("display_name"), 255) or " ".join(
        x for x in (out["given_name"], out["surname"]) if x)
    out["emails"] = _emails(body.get("emails"), "почта")[:3]
    phones = {}
    for kind in PHONE_KINDS:
        value = _text((body.get("phones") or {}).get(kind), 60)
        if value and not re.match(r"^[\d\s()+\-.#*]{3,60}$", value):
            raise ValueError("Неверный телефон: {}".format(value))
        phones[kind] = value
    out["phones"] = phones
    out["notes"] = _text(body.get("notes"), MAX_TEXT)
    if not out["display_name"] and not out["emails"]:
        raise ValueError("Нужно имя или адрес")
    out["display_name"] = out["display_name"] or out["emails"][0]
    return out


def clean_task(body: dict, partial: bool = False) -> dict:
    out = {}
    if not partial or "subject" in body:
        out["subject"] = _text(body.get("subject"), 255)
        if not out["subject"]:
            raise ValueError("Нужна тема задачи")
    if not partial or "due" in body:
        out["due"] = _day(body.get("due"))
    if not partial or "status" in body:
        out["status"] = body.get("status") or "NotStarted"
        if out["status"] not in TASK_STATUS:
            raise ValueError("Неизвестное состояние задачи")
    if not partial or "importance" in body:
        out["importance"] = body.get("importance") or "Normal"
        if out["importance"] not in IMPORTANCE:
            raise ValueError("Важность: Low, Normal или High")
    if not partial or "body" in body:
        out["body"] = _text(body.get("body"), MAX_TEXT)
    return out


# --- Exchange ------------------------------------------------------------
_flag_registered = False


def register_flag() -> None:
    """Флажок письма в EWS отдельного поля не имеет: это MAPI-свойство PidTagFlagStatus (0x1090)."""
    global _flag_registered
    if _flag_registered:
        return
    from exchangelib import ExtendedProperty
    from exchangelib.items import MeetingCancellation, MeetingRequest, MeetingResponse, Message

    class FlagStatus(ExtendedProperty):
        property_tag = 0x1090
        property_type = "Integer"

    for cls in (Message, MeetingRequest, MeetingResponse, MeetingCancellation):
        try:
            cls.register("flag_status", FlagStatus)
        except ValueError:  # уже зарегистрировано
            pass
    _flag_registered = True


class ExchangeOffice:
    account = None  # задаёт ExchangeSource

    def _item(self, item_id: str):
        found = list(self.account.fetch(ids=[(item_id, None)]))
        if not found or isinstance(found[0], Exception):
            raise OfficeError("Элемент не найден: возможно, его удалили в Outlook")
        return found[0]

    # флажки
    def set_flag(self, item_id: str, flag: int) -> None:
        register_flag()
        item = self._item(item_id)
        item.flag_status = flag or None
        item.save(update_fields=["flag_status"])

    # автоответ
    def get_oof(self) -> dict:
        s = self.account.oof_settings
        iso = lambda v: v.astimezone(timezone.utc).isoformat(timespec="seconds") if v else None  # noqa: E731
        return {"state": s.state, "external_audience": s.external_audience or "None",
                "start": iso(s.start), "end": iso(s.end),
                "internal_reply": _plain(s.internal_reply), "external_reply": _plain(s.external_reply)}

    def set_oof(self, oof: dict) -> None:
        from exchangelib import EWSDateTime, OofSettings
        from exchangelib.ewsdatetime import UTC

        now = datetime.now(timezone.utc)
        start, end = oof["start"] or now, oof["end"] or now + timedelta(days=1)
        if end <= now:
            start, end = now, now + timedelta(days=1)  # для «включён» и «выключен» сроки не важны, но EWS их требует
        conv = lambda v: EWSDateTime.from_datetime(v.astimezone(timezone.utc)).astimezone(UTC)  # noqa: E731
        self.account.oof_settings = OofSettings(
            state=oof["state"], external_audience=oof["external_audience"], start=conv(start), end=conv(end),
            internal_reply=oof["internal_reply"] or " ", external_reply=oof["external_reply"] or " ")

    # правила
    def rules(self) -> List[dict]:
        out = []
        for r in sorted(self.account.rules, key=lambda x: x.priority or 0):
            out.append(_rule_from_ews(r))
        return out

    def _rule_to_ews(self, rule: dict, priority: int, rule_id: Optional[str] = None):
        from exchangelib.folders import FolderId
        from exchangelib.properties import Actions, Address, Conditions, MoveToFolder, Rule

        c, a = rule["conditions"], rule["actions"]
        cond = Conditions(contains_sender_strings=c["from"] or None, contains_recipient_strings=c["to"] or None,
                          contains_subject_strings=c["subject"] or None, contains_body_strings=c["body"] or None,
                          sent_to_me=c["sent_to_me"] or None, has_attachments=c["has_attachments"] or None)
        act = Actions(move_to_folder=MoveToFolder(folder_id=FolderId(id=a["move_to"])) if a["move_to"] else None,
                      mark_as_read=a["mark_read"] or None, delete=a["delete"] or None,
                      forward_to_recipients=[Address(email_address=x) for x in a["forward_to"]] or None,
                      stop_processing_rules=a["stop"] or None)
        return Rule(account=self.account, id=rule_id, display_name=rule["name"], priority=priority,
                    is_enabled=rule["enabled"], conditions=cond, actions=act)

    def create_rule(self, rule: dict) -> str:
        priority = max([r.priority or 0 for r in self.account.rules] + [0]) + 1
        r = self._rule_to_ews(rule, priority)
        self.account.create_rule(r)
        return r.id

    def update_rule(self, rule_id: str, rule: dict) -> None:
        old = next((r for r in self.account.rules if r.id == rule_id), None)
        if old is None:
            raise OfficeError("Правило не найдено: возможно, его удалили в Outlook")
        if _rule_from_ews(old)["unsupported"] and rule.get("only_enabled") is None:
            raise OfficeError("Это правило настроено в Outlook с условиями, которых нет в GigaMail: меняйте его там")
        if rule.get("only_enabled") is not None:
            old.is_enabled = rule["only_enabled"]
            self.account.set_rule(old)
            return
        self.account.set_rule(self._rule_to_ews(rule, old.priority, rule_id))

    def delete_rule(self, rule_id: str) -> None:
        old = next((r for r in self.account.rules if r.id == rule_id), None)
        if old is None:
            raise OfficeError("Правило не найдено")
        self.account.delete_rule(old)

    # контакты
    CONTACT_FIELDS = ("display_name", "given_name", "surname", "company_name", "job_title", "department",
                      "email_addresses", "phone_numbers")

    def contacts_list(self) -> List[dict]:
        from exchangelib.items import Contact

        out = []
        for c in self.account.contacts.all().only(*self.CONTACT_FIELDS)[:2000]:
            if isinstance(c, Contact):
                out.append(_contact_from_ews(c))
        return sorted(out, key=lambda x: x["display_name"].lower())

    def contact_get(self, cid: str) -> dict:
        c = self._item(cid)
        out = _contact_from_ews(c)
        out["notes"] = c.text_body or _plain(c.body)
        return out

    def _contact_apply(self, c, data: dict) -> None:
        from exchangelib import Body
        from exchangelib.indexed_properties import EmailAddress, PhoneNumber

        c.display_name = data["display_name"]
        c.given_name, c.surname = data["given_name"] or None, data["surname"] or None
        c.company_name, c.job_title = data["company"] or None, data["job_title"] or None
        c.department = data["department"] or None
        c.email_addresses = [EmailAddress(email=e, label="EmailAddress%d" % (i + 1))
                             for i, e in enumerate(data["emails"])] or None
        c.phone_numbers = [PhoneNumber(label=PHONE_KINDS[k], phone_number=v)
                           for k, v in data["phones"].items() if v] or None
        c.body = Body(data["notes"]) if data["notes"] else None

    def contact_save(self, cid: Optional[str], data: dict) -> str:
        from exchangelib.items import Contact

        c = self._item(cid) if cid else Contact(account=self.account, folder=self.account.contacts)
        self._contact_apply(c, data)
        c.save()
        return c.id

    def contact_delete(self, cid: str) -> None:
        self._item(cid).move_to_trash()

    # задачи
    TASK_FIELDS = ("subject", "due_date", "status", "importance", "complete_date", "percent_complete",
                   "datetime_created", "is_complete")

    def tasks_list(self) -> List[dict]:
        from exchangelib.items import Task

        return [_task_from_ews(t) for t in self.account.tasks.all().only(*self.TASK_FIELDS)[:1000]
                if isinstance(t, Task)]

    def task_get(self, tid: str) -> dict:
        t = self._item(tid)
        return dict(_task_from_ews(t), body=t.text_body or _plain(t.body))

    def task_save(self, tid: Optional[str], data: dict) -> str:
        from exchangelib import Body, EWSDate
        from exchangelib.items import Task

        t = self._item(tid) if tid else Task(account=self.account, folder=self.account.tasks)
        fields = []
        if "subject" in data:
            t.subject = data["subject"]
            fields.append("subject")
        if "due" in data:
            t.due_date = EWSDate.from_date(date.fromisoformat(data["due"])) if data["due"] else None
            fields.append("due_date")
        if "status" in data:
            t.status = data["status"]
            t.percent_complete = 100 if data["status"] == "Completed" else (0 if t.status == "NotStarted" else
                                                                            t.percent_complete if t.percent_complete
                                                                            and t.percent_complete < 100 else 0)
            fields += ["status", "percent_complete"]
        if "importance" in data:
            t.importance = data["importance"]
            fields.append("importance")
        if "body" in data:
            t.body = Body(data["body"]) if data["body"] else None
            fields.append("body")
        if tid:
            t.save(update_fields=fields)
        else:
            t.save()
        return t.id

    def task_delete(self, tid: str) -> None:
        self._item(tid).move_to_trash()


def _plain(value) -> str:
    from .mail_source import html_to_text

    text = str(value or "")
    return html_to_text(text) if "<" in text else text.strip()


def _rule_from_ews(r) -> dict:
    c, a = r.conditions, r.actions
    known_c = {"contains_sender_strings", "contains_recipient_strings", "contains_subject_strings",
               "contains_body_strings", "sent_to_me", "has_attachments"}
    known_a = {"move_to_folder", "mark_as_read", "delete", "forward_to_recipients", "stop_processing_rules"}
    def used(obj) -> set:
        if not obj:
            return set()
        out = set()
        for f in obj.FIELDS:
            v = getattr(obj, f.name, None)
            if v is None or v is False or (isinstance(v, (list, tuple)) and not v):
                continue
            out.add(f.name)
        return out

    move = a.move_to_folder if a else None
    return {"id": r.id, "name": r.display_name or "", "enabled": bool(r.is_enabled), "priority": r.priority or 0,
            "conditions": {"from": list(c.contains_sender_strings or []) if c else [],
                           "to": list(c.contains_recipient_strings or []) if c else [],
                           "subject": list(c.contains_subject_strings or []) if c else [],
                           "body": list(c.contains_body_strings or []) if c else [],
                           "sent_to_me": bool(c and c.sent_to_me), "has_attachments": bool(c and c.has_attachments)},
            "actions": {"move_to": move.folder_id.id if move and move.folder_id else None,
                        "mark_read": bool(a and a.mark_as_read), "delete": bool(a and a.delete),
                        "forward_to": [x.email_address for x in (a.forward_to_recipients or [])] if a else [],
                        "stop": bool(a and a.stop_processing_rules)},
            "unsupported": bool(used(c) - known_c or used(a) - known_a or r.exceptions or r.is_not_supported),
            "error": bool(r.is_in_error)}


def _contact_from_ews(c) -> dict:
    phones = {k: "" for k in PHONE_KINDS}
    back = {v: k for k, v in PHONE_KINDS.items()}
    for p in c.phone_numbers or []:
        if p.label in back and p.phone_number:
            phones[back[p.label]] = p.phone_number
    return {"id": c.id, "display_name": c.display_name or "", "given_name": c.given_name or "",
            "surname": c.surname or "", "company": c.company_name or "", "job_title": c.job_title or "",
            "department": c.department or "",
            "emails": [e.email for e in sorted(c.email_addresses or [], key=lambda e: e.label or "") if e.email],
            "phones": phones}


def _task_from_ews(t) -> dict:
    due = t.due_date
    return {"id": t.id, "subject": t.subject or "", "due": due.isoformat()[:10] if due else None,
            "status": t.status or "NotStarted", "importance": t.importance or "Normal",
            "done": t.status == "Completed" or bool(t.is_complete),
            "completed": t.complete_date.isoformat() if t.complete_date else None,
            "created": t.datetime_created.isoformat() if t.datetime_created else None}


# --- тестовый ящик -------------------------------------------------------
class FakeOffice:
    def _office_seed(self) -> None:
        if hasattr(self, "_oof"):
            return
        self._oof = {"state": "Disabled", "external_audience": "None", "start": None, "end": None,
                     "internal_reply": "", "external_reply": ""}
        self._rules: List[dict] = [
            {"id": "rule-1", "name": "Рассылки в папку", "enabled": True, "priority": 1,
             "conditions": {"from": ["noreply@", "promo@"], "to": [], "subject": [], "body": [],
                            "sent_to_me": False, "has_attachments": False},
             "actions": {"move_to": "f-news", "mark_read": False, "delete": False, "forward_to": [], "stop": True},
             "unsupported": False, "error": False}]
        self._people: dict = {}
        for i, (name, email, company, job, phone) in enumerate([
                ("Анна Тестова", "qa.lead@example.com", "Пример", "Ведущий тестировщик", "+7 900 000-00-01"),
                ("Борис Разработчиков", "boris.dev@example.com", "Пример", "Разработчик", ""),
                ("Иван Подрядчиков", "ivan@contractor.example.org", "Подрядчик", "Менеджер проекта", "+7 900 000-00-07")]):
            first, last = name.split()
            self._people["contact-%d" % (i + 1)] = {
                "id": "contact-%d" % (i + 1), "display_name": name, "given_name": first, "surname": last,
                "company": company, "job_title": job, "department": "", "emails": [email],
                "phones": {"mobile": phone, "work": "", "home": ""}, "notes": ""}
        today = date.today()
        self._tasks: dict = {}
        for i, (subject, due, status, imp) in enumerate([
                ("Проверить релиз ABC-123", today + timedelta(days=1), "InProgress", "High"),
                ("Обновить чек-лист ПСИ", today + timedelta(days=4), "NotStarted", "Normal"),
                ("Отправить отчёт за октябрь", today - timedelta(days=2), "NotStarted", "Normal"),
                ("Согласовать отпуск", None, "Completed", "Low")]):
            tid = "task-%d" % (i + 1)
            self._tasks[tid] = {"id": tid, "subject": subject, "due": due.isoformat() if due else None,
                                "status": status, "importance": imp, "done": status == "Completed",
                                "completed": datetime.now(timezone.utc).isoformat() if status == "Completed" else None,
                                "created": datetime.now(timezone.utc).isoformat(), "body": ""}

    def set_flag(self, item_id: str, flag: int) -> None:
        if item_id not in self.box:
            raise OfficeError("Письмо не найдено в тестовом ящике")
        self.box[item_id]["flag"] = flag

    def get_oof(self) -> dict:
        self._office_seed()
        o = dict(self._oof)
        for k in ("start", "end"):
            o[k] = o[k].astimezone(timezone.utc).isoformat(timespec="seconds") if o[k] else None
        return o

    def set_oof(self, oof: dict) -> None:
        self._office_seed()
        self._oof = dict(oof)

    def rules(self) -> List[dict]:
        self._office_seed()
        return [dict(r) for r in sorted(self._rules, key=lambda r: r["priority"])]

    def create_rule(self, rule: dict) -> str:
        self._office_seed()
        rid = "rule-" + uuid.uuid4().hex[:6]
        self._rules.append(dict(rule, id=rid, priority=len(self._rules) + 1, unsupported=False, error=False))
        return rid

    def update_rule(self, rule_id: str, rule: dict) -> None:
        self._office_seed()
        old = next((r for r in self._rules if r["id"] == rule_id), None)
        if old is None:
            raise OfficeError("Правило не найдено")
        if rule.get("only_enabled") is not None:
            old["enabled"] = rule["only_enabled"]
        else:
            old.update(rule)

    def delete_rule(self, rule_id: str) -> None:
        self._office_seed()
        self._rules = [r for r in self._rules if r["id"] != rule_id]

    def contacts_list(self) -> List[dict]:
        self._office_seed()
        return sorted(({k: v for k, v in c.items() if k != "notes"} for c in self._people.values()),
                      key=lambda c: c["display_name"].lower())

    def contact_get(self, cid: str) -> dict:
        self._office_seed()
        if cid not in self._people:
            raise OfficeError("Контакт не найден")
        return dict(self._people[cid])

    def contact_save(self, cid: Optional[str], data: dict) -> str:
        self._office_seed()
        cid = cid or "contact-" + uuid.uuid4().hex[:6]
        self._people[cid] = dict(data, id=cid)
        return cid

    def contact_delete(self, cid: str) -> None:
        self._office_seed()
        self._people.pop(cid, None)

    def tasks_list(self) -> List[dict]:
        self._office_seed()
        return [{k: v for k, v in t.items() if k != "body"} for t in self._tasks.values()]

    def task_get(self, tid: str) -> dict:
        self._office_seed()
        if tid not in self._tasks:
            raise OfficeError("Задача не найдена")
        return dict(self._tasks[tid])

    def task_save(self, tid: Optional[str], data: dict) -> str:
        self._office_seed()
        if tid and tid not in self._tasks:
            raise OfficeError("Задача не найдена")
        t = self._tasks.setdefault(tid or "task-" + uuid.uuid4().hex[:6], {
            "subject": "", "due": None, "status": "NotStarted", "importance": "Normal", "body": "",
            "created": datetime.now(timezone.utc).isoformat()})
        tid = tid or next(k for k, v in self._tasks.items() if v is t)
        t.update(data, id=tid)
        t["done"] = t["status"] == "Completed"
        t["completed"] = datetime.now(timezone.utc).isoformat() if t["done"] else None
        return tid

    def task_delete(self, tid: str) -> None:
        self._office_seed()
        self._tasks.pop(tid, None)
