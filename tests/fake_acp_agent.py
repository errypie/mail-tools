#!/usr/bin/env python3
"""Имитация `gigacode --acp` для локальной проверки сервера.

python server.py --cmd "python3 $PWD/tests/fake_acp_agent.py"
Слова в запросе: TOOL — вызов инструмента с запросом разрешения, SLOW — долгие размышления,
CRASH — процесс падает посреди хода.
"""
import json
import os
import re
import sys
import threading
import time
import uuid

out_lock = threading.Lock()
pending = {}
cancelled = set()
next_id = [1000]


def send(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})


def chunk(sid, kind, text):
    for word in text.split(" "):
        if sid in cancelled:
            return
        update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": word + " "}})
        time.sleep(0.03)


def ask_client(method, params):
    next_id[0] += 1
    rid = next_id[0]
    ev = threading.Event()
    pending[rid] = {"event": ev}
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    ev.wait()
    return pending.pop(rid)["msg"]


MODES = {"currentModeId": "auto-edit", "availableModes": [
    {"id": "plan", "name": "Plan"}, {"id": "default", "name": "Default"},
    {"id": "auto-edit", "name": "Auto Edit"}]}


def handle_prompt(rid, params):
    sid = params["sessionId"]
    cancelled.discard(sid)
    text = " ".join(p.get("text", "") for p in params["prompt"])
    sys.stderr.write("[fake] prompt received: {} chars\n".format(len(text)))
    sys.stderr.flush()
    if "Ты помогаешь написать" in text:
        compose(rid, sid, text)
        return
    if "<<<EMAIL" in text:
        classify(rid, sid, text)
        return
    chunk(sid, "agent_thought_chunk", "Пользователь спрашивает про «{}». Надо подумать, какие файлы посмотреть.".format(text[:40]))
    if "SLOW" in text:
        for _ in range(100):
            if sid in cancelled:
                break
            time.sleep(0.1)
    if "CRASH" in text:
        sys.stderr.write("FATAL: simulated crash\n")
        sys.stderr.flush()
        os._exit(3)
    update(sid, {"sessionUpdate": "plan", "entries": [
        {"content": "Прочитать README", "status": "completed", "priority": "medium"},
        {"content": "Выполнить команду", "status": "in_progress", "priority": "high"},
        {"content": "Ответить", "status": "pending", "priority": "medium"}]})
    if "TOOL" in text and sid not in cancelled:
        tc = "call_" + uuid.uuid4().hex[:6]
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": tc, "title": "ls -la", "kind": "execute",
                     "status": "pending", "rawInput": {"command": "ls -la"}})
        resp = ask_client("session/request_permission", {"sessionId": sid, "toolCall": {
            "toolCallId": tc, "title": "Shell: ls -la", "kind": "execute", "rawInput": {"command": "ls -la"}},
            "options": [{"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "proceed_always", "name": "Always Allow in project: ls", "kind": "allow_always"},
                        {"optionId": "cancel", "name": "Reject", "kind": "reject_once"}]})
        outcome = (resp.get("result") or {}).get("outcome") or {}
        if outcome.get("outcome") == "selected" and outcome.get("optionId") != "cancel":
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tc, "status": "in_progress"})
            time.sleep(0.3)
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tc, "status": "completed",
                         "content": [{"type": "content", "content": {"type": "text", "text": "README.md\nserver.py"}}]})
        else:
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tc, "status": "failed",
                         "content": [{"type": "content", "content": {"type": "text", "text": "Отклонено пользователем"}}]})
    if sid in cancelled:
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        return
    chunk(sid, "agent_message_chunk", "Готово. Получил **{}** символов.\n\n```bash\nls -la\n```".format(len(text)))
    update(sid, {"sessionUpdate": "usage_update", "used": 1234, "size": 128000})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled" if sid in cancelled else "end_turn"}})


def compose(rid, sid, text):
    """Черновик письма для GigaMail: учитывает пожелания, чтобы тест видел, что они дошли."""
    wish = re.search(r"Пожелания пользователя: (.*)", text)
    lines = ["Добрый день!", "",
             "Спасибо за письмо. Посмотрю материалы и вернусь с ответом до конца дня."]
    if wish:
        lines += ["", "Учёл пожелание: {}.".format(wish.group(1).strip().rstrip("."))]
    lines += ["", "С уважением,"]
    for part in ["\n".join(lines[:3]), "\n" + "\n".join(lines[3:])]:
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": part}})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def classify(rid, sid, text):
    """Ответ на промпт mailbot: категория по ключевым словам письма."""
    head, mail = text.split("<<<EMAIL", 1)
    entities = {name: "тест-" + name for name in re.findall(r'"(\w+)": <значение или null>', head)}
    if "BROKEN" in mail:
        answer = "это не JSON"
    else:
        invite = "Это приглашение на встречу." in head
        # приглашение — по теме, если такая категория есть (как просит промпт), иначе «Встречи»
        topical = next((name for name, title in re.findall(r"^### (\w+) \(([^)]*)\)", head, re.M)
                        if invite and title and name != "MEETING"
                        and re.search(r"(?<!\w)" + re.escape(title.lower()) + r"(?!\w)", mail.lower())), None)
        category = (topical if topical else "MEETING" if invite else "RELEASE" if "ABC-" in mail else "AUTO" if "[JIRA]" in mail else
                    "SPAM" if "Скидки" in mail else "UNINFO")
        data = {"category": category, "confidence": 0.9, "summary": "Тестовое саммари письма.",
                "reason": "по ключевым словам", "jira_key": "ABC-123" if category == "RELEASE" else None,
                "recommend_action": category == "RELEASE",
                "action_hint": "Проверить релиз" if category == "RELEASE" else "", "entities": entities}
        if invite:  # свободно — принять, митап — решит человек, занято — отклонить и предложить окно
            if "Митап" in mail or "Для информации" in mail:
                data["invite"] = {"action": "none", "reply": "", "propose": None}
            elif "Пересечения в календаре:\nнет" in head:
                data["invite"] = {"action": "accept", "reply": "Буду.", "propose": None}
            else:
                data["invite"] = {"action": "decline", "propose": 1, "reply":
                                  "Добрый день! В это время у меня другая встреча. Могу в одно из окон:\n{free_slots}"}
        answer = "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": answer}})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def main():
    args = sys.argv[1:]
    if "--acp" not in args:
        sys.stderr.write("expected --acp, got {!r}\n".format(args))
        sys.exit(2)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "method" not in msg:
            if msg.get("id") in pending:
                pending[msg["id"]]["msg"] = msg
                pending[msg["id"]]["event"].set()
            continue
        m, p, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                "agentInfo": {"name": "fake-gigacode", "version": "0.0.1"}, "authMethods": []}})
        elif m == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": uuid.uuid4().hex, "modes": MODES}})
        elif m == "session/prompt":
            threading.Thread(target=handle_prompt, args=(rid, p), daemon=True).start()
        elif m == "session/cancel":
            cancelled.add(p.get("sessionId"))
        elif m == "session/set_mode":
            MODES["currentModeId"] = p.get("modeId")
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
            update(p["sessionId"], {"sessionUpdate": "current_mode_update", "currentModeId": p.get("modeId")})
        elif m == "session/set_model":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found: " + m}})


if __name__ == "__main__":
    main()
