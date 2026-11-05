"""HTTP-сервер поверх gigacode: живые сессии агента (как `qwen serve`), веб-интерфейс и API.

Запуск:  python server.py --port 8765 --workdir ./workspace

Живые сессии (`gigacode --acp`, маршруты как у qwen serve):
  POST   /session                              создать / подключиться   {cwd?, sessionScope?, permissionPolicy?,
                                               includeDirectories?}
  GET    /session                              список сессий (расширение)
  GET    /session/{id}/status                  состояние
  GET    /session/{id}/events                  SSE-поток событий (Last-Event-ID для дочитки)
  POST   /session/{id}/prompt                  {prompt: [{type: "text", text}]} -> 202 {promptId}
  POST   /session/{id}/cancel                  отменить текущий запрос
  GET    /session/{id}/pending-prompts         очередь запросов
  POST   /session/{id}/permission/{requestId}  {outcome: "selected", optionId} | {outcome: "cancelled"}
  POST   /permission/{requestId}               то же, без id сессии
  POST   /session/{id}/model                   {modelId}
  POST   /session/{id}/approval-mode           {mode}
  PATCH  /session/{id}/metadata                {displayName}
  POST   /session/{id}/heartbeat
  GET    /session/{id}/transcript              ?cursor=&limit=
  GET    /session/{id}/export                  ?format=md|json
  DELETE /session/{id}                         закрыть
  GET    /health, /capabilities

Разовые запросы (каждый тоже идёт через живую сессию и виден в интерфейсе):
  POST /api/chat              {"prompt": "..."} или {"messages": [...]}, опц. "stream", "cwd"
  POST /v1/chat/completions   OpenAI-совместимый
  GET  /v1/models
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import select
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from gigacode_runner import GigaCode, GigaCodeError, build_prompt
from sessions import PERMISSION_POLICIES, ApiError, SessionManager, run_once

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MODEL_NAME = "gigacode"
MAX_BODY = 10 * 1024 * 1024
HEARTBEAT = 3.0  # с; как часто проверять клиента, пока агент молчит
SSE_KEEPALIVE = 15.0
TERMINAL_EVENTS = ("session_closed", "session_died")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class App:
    def __init__(self, runner: GigaCode, manager: SessionManager, token: Optional[str], max_parallel: int,
                 api_backend: str, api_permission: str):
        self.runner = runner
        self.manager = manager
        self.token = token
        self.api_backend = api_backend
        self.api_permission = api_permission
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.max_parallel = max_parallel
        self.active = 0
        self.lock = threading.Lock()
        self.started = int(time.time() * 1000)


ROUTES = []


def route(method: str, pattern: str):
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "gigacode-serve/2.0"
    protocol_version = "HTTP/1.1"
    app: App  # выставляется в main()

    # --- утилиты ----------------------------------------------------------
    def log_message(self, fmt, *args):
        print("[{}] {} {}".format(time.strftime("%H:%M:%S"), self.address_string(), fmt % args), flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, X-Qwen-Client-Id, Last-Event-ID, X-Qwen-Event-Epoch")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Retry-After, X-Qwen-Event-Epoch, X-Qwen-SSE-Stream-Id")

    def _json(self, status: int, obj, headers: Optional[dict] = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int = 204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _error(self, status: int, message: str, openai_style: bool = False, **extra):
        if openai_style:
            self._json(status, {"error": {"message": message, "type": "gigacode_error", **extra}})
        else:
            self._json(status, {"error": message, **extra})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Слишком большой запрос", "payload_too_large")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON in request body", "invalid_json")
        if not isinstance(data, dict):
            raise ApiError(400, "Ожидался JSON-объект", "invalid_json")
        return data

    def _authorized(self) -> bool:
        if not self.app.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth[:7].lower() == "bearer " and auth[7:].strip() == self.app.token

    def _client_id(self) -> Optional[str]:
        cid = self.headers.get("X-Qwen-Client-Id")
        return cid if cid and CLIENT_ID_RE.match(cid) else None

    def _client_gone(self) -> bool:
        """True, если клиент закрыл соединение (сокет читаем и отдаёт EOF)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _check_client(self):
        if self._client_gone():
            raise BrokenPipeError("client disconnected")

    def _start_sse(self, headers: Optional[dict] = None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.close_connection = True

    def _sse(self, payload, event: Optional[str] = None, eid: Optional[int] = None):
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        frame = ""
        if eid is not None:
            frame += "id: {}\n".format(eid)
        if event:
            frame += "event: {}\n".format(event)
        frame += "data: {}\n\n".format(data)
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _ping(self):
        # SSE-комментарий: клиенты его игнорируют, а прокси не рвут «молчащее» соединение
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()
        self._check_client()

    # --- диспетчер --------------------------------------------------------
    def do_OPTIONS(self):
        self._empty(204)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        parts = urlsplit(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        path = parts.path.rstrip("/") or "/"
        openai_style = path.startswith("/v1/")
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if m == method and match:
                break
        else:
            return self._error(404, "Not found", openai_style)
        public = path in ("/", "/index.html", "/health")
        if not public and not self._authorized():
            return self._error(401, "Unauthorized", openai_style)
        try:
            fn(self, **match.groupdict())
        except ApiError as e:
            if openai_style:
                self._error(e.status, str(e), True, code=e.code, **e.extra)
            else:
                headers = {"Retry-After": "5"} if e.status == 503 else None
                self._json(e.status, e.body(), headers)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("клиент отключился")

    # === страницы и служебное ============================================
    @route("GET", "/")
    def index(self):
        try:
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                body = f.read()
        except OSError:
            return self._error(404, "Not found")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @route("GET", "/health")
    def health(self):
        if "deep" not in self.query:
            return self._json(200, {"status": "ok"})
        m = self.app.manager
        return self._json(200, dict(status="ok", workspaceCount=1, cliCallsActive=self.app.active,
                                    workdir=m.default_cwd, command=m.base_command,
                                    **m.counters()))

    @route("GET", "/capabilities")
    def capabilities(self):
        m = self.app.manager
        return self._json(200, {
            "v": 1, "protocolVersions": {"current": "v1", "supported": ["v1"]}, "mode": "gigacode-serve",
            "features": ["session_create", "session_list", "session_prompt", "session_events", "session_cancel",
                         "session_set_model", "session_permission_vote", "permission_mediation",
                         "session_approval_mode_control", "client_heartbeat", "session_close",
                         "session_metadata", "session_transcript", "session_export", "pending_prompts",
                         "openai_chat_completions"],
            "limits": {"maxSessions": m.max_sessions, "maxPendingPromptsPerSession": m.max_pending_prompts,
                       "eventRingSize": m.event_ring_size, "promptDeadlineMs": int(m.prompt_deadline * 1000)},
            "modelServices": [], "workspaceCwd": m.default_cwd,
            "workspaces": [{"id": "primary", "cwd": m.default_cwd, "primary": True, "trusted": True}],
            "approvalMode": m.approval_mode, "apiBackend": self.app.api_backend,
            "apiPermissionPolicy": self.app.api_permission, "permissionPolicies": list(PERMISSION_POLICIES),
        })

    # === живые сессии ====================================================
    @route("POST", "/session")
    def create_session(self):
        body = self._read_json()
        include_dirs = body.get("includeDirectories") or []
        if not isinstance(include_dirs, list) or not all(isinstance(d, str) for d in include_dirs):
            raise ApiError(400, "includeDirectories: список путей", "invalid_include_directory")
        s, attached = self.app.manager.create(
            body.get("cwd"), scope=body.get("sessionScope", "single"), title=body.get("displayName", ""),
            permission_policy=body.get("permissionPolicy", "ask"), include_dirs=include_dirs)
        if self._client_id():
            s.client_seen[self._client_id()] = int(time.time() * 1000)
        self._json(200, {"sessionId": s.id, "workspaceCwd": s.cwd, "attached": attached,
                         "clientId": self._client_id(), "createdAt": s.created, "modes": s.modes,
                         "models": s.models, "agentInfo": s.agent_info})

    @route("GET", "/session")
    def list_sessions(self):
        self._json(200, {"sessions": self.app.manager.list()})

    @route("GET", "/session/{sid}/status")
    def session_status(self, sid):
        self._json(200, self.app.manager.get(sid).status())

    @route("DELETE", "/session/{sid}")
    def close_session(self, sid):
        self.app.manager.get(sid).close(reason="client_request")
        self._empty(204)

    @route("POST", "/session/{sid}/prompt")
    def submit_prompt(self, sid):
        body = self._read_json()
        prompt = body.get("prompt")
        if isinstance(prompt, str):  # удобство: разрешаем строку
            prompt = [{"type": "text", "text": prompt}]
        if not isinstance(prompt, list) or not prompt:
            raise ApiError(400, "prompt: нужен массив блоков [{type: \"text\", text}]", "invalid_prompt")
        s = self.app.manager.get(sid)
        res = s.submit_prompt(prompt, source=body.get("source") or "api", client_id=self._client_id())
        self._json(202, res)

    @route("POST", "/session/{sid}/cancel")
    def cancel(self, sid):
        self.app.manager.get(sid).cancel()
        self._empty(204)

    @route("GET", "/session/{sid}/pending-prompts")
    def pending_prompts(self, sid):
        self._json(200, {"pendingPrompts": self.app.manager.get(sid).pending_prompts()})

    @route("POST", "/session/{sid}/permission/{rid}")
    def vote(self, sid, rid):
        self.app.manager.get(sid).vote(rid, self._read_json(), self._client_id())
        self._json(200, {})

    @route("POST", "/permission/{rid}")
    def vote_legacy(self, rid):
        self.app.manager.find_permission(rid).vote(rid, self._read_json(), self._client_id())
        self._json(200, {})

    @route("POST", "/session/{sid}/model")
    def set_model(self, sid):
        model = (self._read_json().get("modelId") or "").strip()
        if not model:
            raise ApiError(400, "modelId обязателен", "invalid_model_id")
        self.app.manager.get(sid).set_model(model)
        self._json(200, {})

    @route("POST", "/session/{sid}/approval-mode")
    def set_mode(self, sid):
        body = self._read_json()
        mode = body.get("mode") or body.get("approvalMode") or body.get("modeId")
        if not mode:
            raise ApiError(400, "mode обязателен", "invalid_approval_mode")
        self.app.manager.get(sid).set_approval_mode(mode)
        self._json(200, {})

    @route("PATCH", "/session/{sid}/metadata")
    def metadata(self, sid):
        s = self.app.manager.get(sid)
        name = self._read_json().get("displayName")
        if name is not None:
            if not isinstance(name, str) or len(name) > 256 or re.search(r"[\x00-\x1f\x7f]", name):
                raise ApiError(400, "displayName: строка до 256 символов без управляющих", "invalid_metadata")
            s.display_name = name
            s.emit("session_metadata_updated", displayName=name)
        self._json(200, {"sessionId": s.id, "displayName": s.display_name})

    @route("POST", "/session/{sid}/heartbeat")
    def heartbeat(self, sid):
        s = self.app.manager.get(sid)
        now = int(time.time() * 1000)
        if self._client_id():
            s.client_seen[self._client_id()] = now
        self._json(200, {"sessionId": s.id, "clientId": self._client_id(), "lastSeenAt": now})

    @route("GET", "/session/{sid}/transcript")
    def transcript(self, sid):
        try:
            cursor = int(self.query.get("cursor", 0))
            limit = max(1, min(int(self.query.get("limit", 100)), 500))
        except ValueError:
            raise ApiError(400, "cursor и limit — целые числа", "invalid_query")
        self._json(200, self.app.manager.get(sid).transcript(cursor, limit))

    @route("GET", "/session/{sid}/export")
    def export(self, sid):
        s = self.app.manager.get(sid)
        fmt = self.query.get("format", "md")
        if fmt == "json":
            body = json.dumps(s.transcript(0, 10 ** 9)["events"], ensure_ascii=False, indent=1).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif fmt == "md":
            body, ctype = s.export_markdown().encode("utf-8"), "text/markdown; charset=utf-8"
        else:
            raise ApiError(400, "format: md или json", "invalid_format")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", 'attachment; filename="session-{}.{}"'.format(s.id[:8], fmt))
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    @route("GET", "/session/{sid}/events")
    def events(self, sid):
        s = self.app.manager.get(sid)
        raw = self.headers.get("Last-Event-ID") or self.query.get("lastEventId")
        try:
            after = int(raw) if raw is not None else None
        except ValueError:
            after = None  # как в qwen serve: кривой Last-Event-ID — только живые события
        q, replay, lost = s.subscribe(after)
        stream_id = str(uuid.uuid4())
        try:
            self._start_sse({"X-Qwen-SSE-Stream-Id": stream_id})
            self.wfile.write(b"retry: 3000\n\n")
            if lost:
                self._sse(dict(type="state_resync_required", data=lost), "state_resync_required")
            last = after or 0
            terminal = False
            for ev in replay:
                self._sse(ev, ev["type"], ev["id"])
                last = ev["id"]
                terminal = terminal or ev["type"] in TERMINAL_EVENTS
            if after is not None:
                self._sse({"type": "replay_complete",
                           "data": {"replayedCount": len(replay), "lastReplayedEventId": last}},
                          "replay_complete")
            if terminal or s.state in ("closed", "died") and not q.qsize():
                return
            while True:
                try:
                    ev = q.get(timeout=SSE_KEEPALIVE)
                except queue.Empty:
                    self._ping()
                    continue
                if ev["id"] <= last:
                    continue
                self._sse(ev, ev["type"], ev["id"])
                last = ev["id"]
                if ev["type"] in TERMINAL_EVENTS:
                    return
        finally:
            s.unsubscribe(q)

    # === разовые запросы =================================================
    def _one_shot(self, prompt: str, cwd: Optional[str], source: str, on_delta, streaming: bool) -> dict:
        """Выполняет запрос выбранным бэкендом. Возвращает {"text", "sessionId"?, "elapsedMs"}."""
        started = time.time()
        on_idle = self._ping if streaming else self._check_client
        if self.app.api_backend == "acp":
            return run_once(self.app.manager, prompt, cwd, source=source, policy=self.app.api_permission,
                            on_delta=on_delta, on_idle=on_idle, idle_every=HEARTBEAT)
        parts = []

        def collect(t):
            parts.append(t)
            on_delta(t)
        try:
            self._run_cli(prompt, cwd, collect, on_idle)
        except GigaCodeError as e:
            raise ApiError(502, str(e), "cli_error")
        return {"text": "".join(parts).strip(), "elapsedMs": int((time.time() - started) * 1000)}

    def _run_cli(self, prompt: str, cwd: Optional[str], on_chunk, on_idle):
        """Старый режим: один вызов `gigacode -p` на запрос (--api-backend cli)."""
        if not self.app.slots.acquire(timeout=self.app.runner.timeout):
            raise GigaCodeError("Сервер занят: все {} слотов заняты".format(self.app.max_parallel))
        with self.app.lock:
            self.app.active += 1
        cancel = threading.Event()
        q: "queue.Queue" = queue.Queue()

        def pump():
            try:
                for chunk in self.app.runner.stream(prompt, cwd=cwd, cancel=cancel):
                    q.put(("chunk", chunk))
                q.put(("end", None))
            except GigaCodeError as e:
                q.put(("error", e))
            except Exception as e:  # noqa: BLE001 — не теряем неожиданные ошибки потока
                q.put(("error", GigaCodeError("Внутренняя ошибка: {!r}".format(e))))

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                try:
                    kind, value = q.get(timeout=HEARTBEAT)
                except queue.Empty:
                    on_idle()
                    continue
                if kind == "chunk":
                    on_chunk(value)
                elif kind == "error":
                    raise value
                else:
                    return
        except (BrokenPipeError, ConnectionResetError):
            cancel.set()
            raise
        finally:
            with self.app.lock:
                self.app.active -= 1
            self.app.slots.release()

    def _prompt_from(self, data) -> str:
        if data.get("messages"):
            try:
                return build_prompt(data["messages"], self.app.runner.max_prompt_chars)
            except GigaCodeError as e:
                raise ApiError(400, str(e), "invalid_prompt")
        prompt = str(data.get("prompt") or "")
        if not prompt.strip():
            raise ApiError(400, "Нужно поле 'prompt' или 'messages'", "invalid_prompt")
        return prompt

    @route("POST", "/api/chat")
    def api_chat(self):
        data = self._read_json()
        prompt = self._prompt_from(data)
        cwd = data.get("cwd") or None
        source = data.get("source") or "api"
        if not data.get("stream"):
            res = self._one_shot(prompt, cwd, source, lambda t: None, streaming=False)
            return self._json(200, {"response": res["text"], "sessionId": res.get("sessionId"),
                                    "stopReason": res.get("stopReason"),
                                    "elapsed": round((res.get("elapsedMs") or 0) / 1000, 2)})
        self._start_sse()
        try:
            res = self._one_shot(prompt, cwd, source, lambda t: self._sse({"type": "delta", "text": t}),
                                 streaming=True)
            self._sse({"type": "done", "sessionId": res.get("sessionId"), "stopReason": res.get("stopReason"),
                       "elapsed": round((res.get("elapsedMs") or 0) / 1000, 2)})
        except ApiError as e:
            self._sse({"type": "error", **e.body()})

    @route("GET", "/v1/models")
    def models(self):
        self._json(200, {"object": "list", "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "local"}]})

    @route("POST", "/v1/chat/completions")
    def openai_chat(self):
        data = self._read_json()
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ApiError(400, "Поле 'messages' обязательно", "invalid_messages")
        prompt = self._prompt_from({"messages": messages})
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        model = data.get("model") or MODEL_NAME
        cwd = data.get("cwd") or None

        if not data.get("stream"):
            res = self._one_shot(prompt, cwd, "openai", lambda t: None, streaming=False)
            return self._json(200, {
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": res["text"]},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "gigacode_session_id": res.get("sessionId"),
            })

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        self._start_sse()
        self._sse(chunk({"role": "assistant", "content": ""}))
        try:
            self._one_shot(prompt, cwd, "openai", lambda t: self._sse(chunk({"content": t})), streaming=True)
            self._sse(chunk({}, "stop"))
        except ApiError as e:
            self._sse({"error": {"message": str(e), "type": "gigacode_error", "code": e.code, **e.extra}})
        self._sse("[DONE]")


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Живые сессии, веб-интерфейс и API для gigacode")
    ap.add_argument("--host", default=env("GIGACODE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("GIGACODE_PORT", "8765")))
    ap.add_argument("--workdir", default=env("GIGACODE_WORKDIR", "./workspace"),
                    help="каталог по умолчанию, где gigacode работает и может править файлы")
    ap.add_argument("--cmd", default=None, help="команда запуска CLI (по умолчанию gigacode из PATH)")
    ap.add_argument("--approval-mode", default=env("GIGACODE_APPROVAL_MODE", "auto-edit"),
                    choices=["plan", "default", "auto-edit"])
    ap.add_argument("--api-backend", default=env("GIGACODE_API_BACKEND", "acp"), choices=["acp", "cli"],
                    help="как выполнять /api/chat и /v1: acp — через живую сессию (видно в UI), "
                         "cli — старый вызов gigacode -p")
    ap.add_argument("--api-permission", default=env("GIGACODE_API_PERMISSION", "reject"),
                    choices=PERMISSION_POLICIES,
                    help="что делать с запросами разрешений в /api/chat и /v1: reject (как в -p), "
                         "allow, ask (ждать ответа в веб-интерфейсе)")
    ap.add_argument("--timeout", type=float, default=float(env("GIGACODE_TIMEOUT", "1800")),
                    help="предел одного запроса, с")
    ap.add_argument("--permission-timeout", type=float, default=float(env("GIGACODE_PERMISSION_TIMEOUT", "1800")),
                    help="сколько ждать ответа на запрос разрешения, с; потом — отказ")
    ap.add_argument("--max-sessions", type=int, default=int(env("GIGACODE_MAX_SESSIONS", "16")))
    ap.add_argument("--max-pending-prompts", type=int, default=int(env("GIGACODE_MAX_PENDING", "5")))
    ap.add_argument("--idle-timeout", type=float, default=float(env("GIGACODE_IDLE_TIMEOUT", "1800")),
                    help="закрывать простаивающие сессии без подписчиков через N секунд")
    ap.add_argument("--max-parallel", type=int, default=int(env("GIGACODE_MAX_PARALLEL", "2")),
                    help="только для --api-backend cli")
    ap.add_argument("--token", default=env("GIGACODE_TOKEN"),
                    help="если задан, API требует заголовок Authorization: Bearer <token>")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print("ВНИМАНИЕ: сервер открыт в сеть без --token — любой в сети сможет управлять агентом.", flush=True)

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    try:
        runner = GigaCode(cmd=args.cmd, workdir=workdir, timeout=args.timeout,
                          approval_mode=args.approval_mode)
    except GigaCodeError as e:
        raise SystemExit(str(e))
    manager = SessionManager(runner._base, workdir, approval_mode=args.approval_mode,
                             prompt_deadline=args.timeout, permission_timeout=args.permission_timeout,
                             max_sessions=args.max_sessions, max_pending_prompts=args.max_pending_prompts,
                             idle_ttl=args.idle_timeout)

    Handler.app = App(runner, manager, args.token, max(1, args.max_parallel), args.api_backend,
                      args.api_permission)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("gigacode serve: http://{}:{}  (workdir: {})".format(args.host, args.port, workdir), flush=True)
    print("агент: {} --acp --approval-mode {}".format(" ".join(runner._base), args.approval_mode), flush=True)
    print("/api/chat и /v1: {} (разрешения: {})".format(args.api_backend, args.api_permission), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановка…", flush=True)
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
