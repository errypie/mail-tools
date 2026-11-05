"""Статус внешних сервисов и понятные подсказки к их ошибкам."""
from __future__ import annotations

import re
import time
from typing import Optional

HINTS = {
    "outlook": [
        (r"Unauthorized|\b401\b|credentials|Wrong username", "Exchange отклонил вход. Проверьте логин и пароль, попробуйте формат DOMAIN\\user или user@example.com."),
        (r"CERTIFICATE_VERIFY_FAILED|self.signed certificate|certificate verify", "Не проверен сертификат сервера. Укажите корневой сертификат в настройках."),
        (r"SSLError|SSLEOFError|EOF occurred", "Защищённое соединение оборвалось. Проверьте VPN, прокси и адрес сервера."),
        (r"Name or service not known|nodename nor servname|getaddrinfo|NameResolution|Temporary failure in name", "Имя сервера не находится. Проверьте адрес сервера и подключение к корпоративной сети или VPN."),
        (r"timed out|Timeout|ConnectionError|Connection refused|Max retries|TransportError|No valid version", "Сервер не отвечает. Проверьте VPN, адрес сервера и доступ к EWS."),
        (r"ErrorNonExistentMailbox|ErrorInvalidSmtpAddress|ErrorAccessDenied|ErrorImpersonat", "Проверьте адрес ящика и права этой учётной записи на него."),
        (r"Throttl|ErrorServerBusy|\b429\b", "Exchange ограничивает частоту запросов. Увеличьте интервал опроса."),
    ],
    "gigacode": [
        (r"Connection refused|Errno 61|Errno 111|WinError 10061", "Сервис GigaCode не запущен или адрес указан неверно. Запустите server.py."),
        (r"Unauthorized", "Сервис требует токен. Укажите его в настройках."),
        (r"Агент не запустился|init_failed|Агент завершился|Процесс агента", "Сервис работает, но gigacode --acp не стартует. Посмотрите текст ошибки и проверьте вход в сам gigacode."),
        (r"timed out|Таймаут", "GigaCode не успел ответить. Посмотрите активные сессии и загрузку сервиса."),
        (r"нет JSON|Некорректный JSON", "Модель ответила не в формате JSON. Проверьте промпт классификации."),
        (r"лимит живых сессий|session_limit", "Закончились слоты сессий. Закройте лишние сессии или увеличьте --max-sessions."),
    ],
}


def hint_for(source: str, message: Optional[str]) -> Optional[str]:
    for pattern, hint in HINTS[source] if message else ():
        if re.search(pattern, message, re.IGNORECASE):
            return hint
    return None


def timed(fn) -> tuple:
    started = time.time()
    result = fn()
    return result, int((time.time() - started) * 1000)
