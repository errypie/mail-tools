"""Календарь: проверка формы встречи, правила повтора и их развёртка, общее свободное время участников."""
from __future__ import annotations

import calendar as _cal
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator, List, Optional, Sequence, Tuple

from .compose import EMAIL_RE, parse_person
from .meetings import find_free_slots, local_tz, parse_iso, to_iso

FREQS = ("daily", "weekly", "monthly", "yearly")
FREE_BUSY = ("Free", "Tentative", "Busy", "OOF", "WorkingElsewhere")
REMINDERS = (0, 5, 10, 15, 30, 60, 120, 1440, 2880, 10080)
SCOPES = ("occurrence", "series")
MAX_ATTENDEES = 100
MAX_ROOMS = 5
MAX_EXPAND = 3000          # больше повторов за раз не разворачиваем
MAX_FREEBUSY_PEOPLE = 30
MAX_FREEBUSY_DAYS = 14


def _people(values, what: str, limit: int) -> List[str]:
    if isinstance(values, str):
        values = [v for v in values.replace(";", ",").split(",")]
    out, seen = [], set()
    for v in values or []:
        addr = parse_person(str(v))[1].strip()
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            raise ValueError("Неверный адрес ({}): {}".format(what, v))
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    if len(out) > limit:
        raise ValueError("Слишком много: {}".format(what))
    return out


def _local_dt(value: str) -> datetime:
    """Время из формы без часового пояса — местное время сервиса."""
    text = str(value).strip()
    if text.endswith("Z"):  # так отдаёт время браузер (toISOString); Python 3.9 «Z» не понимает
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())


def clean_rule(raw) -> Optional[dict]:
    """Правило повтора из формы. None — без повтора."""
    if not raw:
        return None
    if not isinstance(raw, dict) or raw.get("freq") not in FREQS:
        raise ValueError("Повтор: каждый день, неделю, месяц или год")
    try:
        interval = int(raw.get("interval") or 1)
    except (TypeError, ValueError):
        raise ValueError("Повтор: интервал — число")
    if not 1 <= interval <= 99:
        raise ValueError("Повтор: интервал от 1 до 99")
    rule = {"freq": raw["freq"], "interval": 1 if raw["freq"] == "yearly" else interval, "end": "never"}
    if raw["freq"] == "weekly":
        try:
            days = sorted({int(d) for d in raw.get("days") or []})
        except (TypeError, ValueError):
            raise ValueError("Повтор: дни недели — числа 0–6")
        if not days or days[0] < 0 or days[-1] > 6:
            raise ValueError("Повтор по неделям: выберите дни недели")
        rule["days"] = days
    end = raw.get("end") or "never"
    if end == "date":
        try:
            rule.update(end="date", until=date.fromisoformat(str(raw.get("until"))).isoformat())
        except ValueError:
            raise ValueError("Повтор: дата окончания ГГГГ-ММ-ДД")
    elif end == "count":
        try:
            count = int(raw.get("count"))
        except (TypeError, ValueError):
            raise ValueError("Повтор: число повторений")
        if not 1 <= count <= 999:
            raise ValueError("Повтор: от 1 до 999 раз")
        rule.update(end="count", count=count)
    elif end != "never":
        raise ValueError("Повтор: окончание never, date или count")
    return rule


