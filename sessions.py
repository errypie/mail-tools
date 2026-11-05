"""Живые сессии агента поверх `gigacode --acp` (модель событий совместима с `qwen serve`).

Каждая сессия — отдельный процесс агента. Всё, что он делает (мысли, текст ответа,
вызовы инструментов, план, запросы разрешений, stderr), складывается в кольцевой журнал
событий с номерами. Подписчики (SSE) получают события вживую и при переподключении
дочитывают пропущенное по Last-Event-ID.

Конверт события: {"id": N, "v": 1, "type": "...", "data": {...}, "_meta": {"serverTimestamp": ms}}
"""
from __future__ import annotations

import collections
import os
import queue
import threading
import time
import uuid
from typing import Callable, Deque, Dict, List, Optional, Tuple

from acp_client import PROTOCOL_VERSION, AcpConnection, AcpError

PERMISSION_POLICIES = ("ask", "allow", "reject")


def _ms() -> int:
    return int(time.time() * 1000)


class ApiError(Exception):
    """Ошибка для HTTP-ответа: статус, текст и машинный код в стиле qwen serve."""

    def __init__(self, status: int, message: str, code: Optional[str] = None, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra

    def body(self) -> dict:
        b = {"error": str(self)}
        if self.code:
            b["code"] = self.code
        b.update(self.extra)
        return b


class Session:
    def __init__(self, manager: "SessionManager", cwd: str, *, title: str = "", kind: str = "interactive",
                 permission_policy: str = "ask", include_dirs: Optional[List[str]] = None):
        self.manager = manager
        self.id = str(uuid.uuid4())
        self.acp_session_id: Optional[str] = None
        self.cwd = cwd
        self.include_dirs = include_dirs or []
        self.kind = kind  # interactive — живая сессия; api — разовый вызов из /api/chat или /v1
        self.display_name = title
        self.created = _ms()
        self.updated = self.created
        self.state = "starting"  # starting | idle | running | closed | died
        self.error: Optional[str] = None
        self.permission_policy = permission_policy
        self.modes: Optional[dict] = None
        self.models: Optional[dict] = None
        self.agent_info: Optional[dict] = None
        self.usage: Optional[dict] = None
        self.client_seen: Dict[str, int] = {}

        self._ring: Deque[dict] = collections.deque(maxlen=manager.event_ring_size)
        self._seq = 0
        self._lock = threading.RLock()
        self._subscribers: List[queue.Queue] = []
        self._permissions: Dict[str, dict] = {}
        self._prompts: Deque[dict] = collections.deque()  # очередь FIFO, первый — выполняется
        self._wake = threading.Condition(self._lock)
        self._running: Optional[dict] = None
        self._stderr_tail: Deque[str] = collections.deque(maxlen=50)
        self.conn: Optional[AcpConnection] = None

    # --- события ----------------------------------------------------------
    def emit(self, type_: str, originator: Optional[str] = None, **data) -> dict:
        with self._lock:
            self._seq += 1
            env = {"id": self._seq, "v": 1, "type": type_,
                   "data": dict(sessionId=self.id, **data), "_meta": {"serverTimestamp": _ms()}}
            if originator:
                env["originatorClientId"] = originator
            self._ring.append(env)
            self.updated = env["_meta"]["serverTimestamp"]
            subs = list(self._subscribers)
        for q in subs:
            q.put(env)
        return env

    @property
    def last_event_id(self) -> int:
        return self._seq

    def subscribe(self, after_id: Optional[int]) -> Tuple[queue.Queue, List[dict], Optional[dict]]:
        """Подписка + события для дочитки (id > after_id). Третий элемент — признак потери истории."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
            if after_id is None:
                return q, [], None
            replay = [e for e in self._ring if e["id"] > after_id]
            earliest = self._ring[0]["id"] if self._ring else self._seq + 1
            lost = None
            if after_id + 1 < earliest:
                lost = {"reason": "ring_evicted", "lastDeliveredId": after_id, "earliestAvailableId": earliest}
            return q, replay, lost

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def transcript(self, cursor: int = 0, limit: int = 100) -> dict:
        with self._lock:
            events = [e for e in self._ring if e["id"] > cursor][:limit]
            has_more = bool(events) and events[-1]["id"] < self._seq
        page = {"v": 1, "sessionId": self.id, "events": events, "hasMore": has_more}
        if has_more:
            page["nextCursor"] = events[-1]["id"]
        return page

    def _set_state(self, state: str):
        self.state = state
        self.emit("session_state", state=state)

    # --- жизненный цикл ---------------------------------------------------
    def start(self):
        m = self.manager
        cmd = m.base_command + ["--acp", "--approval-mode", m.approval_mode]
        for path in self.include_dirs:
            cmd += ["--include-directories", path]
        self.emit("agent_process", command=cmd, cwd=self.cwd)
        try:
            self.conn = AcpConnection(cmd, self.cwd, on_notification=self._on_notification,
                                      on_request=self._on_request, on_stderr=self._on_stderr,
                                      on_exit=self._on_exit)
            init = self.conn.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                # файлы и терминал агент обслуживает сам; мы наблюдаем и подтверждаем
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                       "terminal": False},
            }, timeout=m.init_timeout) or {}
            self.agent_info = init.get("agentInfo")
            self.emit("agent_initialized", result=init)
            res = self._new_session(init)
        except AcpError as e:
            self.error = str(e)
            self.emit("session_died", reason="init_failed", error=self.error,
                      stderrTail="".join(self._stderr_tail)[-4000:])
            self.state = "died"
            if self.conn:
                self.conn.close()
            raise ApiError(504 if "Таймаут" in str(e) else 502, "Агент не запустился: {}".format(e),
                           "init_failed", stderrTail="".join(self._stderr_tail)[-4000:])
        self.acp_session_id = res.get("sessionId")
        self.modes = res.get("modes")
        self.models = res.get("models")
        self.emit("session_created", acpSessionId=self.acp_session_id, modes=self.modes,
                  models=self.models, configOptions=res.get("configOptions"))
        self._set_state("idle")
        threading.Thread(target=self._prompt_worker, daemon=True).start()

    def _new_session(self, init: dict) -> dict:
        params = {"cwd": self.cwd, "mcpServers": []}
        t = self.manager.init_timeout
        try:
            return self.conn.request("session/new", params, timeout=t) or {}
        except AcpError as e:
            methods = init.get("authMethods") or []
            if not methods:
                raise
            # агент мог потребовать аутентификацию — пробуем метод, который он сам предложил
            self.emit("log", text="session/new: {} — пробую authenticate({})".format(e, methods[0].get("id")))
            self.conn.request("authenticate", {"methodId": methods[0].get("id")}, timeout=t)
            return self.conn.request("session/new", params, timeout=t) or {}

    def _on_stderr(self, text: str):
        self._stderr_tail.append(text)
        self.emit("agent_stderr", text=text)

    def _on_exit(self, code: int):
        self._cancel_permissions("agent_exited")
        if self.state in ("closed", "died"):
            return
        self.error = "Процесс агента завершился с кодом {}".format(code)
        self.state = "died"
        self.emit("session_died", reason="agent_exited", returncode=code, error=self.error,
                  stderrTail="".join(self._stderr_tail)[-4000:])
        with self._lock:
            self._wake.notify_all()

    def close(self, reason: str = "client_request"):
        if self.state in ("closed",):
            return
        was_dead = self.state == "died"
        if not was_dead:  # упавшая сессия остаётся "died", чтобы в списке было видно, что она сломалась
            self.state = "closed"
        self._cancel_permissions("session_closed")
        with self._lock:
            dropped = [p for p in self._prompts if p is not self._running]
            self._prompts.clear()
            self._wake.notify_all()
        for p in dropped:
            self.emit("pending_prompt_completed", promptId=p["promptId"], outcome="dropped")
        if self.conn:
            self.conn.close()
        if not was_dead:
            self.emit("session_closed", reason=reason)

    @property
    def alive(self) -> bool:
        return self.state in ("idle", "running")

    # --- запросы ----------------------------------------------------------
    def submit_prompt(self, blocks: List[dict], source: str = "api",
                      client_id: Optional[str] = None) -> dict:
        if not self.alive:
            raise ApiError(409, "Сессия недоступна (состояние: {})".format(self.state), "session_not_live")
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not blocks or not any(b.get("type") for b in blocks):
            raise ApiError(400, "prompt: нужен непустой массив блоков [{type, text}]", "invalid_prompt")
        with self._lock:
            if len(self._prompts) >= self.manager.max_pending_prompts:
                raise ApiError(503, "Очередь запросов сессии заполнена", "prompt_queue_full")
            p = {"promptId": str(uuid.uuid4()), "text": text, "content": blocks, "queuedAt": _ms(),
                 "state": "queued", "source": source, "originatorClientId": client_id}
            self._prompts.append(p)
            self._wake.notify_all()
        if not self.display_name and text:
            self.display_name = text.splitlines()[0][:80]
        self.emit("pending_prompt_added", originator=client_id, promptId=p["promptId"], text=text,
                  source=source, position=len(self._prompts))
        return {"promptId": p["promptId"], "lastEventId": self.last_event_id}

    def pending_prompts(self) -> List[dict]:
        with self._lock:
            return [{k: p[k] for k in ("promptId", "text", "queuedAt", "state", "source", "originatorClientId")}
                    for p in self._prompts]

    def _prompt_worker(self):
        while True:
            with self._lock:
                while self.alive and not self._prompts:
                    self._wake.wait()
                if not self.alive:
                    return
                p = self._prompts[0]
                p["state"] = "running"
                self._running = p
            self._run_prompt(p)
            with self._lock:
                if self._prompts and self._prompts[0] is p:
                    self._prompts.popleft()
                self._running = None

    def _run_prompt(self, p: dict):
        pid, origin = p["promptId"], p["originatorClientId"]
        self._set_state("running")
        self.emit("pending_prompt_started", originator=origin, promptId=pid)
        # реплика пользователя — в том же виде, в каком её шлёт агент при восстановлении истории
        self.emit("session_update", originator=origin, promptId=pid, update={
            "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": p["text"]},
            "_meta": {"source": p["source"]}})
        started = time.time()
        p["cancelled"] = False
        try:
            res = self.conn.request("session/prompt", {"sessionId": self.acp_session_id,
                                                       "prompt": p["content"]},
                                    timeout=self.manager.prompt_deadline) or {}
            stop = res.get("stopReason")
            if p["cancelled"] or stop == "cancelled":
                self.emit("prompt_cancelled", originator=origin, promptId=pid)
            self.emit("turn_complete", originator=origin, promptId=pid, stopReason=stop,
                      elapsedMs=int((time.time() - started) * 1000), _meta=res.get("_meta"))
            outcome = "completed"
        except AcpError as e:
            code = e.code
            if "Таймаут" in str(e):
                code = "prompt_deadline_exceeded"
                self.cancel()
            self.emit("turn_error", originator=origin, promptId=pid, message=str(e), code=code,
                      elapsedMs=int((time.time() - started) * 1000),
                      stderrTail="".join(self._stderr_tail)[-2000:] if self.state == "died" else None)
            outcome = "error"
        finally:
            self._cancel_permissions("turn_finished")
        self.emit("pending_prompt_completed", originator=origin, promptId=pid, outcome=outcome)
        if self.state == "running":
            self._set_state("idle")

    def cancel(self) -> bool:
        """Отменяет выполняющийся запрос (очередь не трогает)."""
        with self._lock:
            running = self._running
        if not running or not self.conn:
            return False
        running["cancelled"] = True
        try:
            self.conn.notify("session/cancel", {"sessionId": self.acp_session_id})
        except AcpError:
            pass
        # по спецификации ACP незакрытые запросы разрешений при отмене отвечаются "cancelled"
        self._cancel_permissions("prompt_cancelled")
        self.emit("cancel_requested", promptId=running["promptId"])
        return True

    def set_model(self, model_id: str):
        prev = (self.models or {}).get("currentModelId")
        try:
            self.conn.request("session/set_model", {"sessionId": self.acp_session_id, "modelId": model_id},
                              timeout=30)
        except AcpError as e:
            self.emit("model_switch_failed", requestedModelId=model_id, error=str(e))
            raise ApiError(400, "Не удалось сменить модель: {}".format(e), "model_switch_failed")
        if self.models is not None:
            self.models["currentModelId"] = model_id
        self.emit("model_switched", modelId=model_id, previous=prev)

    def set_approval_mode(self, mode: str):
        prev = (self.modes or {}).get("currentModeId")
        try:
            self.conn.request("session/set_mode", {"sessionId": self.acp_session_id, "modeId": mode},
                              timeout=30)
        except AcpError as e:
            raise ApiError(400, "Не удалось сменить режим: {}".format(e), "approval_mode_failed")
        if self.modes is not None:
            self.modes["currentModeId"] = mode
        self.emit("approval_mode_changed", previous=prev, next=mode, persisted=False)

    # --- разрешения -------------------------------------------------------
    def pending_interactions(self) -> List[dict]:
        with self._lock:
            return [p["public"] for p in self._permissions.values()]

    def vote(self, request_id: str, body: dict, client_id: Optional[str] = None):
        with self._lock:
            p = self._permissions.get(request_id)
        if not p:
            raise ApiError(404, "Запрос разрешения не найден или уже закрыт", "permission_not_found",
                           requestId=request_id)
        outcome = body.get("outcome")
        if isinstance(outcome, dict):  # допускаем и ACP-форму {"outcome": {"outcome": ..., "optionId": ...}}
            body, outcome = outcome, outcome.get("outcome")
        if outcome == "cancelled":
            result = {"outcome": "cancelled"}
        elif outcome == "selected":
            valid = [o.get("optionId") for o in p["public"]["options"]]
            if body.get("optionId") not in valid:
                raise ApiError(400, "optionId должен быть одним из: {}".format(", ".join(map(str, valid))),
                               "invalid_option_id")
            result = {"outcome": "selected", "optionId": body["optionId"]}
        else:
            raise ApiError(400, 'outcome: "selected" (с optionId) или "cancelled"', "invalid_outcome")
        with self._lock:
            if p["event"].is_set():
                raise ApiError(409, "Уже решено", "permission_already_resolved", requestId=request_id)
            p["outcome"], p["by"], p["client"] = result, "client", client_id
            p["event"].set()

    def _cancel_permissions(self, reason: str):
        with self._lock:
            items = list(self._permissions.values())
            for p in items:
                if not p["event"].is_set():
                    p["outcome"], p["by"] = {"outcome": "cancelled"}, reason
                    p["event"].set()

    def _auto_option(self, options: List[dict], allow: bool) -> Optional[str]:
        kinds = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
        for kind in kinds:
            for o in options:
                if o.get("kind") == kind:
                    return o.get("optionId")
        return None

    def _request_permission(self, params: dict) -> dict:
        options = params.get("options") or []
        rid = str(uuid.uuid4())
        public = {"requestId": rid, "toolCall": params.get("toolCall"), "options": options,
                  "_meta": params.get("_meta"), "createdAt": _ms()}

        if self.permission_policy in ("allow", "reject"):
            opt = self._auto_option(options, allow=self.permission_policy == "allow")
            outcome = {"outcome": "selected", "optionId": opt} if opt else {"outcome": "cancelled"}
            self.emit("permission_request", **public, autoPolicy=self.permission_policy)
            self.emit("permission_resolved", requestId=rid, outcome=outcome, by="policy")
            return {"outcome": outcome}

        entry = {"public": public, "event": threading.Event(), "outcome": None, "by": None, "client": None}
        with self._lock:
            self._permissions[rid] = entry
        self.emit("permission_request", **public)
        try:
            if not entry["event"].wait(self.manager.permission_timeout):
                entry["outcome"], entry["by"] = {"outcome": "cancelled"}, "timeout"
        finally:
            with self._lock:
                self._permissions.pop(rid, None)
        self.emit("permission_resolved", originator=entry["client"], requestId=rid,
                  outcome=entry["outcome"], by=entry["by"])
        return {"outcome": entry["outcome"]}

    # --- обработчики ACP --------------------------------------------------
    def _on_notification(self, method: str, params: dict):
        if method == "session/update":
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "current_mode_update" and self.modes is not None:
                self.modes["currentModeId"] = update.get("currentModeId")
            elif kind == "usage_update":
                self.usage = {"used": update.get("used"), "size": update.get("size")}
            with self._lock:
                pid = self._running["promptId"] if self._running else None
            self.emit("session_update", promptId=pid, update=update)
        else:
            self.emit("agent_notification", method=method, params=params)

    def _on_request(self, method: str, params: dict):
        if method == "session/request_permission":
            return self._request_permission(params)
        # fs/* и terminal/* мы не объявляли — агент не должен их вызывать
        self.emit("log", text="Агент вызвал неподдерживаемый метод {}".format(method))
        raise AcpError("Method not supported by client: {}".format(method), code=-32601)

    # --- представление ----------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            pending = self.pending_interactions()
            running = self._running
            clients = len(self._subscribers)
            queued = len(self._prompts)
        return {
            "sessionId": self.id, "workspaceCwd": self.cwd, "createdAt": self.created,
            "updatedAt": self.updated, "clientCount": clients, "hasActivePrompt": running is not None,
            "activeWorkState": "active" if running else "idle",
            "isWaitingForPermission": bool(pending), "pendingInteractionCount": len(pending),
            "pendingInteractions": pending, "displayName": self.display_name or None,
            # расширения этого сервиса
            "state": self.state, "kind": self.kind, "error": self.error, "queuedPrompts": queued,
            "activePromptId": running["promptId"] if running else None,
            "permissionPolicy": self.permission_policy, "modes": self.modes, "models": self.models,
            "usage": self.usage, "agentInfo": self.agent_info, "lastEventId": self._seq,
            "acpSessionId": self.acp_session_id,
        }

    def export_markdown(self) -> str:
        out = ["# {}\n".format(self.display_name or self.id), "_cwd: {}_\n".format(self.cwd)]
        buf_kind, buf = None, []

        def flush():
            if buf:
                text = "".join(buf).strip()
                if buf_kind == "agent_thought_chunk":
                    out.append("> " + text.replace("\n", "\n> ") + "\n")
                else:
                    out.append(text + "\n")
            buf.clear()

        for e in list(self._ring):
            d = e["data"]
            if e["type"] == "session_update":
                u = d["update"]
                k = u.get("sessionUpdate")
                if k in ("agent_message_chunk", "agent_thought_chunk"):
                    if k != buf_kind:
                        flush()
                        buf_kind = k
                    buf.append((u.get("content") or {}).get("text", ""))
                    continue
                flush()
                buf_kind = None
                if k == "user_message_chunk":
                    out.append("## Пользователь\n\n" + (u.get("content") or {}).get("text", "") + "\n\n## Агент\n")
                elif k == "tool_call":
                    out.append("- 🔧 **{}** ({})".format(u.get("title"), u.get("kind")))
                elif k == "tool_call_update" and u.get("status") in ("completed", "failed"):
                    out.append("  - {}: {}".format(u.get("toolCallId"), u.get("status")))
            elif e["type"] in ("turn_error", "session_died"):
                flush()
                out.append("**Ошибка:** {}\n".format(d.get("message") or d.get("error")))
        flush()
        return "\n".join(out)


class SessionManager:
    def __init__(self, base_command: List[str], default_cwd: str, *, approval_mode: str = "auto-edit",
                 init_timeout: float = 120, prompt_deadline: float = 1800, permission_timeout: float = 1800,
                 max_sessions: int = 16, max_pending_prompts: int = 5, event_ring_size: int = 8000,
                 idle_ttl: float = 1800, keep_closed: int = 200):
        self.base_command = base_command
        self.default_cwd = default_cwd
        self.approval_mode = approval_mode
        self.init_timeout = init_timeout
        self.prompt_deadline = prompt_deadline
        self.permission_timeout = permission_timeout
        self.max_sessions = max_sessions
        self.max_pending_prompts = max_pending_prompts
        self.event_ring_size = event_ring_size
        self.idle_ttl = idle_ttl
        self.keep_closed = keep_closed
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._creating: Dict[str, threading.Event] = {}
        threading.Thread(target=self._reaper, daemon=True).start()

    def create(self, cwd: Optional[str] = None, *, scope: str = "single", title: str = "",
               kind: str = "interactive", permission_policy: str = "ask",
               include_dirs: Optional[List[str]] = None) -> Tuple[Session, bool]:
        """Возвращает (сессия, attached). scope=single — подключиться к живой сессии в том же каталоге."""
        cwd = os.path.abspath(os.path.expanduser(cwd or self.default_cwd))
        if not os.path.isdir(cwd):
            raise ApiError(400, "Каталог не существует: {}".format(cwd), "invalid_cwd")
        if scope not in ("single", "thread"):
            raise ApiError(400, 'sessionScope: "single" или "thread"', "invalid_session_scope")
        if permission_policy not in PERMISSION_POLICIES:
            raise ApiError(400, "permissionPolicy: одно из {}".format(", ".join(PERMISSION_POLICIES)),
                           "invalid_permission_policy")
        include_dirs = list(dict.fromkeys(os.path.normpath(os.path.join(cwd, os.path.expanduser(p)))
                                          for p in include_dirs or []))
        for path in include_dirs:
            if not os.path.isdir(path):
                raise ApiError(400, "Каталог не существует: {}".format(path), "invalid_include_directory")
        with self._lock:
            if scope == "single":
                for s in self.sessions.values():
                    if s.kind == "interactive" and s.cwd == cwd and s.alive:
                        return s, True
            live = sum(1 for s in self.sessions.values() if s.state in ("starting", "idle", "running"))
            if live >= self.max_sessions:
                raise ApiError(503, "Достигнут лимит живых сессий ({})".format(self.max_sessions),
                               "session_limit_exceeded")
            s = Session(self, cwd, title=title, kind=kind, permission_policy=permission_policy,
                        include_dirs=include_dirs)
            self.sessions[s.id] = s
            self._prune()
        s.start()
        return s, False

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s:
            raise ApiError(404, "No session with id {}".format(sid), "session_not_found", sessionId=sid)
        return s

    def find_permission(self, request_id: str) -> Session:
        for s in self.sessions.values():
            if any(p["requestId"] == request_id for p in s.pending_interactions()):
                return s
        raise ApiError(404, "Запрос разрешения не найден", "permission_not_found", requestId=request_id)

    def list(self) -> List[dict]:
        return sorted((s.status() for s in list(self.sessions.values())), key=lambda x: -x["updatedAt"])

    def counters(self) -> dict:
        ss = list(self.sessions.values())
        return {"sessions": sum(1 for s in ss if s.alive),
                "pendingPermissions": sum(len(s.pending_interactions()) for s in ss),
                "activePrompts": sum(1 for s in ss if s.state == "running"),
                "connectedClients": sum(len(s._subscribers) for s in ss)}

    def _prune(self):
        dead = sorted((s for s in self.sessions.values() if not s.alive and s.state != "starting"),
                      key=lambda s: s.updated)
        for s in dead[: max(0, len(dead) - self.keep_closed)]:
            self.sessions.pop(s.id, None)

    def _reaper(self):
        while True:
            time.sleep(60)
            now = _ms()
            for s in list(self.sessions.values()):
                if s.state == "idle" and not s._subscribers and now - s.updated > self.idle_ttl * 1000:
                    s.close(reason="idle_timeout")

    def shutdown(self):
        for s in list(self.sessions.values()):
            s.close(reason="server_shutdown")


def run_once(manager: SessionManager, text: str, cwd: Optional[str], *, source: str, policy: str,
             on_delta: Callable[[str], None], on_idle: Callable[[], None], idle_every: float = 3.0) -> dict:
    """Разовый запрос через живую сессию (для /api/chat и /v1): виден в UI, после ответа сессия закрывается.

    on_idle вызывается, пока агент молчит; если он бросает исключение (клиент ушёл) — запрос отменяется.
    """
    s, _ = manager.create(cwd, scope="thread", kind="api", permission_policy=policy,
                          title="[{}] {}".format(source, text.strip().splitlines()[0][:70] if text.strip() else ""))
    q, _, _ = s.subscribe(None)
    try:
        pid = s.submit_prompt([{"type": "text", "text": text}], source=source)["promptId"]
        parts: List[str] = []
        while True:
            try:
                ev = q.get(timeout=idle_every)
            except queue.Empty:
                try:
                    on_idle()
                except Exception:
                    s.cancel()
                    raise
                continue
            d = ev["data"]
            if ev["type"] == "session_update" and d.get("promptId") == pid:
                u = d["update"]
                if u.get("sessionUpdate") == "agent_message_chunk":
                    t = (u.get("content") or {}).get("text", "")
                    if t:
                        parts.append(t)
                        try:
                            on_delta(t)
                        except Exception:
                            s.cancel()
                            raise
            elif ev["type"] == "turn_complete" and d.get("promptId") == pid:
                return {"text": "".join(parts).strip(), "stopReason": d.get("stopReason"),
                        "sessionId": s.id, "elapsedMs": d.get("elapsedMs")}
            elif ev["type"] == "turn_error" and d.get("promptId") == pid:
                raise ApiError(502, d.get("message") or "Ошибка агента", "turn_error", sessionId=s.id,
                               stderrTail=d.get("stderrTail"))
            elif ev["type"] == "session_died":
                raise ApiError(502, d.get("error") or "Агент упал", "session_died", sessionId=s.id,
                               stderrTail=d.get("stderrTail"))
    finally:
        s.unsubscribe(q)
        s.close(reason="request_done")
