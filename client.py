"""Клиент к server.py для использования из других проектов (только стандартная библиотека).

    from client import GigaCodeClient
    gc = GigaCodeClient("http://127.0.0.1:8765")
    print(gc.ask("Объясни, что делает этот regex: ^\\d{3}$"))

    # живая сессия со всеми событиями агента
    sid = gc.create_session(permission_policy="allow")
    gc.prompt(sid, "Напиши hello world на Go")
    for ev in gc.events(sid, last_event_id=0, until_turn_end=True):
        print(ev["type"])

Из консоли:  python client.py "вопрос"
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Iterator, List, Optional


class GigaCodeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: Optional[str] = None,
                 timeout: float = 1800):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _open(self, method: str, path: str, payload=None, headers: Optional[dict] = None):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        h.update(headers or {})
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=h)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode("utf-8"))
                msg = data.get("error", str(e))
                if data.get("stderrTail"):
                    msg = "{}\n{}".format(msg, data["stderrTail"].strip())
            except ValueError:
                msg = str(e)
            raise RuntimeError(msg) from None

    def _json(self, method: str, path: str, payload=None):
        with self._open(method, path, payload) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None

    # --- разовые запросы --------------------------------------------------
    def ask(self, prompt: str, cwd: Optional[str] = None) -> str:
        return self._json("POST", "/api/chat", {"prompt": prompt, "cwd": cwd})["response"]

    def chat(self, messages: List[dict], cwd: Optional[str] = None) -> str:
        return self._json("POST", "/api/chat", {"messages": messages, "cwd": cwd})["response"]

    def stream(self, prompt: str = "", messages: Optional[List[dict]] = None,
               cwd: Optional[str] = None) -> Iterator[str]:
        payload = {"stream": True, "cwd": cwd}
        payload.update({"messages": messages} if messages else {"prompt": prompt})
        with self._open("POST", "/api/chat", payload) as r:
            for raw in r:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                if ev["type"] == "delta":
                    yield ev["text"]
                elif ev["type"] == "error":
                    raise RuntimeError(ev["error"])

    # --- живые сессии -----------------------------------------------------
    def create_session(self, cwd: Optional[str] = None, permission_policy: str = "ask",
                       scope: str = "thread", include_directories: Optional[List[str]] = None) -> str:
        body = {"sessionScope": scope, "permissionPolicy": permission_policy}
        if cwd:
            body["cwd"] = cwd
        if include_directories:
            body["includeDirectories"] = include_directories
        return self._json("POST", "/session", body)["sessionId"]

    def prompt(self, session_id: str, text: str) -> str:
        return self._json("POST", "/session/{}/prompt".format(session_id),
                          {"prompt": [{"type": "text", "text": text}]})["promptId"]

    def set_approval_mode(self, session_id: str, mode: str) -> None:
        self._json("POST", "/session/{}/approval-mode".format(session_id), {"mode": mode})

    def set_model(self, session_id: str, model_id: str) -> None:
        self._json("POST", "/session/{}/model".format(session_id), {"modelId": model_id})

    def status(self, session_id: str) -> dict:
        return self._json("GET", "/session/{}/status".format(session_id))

    def answer_permission(self, session_id: str, request_id: str, option_id: Optional[str]) -> None:
        body = {"outcome": "selected", "optionId": option_id} if option_id else {"outcome": "cancelled"}
        self._json("POST", "/session/{}/permission/{}".format(session_id, request_id), body)

    def cancel(self, session_id: str) -> None:
        self._json("POST", "/session/{}/cancel".format(session_id))

    def close(self, session_id: str) -> None:
        self._json("DELETE", "/session/{}".format(session_id))

    def events(self, session_id: str, last_event_id: Optional[int] = None,
               until_turn_end: bool = False) -> Iterator[dict]:
        """События сессии (SSE). until_turn_end — остановиться на turn_complete / turn_error."""
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        with self._open("GET", "/session/{}/events".format(session_id), headers=headers) as r:
            data = []
            for raw in r:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    ev = json.loads("\n".join(data))
                    data = []
                    yield ev
                    if ev.get("type") in ("session_closed", "session_died"):
                        return
                    if until_turn_end and ev.get("type") in ("turn_complete", "turn_error"):
                        return


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or sys.stdin.read()
    for part in GigaCodeClient().stream(question):
        sys.stdout.write(part)
        sys.stdout.flush()
    print()
