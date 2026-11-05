"""Источники писем: Exchange через EWS и тестовый генератор."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import List, Optional, Sequence


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


class ExchangeSource:
    """Читает Inbox по EWS. Состояние «прочитано» не используется: письмо, открытое
    человеком в Outlook, тоже будет обработано. Дубли отсекает уникальный Message-ID в БД."""

    FIELDS = ("message_id", "subject", "sender", "to_recipients", "datetime_received", "text_body", "body",
              "has_attachments")

    def __init__(self, server: str, email: str, username: str, password: str, auth: str = "NTLM",
                 ca_bundle: str = ""):
        from exchangelib import BASIC, DELEGATE, NTLM, Account, Configuration, Credentials
        from exchangelib.protocol import BaseProtocol

        if not (server and email and username and password):
            raise MailError("Не заполнены сервер, адрес, логин или пароль Exchange")
        if ca_bundle:
            BaseProtocol.HTTP_ADAPTER_CLS = _ca_adapter(ca_bundle)
        auth_type = {"NTLM": NTLM, "BASIC": BASIC}.get(auth.upper())
        if auth_type is None:
            raise MailError("Неизвестный тип авторизации: {}".format(auth))
        config = Configuration(server=server, credentials=Credentials(username, password), auth_type=auth_type)
        self.account = Account(primary_smtp_address=email, config=config, autodiscover=False, access_type=DELEGATE)

    def fetch(self, since: datetime, limit: int = 100) -> List[dict]:
        from exchangelib import EWSDateTime
        from exchangelib.items import Message

        query = (self.account.inbox.filter(datetime_received__gte=EWSDateTime.from_datetime(since))
                 .only(*self.FIELDS).order_by("datetime_received"))
        out = []
        for item in query[:limit]:
            if isinstance(item, Message):  # в Inbox бывают приглашения и отчёты, у них другие поля
                out.append(self._to_dict(item))
        return out

    def test(self) -> str:
        return "Подключено. Писем во входящих: {}".format(self.account.inbox.total_count)

    @staticmethod
    def _to_dict(item) -> dict:
        sender = item.sender
        recipients = ", ".join(r.email_address or r.name or "" for r in (item.to_recipients or []))
        body = item.text_body or html_to_text(str(item.body or ""))
        return {
            "message_id": item.message_id or item.id,
            "subject": item.subject or "",
            "sender": (sender.email_address if sender else "") or "",
            "sender_name": (sender.name if sender else "") or "",
            "recipients": recipients,
            # через timestamp: EWSDateTime.astimezone принимает только EWSTimeZone
            "received_at": datetime.fromtimestamp(item.datetime_received.timestamp(), timezone.utc)
            .isoformat(timespec="seconds"),
            "body": body,
            "has_attachments": bool(item.has_attachments),
        }


def _ca_adapter(path: str):
    """Корпоративный корневой сертификат для requests внутри exchangelib."""
    import requests.adapters

    class RootCAAdapter(requests.adapters.HTTPAdapter):
        def cert_verify(self, conn, url, verify, cert):
            super().cert_verify(conn=conn, url=url, verify=path, cert=cert)

    return RootCAAdapter


class FakeSource:
    """Тестовые письма для проверки без Exchange: по одному новому на каждый опрос, пока не кончатся."""

    SAMPLES = [
        ("release.manager@example.com", "Релиз ABC-123 готов к приёмке", "Коллеги, просьба проверить релиз ABC-123 до пятницы."),
        ("noreply@jira.example.ru", "[JIRA] Задача обновлена", "Статус задачи изменён автоматически."),
        ("promo@shop.example.com", "Скидки 90% только сегодня!", "Купите прямо сейчас, предложение ограничено."),
        ("colleague@example.com", "Перенос встречи", "Встреча по проекту переносится на четверг."),
    ]

    def __init__(self):
        self.sent = 0

    def fetch(self, since: datetime, limit: int = 100) -> List[dict]:
        if self.sent >= len(self.SAMPLES):
            return []
        sender, subject, body = self.SAMPLES[self.sent]
        self.sent += 1
        return [{"message_id": "<fake-{}@test>".format(self.sent), "subject": subject, "sender": sender,
                 "sender_name": sender.split("@")[0], "recipients": "me@example.com",
                 "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": body,
                 "has_attachments": False}]

    def test(self) -> str:
        return "Тестовый источник работает"


def fetch_since(watermark: Optional[str], initial_days: int) -> datetime:
    """С какого момента запрашивать письма. Перекрытие 5 минут ловит письма с запоздалой доставкой."""
    if watermark:
        return datetime.fromisoformat(watermark) - timedelta(minutes=5)
    return datetime.now(timezone.utc) - timedelta(days=initial_days)
