"""Клиент протокола ACP (Agent Client Protocol) для `gigacode --acp`.

Транспорт — JSON-RPC 2.0, по одному JSON-объекту на строку через stdin/stdout процесса.
Агент шлёт уведомления session/update (мысли, ответ, вызовы инструментов, план)
и запросы к клиенту (session/request_permission, fs/*), на которые мы отвечаем.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Callable, Dict, List, Optional

from gigacode_runner import IS_WINDOWS, _kill_tree

PROTOCOL_VERSION = 1


class AcpError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error: Optional[AcpError] = None


class AcpConnection:
    """Один процесс агента и JSON-RPC поверх его stdio.

    on_notification(method, params)          — уведомления агента
    on_request(method, params) -> result     — запросы агента (выполняются в отдельном потоке,
                                               могут блокироваться, например ждать решения человека)
    on_stderr(text), on_exit(returncode)     — диагностика
    """

    def __init__(self, command: List[str], cwd: str,
                 on_notification: Callable[[str, dict], None],
                 on_request: Callable[[str, dict], object],
                 on_stderr: Optional[Callable[[str], None]] = None,
                 on_exit: Optional[Callable[[int], None]] = None):
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_stderr = on_stderr
        self.on_exit = on_exit
        self._next_id = 0
        self._pending: Dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.closed = False
        self.returncode: Optional[int] = None

        popen_kwargs = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
        try:
            self.proc = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, **popen_kwargs,
            )
        except OSError as e:
            raise AcpError("Не удалось запустить агента: {}".format(e))
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # --- отправка ---------------------------------------------------------
    def _send(self, msg: dict):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self.closed:
                raise AcpError("Агент завершён")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise AcpError("Агент недоступен: {}".format(e))

    def request(self, method: str, params: dict, timeout: Optional[float] = None):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            pending = self._pending[rid] = _Pending()
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not pending.event.wait(timeout):
                raise AcpError("Таймаут ответа на {}".format(method))
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if pending.error:
            raise pending.error
        return pending.result

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # --- приём ------------------------------------------------------------
    def _read_stdout(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                # не JSON — значит агент что-то напечатал мимо протокола; показываем как stderr
                if self.on_stderr:
                    self.on_stderr("[stdout] " + line + "\n")
                continue
            self._dispatch(msg)
        self._finish()

    def _read_stderr(self):
        for raw in self.proc.stderr:
            if self.on_stderr:
                self.on_stderr(raw.decode("utf-8", "replace"))

    def _dispatch(self, msg: dict):
        if "method" in msg and "id" in msg:
            threading.Thread(target=self._handle_request, args=(msg,), daemon=True).start()
        elif "method" in msg:
            try:
                self.on_notification(msg["method"], msg.get("params") or {})
            except Exception as e:  # noqa: BLE001 — ошибка обработчика не должна валить чтение
                if self.on_stderr:
                    self.on_stderr("[client] ошибка обработки {}: {!r}\n".format(msg["method"], e))
        elif "id" in msg:
            with self._lock:
                pending = self._pending.get(msg["id"])
            if pending is None:
                return
            if "error" in msg:
                err = msg["error"] or {}
                pending.error = AcpError(err.get("message", "Ошибка агента"), err.get("code"), err.get("data"))
            else:
                pending.result = msg.get("result")
            pending.event.set()

    def _handle_request(self, msg: dict):
        try:
            result = self.on_request(msg["method"], msg.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except AcpError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": e.code or -32603, "message": str(e)}}
        except Exception as e:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": repr(e)}}
        try:
            self._send(reply)
        except AcpError:
            pass

    def _finish(self):
        self.returncode = self.proc.wait()
        with self._write_lock:
            self.closed = True
        with self._lock:
            pending = list(self._pending.values())
        for p in pending:
            p.error = AcpError("Агент завершился с кодом {}".format(self.returncode))
            p.event.set()
        if self.on_exit:
            self.on_exit(self.returncode)

    def close(self):
        with self._write_lock:
            self.closed = True
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _kill_tree(self.proc)
