"""Приглашения на встречи: занятость, свободные окна, блок для модели и её решение."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Sequence

ACTIONS = ("accept", "tentative", "decline")
ACTION_TEXT = {"accept": "принять", "tentative": "под вопросом", "decline": "отклонить", "none": "ничего не делать"}
INVITE_MODES = ("", "auto")  # "" — модель предлагает, отправляет человек; auto — модель отправляет сама
AUTO_MIN_CONFIDENCE = 0.7
SLOT_SEARCH_DAYS = 14
SLOT_STEP = timedelta(minutes=30)
MAX_REPLY_CHARS = 4000
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

MEETING_CATEGORY = "MEETING"  # приглашения видны в этой категории всегда, какую бы тему ни выбрала модель

DEFAULT_INVITE_PROMPT = (
    "Если время свободно и встреча рабочая, прими её. Если время занято другой встречей, отклони: в reply "
    "вежливо напиши, что слот занят, и предложи свободные окна из списка, а в propose укажи номер лучшего окна. "
    "Если приглашение только для информации, например митап или рассылка, ничего не делай: решит человек."
)


def local_tz():
    return datetime.now().astimezone().tzinfo


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def fmt_range(start: str, end: str, all_day: bool = False) -> str:
    a, b = parse_iso(start).astimezone(local_tz()), parse_iso(end).astimezone(local_tz())
    day = "{} {}".format(WEEKDAYS[a.weekday()], a.strftime("%d.%m"))
    if all_day:
        return day + ", весь день"
    if a.date() == b.date():
        return "{}, {}–{}".format(day, a.strftime("%H:%M"), b.strftime("%H:%M"))
    return "{} {} – {} {}".format(day, a.strftime("%H:%M"), b.strftime("%d.%m"), b.strftime("%H:%M"))


def is_busy(event: dict, own_ids: Sequence[str] = ()) -> bool:
    """Занимает ли событие время. Само приглашение уже лежит в календаре как предварительное, его не считаем."""
    return (event.get("id") not in own_ids and not event.get("cancelled")
            and event.get("free_busy") not in ("Free", "WorkingElsewhere")
            and event.get("my_response") != "Decline")


def overlaps(event: dict, start: datetime, end: datetime) -> bool:
    return parse_iso(event["start"]) < end and parse_iso(event["end"]) > start


def find_free_slots(busy: List[dict], start_from: datetime, duration: timedelta, work_start: str, work_end: str,
                    count: int, days: int = SLOT_SEARCH_DAYS) -> List[dict]:
    """Ближайшие окна нужной длины в рабочие часы с понедельника по пятницу, шагом в полчаса."""
    tz = local_tz()
    start_from = start_from.astimezone(tz)
    intervals = sorted((parse_iso(e["start"]), parse_iso(e["end"])) for e in busy)
    out: List[dict] = []
    for offset in range(days):
        day = start_from.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        cursor = datetime.combine(day, _hhmm(work_start), tz)
        day_end = datetime.combine(day, _hhmm(work_end), tz)
        if cursor < start_from:  # не раньше текущего момента и с ровной получасовой отметки
            cursor += SLOT_STEP * -(-(start_from - cursor) // SLOT_STEP)
        while cursor + duration <= day_end:
            finish = cursor + duration
            clash = next((b for a, b in intervals if a < finish and b > cursor), None)
            if clash is None:
                out.append({"start": to_iso(cursor), "end": to_iso(finish)})
                if len(out) >= count:
                    return out
                cursor = finish
            else:
                cursor += SLOT_STEP * -(-(clash - cursor) // SLOT_STEP)
    return out


def enrich_invite(invite: dict, events: List[dict], settings: dict, now: Optional[datetime] = None) -> dict:
    """Добавляет к приглашению пересечения и свободные окна той же длины."""
    if invite.get("kind") != "request" or not invite.get("start"):
        return invite
    start, end = parse_iso(invite["start"]), parse_iso(invite["end"])
    own = [invite.get("calendar_id")]
    busy = [e for e in events if is_busy(e, own)]
    conflicts = [{"subject": e["subject"], "start": e["start"], "end": e["end"]}
                 for e in busy if overlaps(e, start, end)]
    slots = []
    if not invite.get("all_day"):
        now = now or datetime.now(timezone.utc)
        day_start = datetime.combine(start.astimezone(local_tz()).date(), time(0), local_tz())
        slots = find_free_slots(busy, max(now, day_start), end - start, settings["work_start"], settings["work_end"],
                                int(settings["free_slots_count"]))
    return dict(invite, conflicts=conflicts, free_slots=slots)


def search_window(invite: dict) -> tuple:
    """За какой период нужен календарь, чтобы найти пересечения и окна."""
    start = parse_iso(invite["start"])
    begin = min(start, datetime.now(timezone.utc)) - timedelta(days=1)
    return begin, max(start, datetime.now(timezone.utc)) + timedelta(days=SLOT_SEARCH_DAYS + 1)


def describe(invite: dict) -> dict:
    """Значения для переменных и промпта: время в часовом поясе сервиса."""
    if not invite or not invite.get("start"):
        return {"start": "", "end": "", "when": "", "organizer": (invite or {}).get("organizer", ""),
                "location": (invite or {}).get("location", ""), "conflicts": "", "free_slots": ""}
    conflicts = invite.get("conflicts") or []
    slots = invite.get("free_slots") or []
    local = lambda v: parse_iso(v).astimezone(local_tz()).strftime("%d.%m.%Y %H:%M")  # noqa: E731
    return {
        "start": local(invite["start"]), "end": local(invite["end"]),
        "when": fmt_range(invite["start"], invite["end"], invite.get("all_day")),
        "organizer": invite.get("organizer", ""), "location": invite.get("location", ""),
        "conflicts": "\n".join("- {} ({})".format(c["subject"], fmt_range(c["start"], c["end"])) for c in conflicts),
        "free_slots": "\n".join("{}. {}".format(i, fmt_range(s["start"], s["end"])) for i, s in enumerate(slots, 1)),
    }


def prompt_block(invite: dict, categories: List[dict], base_rules: str) -> tuple:
    """Текст для промпта классификации и кусок JSON-формата ответа."""
    if not invite:
        return "", ""
    if invite.get("kind") == "cancel":
        return "Это отмена встречи. Отвечать на неё не нужно.\n\n", ""
    d = describe(invite)
    rules = "\n".join("- {}: {}".format(c["name"], c["invite_rules"].strip())
                      for c in categories if (c.get("invite_rules") or "").strip())
    block = (
        "Это приглашение на встречу.\n"
        "Категорию выбери по теме встречи, как для обычного письма (например, релиз или ПСИ). Категорию {meeting} "
        "ставь, только если по теме ничего не подходит: приглашение и так попадёт во «Встречи».\nКогда: {when}\nМесто: {location}\nОрганизатор: {organizer}\n"
        "Пересечения в календаре:\n{conflicts}\nСвободные окна той же длительности:\n{slots}\n\n"
        "Как отвечать на приглашения: {rules}\n{cat_rules}"
        "Реши, что сделать с приглашением. Текст reply уйдёт организатору от имени пользователя, "
        "пиши его на русском, коротко и вежливо.\n\n"
    ).format(when=d["when"], location=d["location"] or "не указано", organizer=d["organizer"] or "не указан",
             meeting=MEETING_CATEGORY, conflicts=d["conflicts"] or "нет", slots=d["free_slots"] or "нет",
             rules=(base_rules or DEFAULT_INVITE_PROMPT).strip(),
             cat_rules="Особые правила категорий:\n{}\n".format(rules) if rules else "")
    extra = (', "invite": {"action": "accept" | "tentative" | "decline" | "none", '
             '"reply": "<текст ответа или пустая строка>", "propose": <номер свободного окна или null>}')
    return block, extra


def parse_decision(data: dict, invite: Optional[dict]) -> Optional[dict]:
    if not invite or invite.get("kind") != "request":
        return None
    raw = data.get("invite") if isinstance(data.get("invite"), dict) else {}
    action = str(raw.get("action") or "none").strip().lower()
    if action not in ACTIONS:
        action = "none"
    propose = raw.get("propose")
    slots = invite.get("free_slots") or []
    try:
        propose = int(propose) if propose is not None and 1 <= int(propose) <= len(slots) else None
    except (TypeError, ValueError):
        propose = None
    if action == "accept":
        propose = None
    return {"action": action, "reply": str(raw.get("reply") or "").strip()[:MAX_REPLY_CHARS], "propose": propose}