def clean_spec(body: dict, partial: bool = False) -> dict:
    """Поля встречи из формы. partial — только присланные поля (перенос перетаскиванием, смена напоминания)."""
    spec: dict = {}
    has = (lambda k: k in body) if partial else (lambda k: True)  # noqa: E731
    if has("subject"):
        spec["subject"] = str(body.get("subject") or "").strip()[:255]
        if not spec["subject"]:
            raise ValueError("Нужна тема встречи")
    if has("all_day"):
        spec["all_day"] = bool(body.get("all_day"))
    if has("start") or has("end"):
        try:
            start, end = _local_dt(body["start"]), _local_dt(body["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Начало и конец — дата и время")
        if spec.get("all_day"):
            start = datetime.combine(start.date(), time(0), start.tzinfo)
            end = datetime.combine(max(end.date(), start.date() + timedelta(days=1)), time(0), start.tzinfo)
        if end <= start:
            raise ValueError("Конец встречи должен быть позже начала")
        if end - start > timedelta(days=31):
            raise ValueError("Встреча длиннее месяца — так не бывает")
        spec.update(start=start, end=end)
    for key in ("location", "body"):
        if has(key):
            spec[key] = str(body.get(key) or "")[:255 if key == "location" else 20000]
    for key, what, limit in (("required", "участники", MAX_ATTENDEES), ("optional", "по желанию", MAX_ATTENDEES),
                             ("rooms", "переговорные", MAX_ROOMS)):
        if has(key):
            spec[key] = _people(body.get(key), what, limit)
    if has("reminder"):
        value = body.get("reminder")
        if value in (None, "", False, -1, "-1"):
            spec["reminder"] = None
        else:
            try:
                spec["reminder"] = max(0, min(int(value), 4 * 7 * 24 * 60))
            except (TypeError, ValueError):
                raise ValueError("Напоминание — минуты до начала")
    if has("free_busy"):
        spec["free_busy"] = body.get("free_busy") if body.get("free_busy") in FREE_BUSY else "Busy"
    if has("recurrence"):
        spec["recurrence"] = clean_rule(body.get("recurrence"))
    return spec


# --- развёртка повторов (тестовый календарь и проверка правил) -------------
def _add_months(d: date, months: int) -> Tuple[int, int]:
    total = d.year * 12 + d.month - 1 + months
    return total // 12, total % 12 + 1


def rule_dates(rule: dict, first: date) -> Iterator[date]:
    """Даты повторов по правилу начиная с даты первой встречи, без учёта окончания."""
    step = rule.get("interval") or 1
    if rule["freq"] == "daily":
        d = first
        while True:
            yield d
            d += timedelta(days=step)
    elif rule["freq"] == "weekly":
        week = first - timedelta(days=first.weekday())
        while True:
            for wd in rule["days"]:
                d = week + timedelta(days=wd)
                if d >= first:
                    yield d
            week += timedelta(days=7 * step)
    else:
        months = step if rule["freq"] == "monthly" else 12
        k = 0
        while True:
            y, m = _add_months(first, k * months)
            if first.day <= _cal.monthrange(y, m)[1]:
                yield date(y, m, first.day)
            k += 1


def occurrences(rule: dict, start: datetime, end: datetime, win_start: datetime,
                win_end: datetime) -> List[Tuple[int, datetime, datetime]]:
    """[(номер с 1, начало, конец)] повторов, попадающих в окно. Время суток — как у первой встречи по местному."""
    tz = local_tz()
    local = start.astimezone(tz)
    duration = end - start
    until = date.fromisoformat(rule["until"]) if rule.get("end") == "date" else None
    count = rule.get("count") if rule.get("end") == "count" else None
    out = []
    for idx, d in enumerate(rule_dates(rule, local.date()), 1):
        if (count and idx > count) or (until and d > until) or idx > MAX_EXPAND:
            break
        s = datetime.combine(d, local.timetz().replace(tzinfo=None), tz)
        if s >= win_end:
            break
        if s + duration > win_start:
            out.append((idx, s, s + duration))
    return out


def first_matching(rule: dict, start: datetime) -> bool:
    """Exchange требует, чтобы первая встреча сама подходила под правило (например, день недели)."""
    return next(rule_dates(rule, start.astimezone(local_tz()).date())) == start.astimezone(local_tz()).date()


# --- цвета встреч ---------------------------------------------------------
DEFAULT_COLORS = [
    {"name": "ПСИ", "color": "#7c3aed", "words": ["ПСИ"]},
    {"name": "Встречи отдела", "color": "#dc2626", "words": ["отдел"]},
    {"name": "Авто", "color": "#2563eb", "words": [], "category": "AUTO"},
]
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_COLOR_RULES = 30


def clean_colors(raw) -> List[dict]:
    """Правила раскраски: имя (оно же категория Outlook), цвет #rrggbb и слова в теме, месте или организаторе."""
    if not isinstance(raw, list):
        raise ValueError("Цвета встреч — список правил")
    if len(raw) > MAX_COLOR_RULES:
        raise ValueError("Не больше {} правил раскраски".format(MAX_COLOR_RULES))
    out = []
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError("Правило раскраски — объект")
        name = str(r.get("name") or "").strip()[:40]
        color = str(r.get("color") or "").strip()
        words = r.get("words") or []
        if isinstance(words, str):
            words = words.split(",")
        words = [w for w in (str(x).strip()[:60] for x in words) if w][:20]
        category = str(r.get("category") or "").strip()[:60]
        if not name:
            raise ValueError("У правила раскраски нужно имя")
        if not COLOR_RE.match(color):
            raise ValueError("Цвет «{}» — в виде #rrggbb".format(name))
        out.append({"name": name, "color": color.lower(), "words": words, "category": category})
    return out


# --- занятость -----------------------------------------------------------
def common_slots(busy_by_person: dict, start_from: datetime, duration: timedelta, work_start: str, work_end: str,
                 count: int = 5, days: int = MAX_FREEBUSY_DAYS) -> List[dict]:
    """Окна, когда свободны все: объединяем занятость и ищем как для приглашений."""
    busy = [b for blocks in busy_by_person.values() for b in blocks if b.get("busy_type") not in ("Free", "NoData")]
    return find_free_slots(busy, start_from, duration, work_start, work_end, count, days)


def utc_iso(value: datetime) -> str:
    return to_iso(value if value.tzinfo else value.replace(tzinfo=timezone.utc))


__all__ = ["clean_spec", "clean_rule", "occurrences", "rule_dates", "common_slots", "first_matching",
           "parse_iso", "clean_colors", "DEFAULT_COLORS", "SCOPES", "REMINDERS", "MAX_FREEBUSY_PEOPLE", "MAX_FREEBUSY_DAYS"]
