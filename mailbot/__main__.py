"""Запуск из корня репозитория:  python3 -m mailbot"""
from __future__ import annotations

import argparse
import os
import sys
from http.server import ThreadingHTTPServer


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="GigaMail: почта и календарь Exchange с ИИ-инструментами GigaCode")
    ap.add_argument("--host", default=env("MAILBOT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("MAILBOT_PORT", "8770")))
    ap.add_argument("--data-dir", default=env("MAILBOT_DATA", "./mail_data"),
                    help="каталог для базы писем и зашифрованных секретов")
    ap.add_argument("--fake-mail", action="store_true", help="тестовые письма вместо Exchange")
    args = ap.parse_args()

    try:
        from .app import Context, Handler
        from .db import Database
        from .mail_source import FakeSource
        from .vault import Vault
        from .worker import Worker
    except ImportError as e:
        raise SystemExit("Не хватает зависимости ({}). Установите: pip install -r mailbot/requirements.txt".format(e))

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("ВНИМАНИЕ: сервис работает по HTTP без шифрования. Пароль и письма пойдут по сети открыто.",
              file=sys.stderr, flush=True)

    os.makedirs(args.data_dir, mode=0o700, exist_ok=True)
    db = Database(os.path.join(args.data_dir, "mail.db"))
    vault = Vault(os.path.join(args.data_dir, "vault.json"))
    fake = FakeSource() if args.fake_mail else None
    worker = Worker(db, vault, (lambda: fake) if fake else None)
    Handler.ctx = Context(db, vault, worker)
    worker.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("GigaMail: http://{}:{}  (данные: {})".format(args.host, args.port, os.path.abspath(args.data_dir)), flush=True)
    print("Почта не опрашивается, пока вы не войдёте: ключ шифрования живёт только в памяти.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен", flush=True)


if __name__ == "__main__":
    main()
