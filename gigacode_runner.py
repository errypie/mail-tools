"""Обёртка над CLI: gigacode --approval-mode auto-edit -p "<prompt>".

Каждый вызов CLI независим, поэтому история диалога вклеивается в текст промпта.
Используется и сервером (server.py), и напрямую из других скриптов:

    from gigacode_runner import GigaCode
    print(GigaCode().ask("Привет!"))
"""
from __future__ import annotations

import codecs
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional

IS_WINDOWS = os.name == "nt"
# Windows ограничивает командную строку ~32k символов, unix — сотнями КБ.
DEFAULT_MAX_PROMPT_CHARS = 30000 if IS_WINDOWS else 200000

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


class GigaCodeError(RuntimeError):
    pass


def _resolve_base_command(explicit: Optional[str]) -> List[str]:
    """Возвращает команду запуска CLI без аргументов.

    GIGACODE_CMD (или explicit) позволяет задать её вручную, например
    "node C:/Users/me/AppData/Roaming/npm/node_modules/gigacode/dist/cli.js".
    """
    explicit = explicit or os.environ.get("GIGACODE_CMD")
    if explicit:
        return shlex.split(explicit, posix=not IS_WINDOWS)

    path = shutil.which("gigacode")
    if not path:
        raise GigaCodeError(
            "gigacode не найден в PATH. Укажите путь через переменную GIGACODE_CMD или --cmd."
        )

    # На Windows npm ставит обёртку gigacode.cmd. Запуск .cmd идёт через cmd.exe,
    # который ломает аргументы со спецсимволами (& | " % ^ <>), поэтому
    # по возможности вызываем node напрямую с JS-файлом из обёртки.
    if IS_WINDOWS and path.lower().endswith((".cmd", ".bat")):
        node_cmd = _node_cmd_from_npm_shim(path)
        if node_cmd:
            return node_cmd
    return [path]


def _node_cmd_from_npm_shim(shim_path: str) -> Optional[List[str]]:
    try:
        text = open(shim_path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    m = re.search(r'"%dp0%\\([^"]+?\.(?:js|mjs|cjs))"', text, re.IGNORECASE)
    if not m:
        return None
    script = os.path.join(os.path.dirname(shim_path), m.group(1))
    if not os.path.exists(script):
        return None
    local_node = os.path.join(os.path.dirname(shim_path), "node.exe")
    node = local_node if os.path.exists(local_node) else shutil.which("node")
    return [node, script] if node else None


def _content_to_text(content) -> str:
    """OpenAI допускает content строкой или списком частей [{type: text, text: ...}]."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


ROLE_TITLES = {"user": "Пользователь", "assistant": "Ассистент", "tool": "Инструмент"}


def build_prompt(messages: List[dict], max_chars: int = DEFAULT_MAX_PROMPT_CHARS) -> str:
    """Склеивает историю чата в один промпт для одиночного вызова CLI.

    Если всё не влезает в max_chars, самые старые реплики отбрасываются
    (системные инструкции и последний вопрос сохраняются всегда).
    """
    system = "\n\n".join(
        _content_to_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")
    ).strip()
    dialog = [
        (m.get("role", "user"), _content_to_text(m.get("content")).strip())
        for m in messages
        if m.get("role") not in ("system", "developer")
    ]
    dialog = [(r, t) for r, t in dialog if t]
    if not dialog:
        raise GigaCodeError("Пустой запрос")

    last_role, last_text = dialog[-1]
    history = dialog[:-1]
    if not history and not system:
        return last_text[:max_chars]

    def render(hist):
        out = []
        if system:
            out.append("### Инструкции\n" + system)
        if hist:
            lines = ["### История диалога (для контекста)"]
            for role, text in hist:
                lines.append("[{}]:\n{}".format(ROLE_TITLES.get(role, role), text))
            out.append("\n\n".join(lines))
        out.append("### Текущий запрос пользователя\n" + last_text)
        return "\n\n".join(out)

    prompt = render(history)
    while len(prompt) > max_chars and history:
        history = history[1:]
        prompt = render(history)
    return prompt[-max_chars:] if len(prompt) > max_chars else prompt


@dataclass
class GigaCode:
    cmd: Optional[str] = None  # переопределение команды запуска (иначе GIGACODE_CMD / PATH)
    workdir: Optional[str] = None  # каталог, в котором CLI работает и может править файлы
    timeout: float = 600.0
    approval_mode: str = "auto-edit"
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    extra_env: dict = field(default_factory=dict)

    def __post_init__(self):
        self._base = _resolve_base_command(self.cmd)

    def command(self, prompt: str) -> List[str]:
        return self._base + ["--approval-mode", self.approval_mode, "-p", prompt]

    # --- простые вызовы -------------------------------------------------
    def ask(self, prompt: str, cwd: Optional[str] = None) -> str:
        return "".join(self.stream(prompt, cwd=cwd)).strip()

    def chat(self, messages: List[dict], cwd: Optional[str] = None) -> str:
        return self.ask(build_prompt(messages, self.max_prompt_chars), cwd=cwd)

    # --- потоковый вызов ------------------------------------------------
    def stream(self, prompt: str, cwd: Optional[str] = None,
               cancel: Optional[threading.Event] = None,
               on_stderr: Optional[Callable[[str], None]] = None,
               info: Optional[dict] = None) -> Iterator[str]:
        """Запускает CLI и отдаёт stdout кусками по мере появления.

        Если генератор закрыть (или выставить cancel), процесс будет убит вместе с дочерними.
        on_stderr(text) получает stderr по мере появления; в info (если передан) пишутся
        pid, cwd и returncode — для журнала запросов.
        """
        if not prompt.strip():
            raise GigaCodeError("Пустой запрос")
        prompt = prompt[: self.max_prompt_chars]
        workdir = cwd or self.workdir or os.getcwd()
        if not os.path.isdir(workdir):
            raise GigaCodeError("Рабочий каталог не существует: {}".format(workdir))

        env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0", **self.extra_env)
        popen_kwargs = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(
                self.command(prompt), cwd=workdir, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **popen_kwargs,
            )
        except OSError as e:
            raise GigaCodeError("Не удалось запустить gigacode: {}".format(e))

        if info is not None:
            info.update(pid=proc.pid, cwd=workdir)

        stderr_chunks: List[str] = []

        def read_stderr():
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                raw = proc.stderr.read1(4096)
                text = ANSI_RE.sub("", dec.decode(raw, final=not raw))
                if text:
                    stderr_chunks.append(text)
                    if on_stderr:
                        on_stderr(text)
                if not raw:
                    return

        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_err.start()

        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            _kill_tree(proc)

        timer = threading.Timer(self.timeout, on_timeout) if self.timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        done = threading.Event()
        if cancel is not None:
            def watch():
                while not done.wait(0.2):
                    if cancel.is_set():
                        _kill_tree(proc)
                        return
            threading.Thread(target=watch, daemon=True).start()

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        produced = False
        try:
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                text = ANSI_RE.sub("", decoder.decode(chunk))
                if text:
                    produced = True
                    yield text
            tail = ANSI_RE.sub("", decoder.decode(b"", final=True))
            if tail:
                produced = True
                yield tail
            code = proc.wait()
            t_err.join(timeout=5)
            if info is not None:
                info["returncode"] = code
            if timed_out.is_set():
                raise GigaCodeError("Превышен таймаут {} с".format(int(self.timeout)))
            if cancel is not None and cancel.is_set():
                return
            if code != 0:
                err = "".join(stderr_chunks).strip()
                raise GigaCodeError("gigacode завершился с кодом {}: {}".format(code, err[-2000:] or "(stderr пуст)"))
            if not produced:
                err = "".join(stderr_chunks).strip()
                if err:
                    raise GigaCodeError("gigacode ничего не вернул. stderr: {}".format(err[-2000:]))
        finally:
            if timer:
                timer.cancel()
            done.set()
            if proc.poll() is None:
                _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


if __name__ == "__main__":
    # python gigacode_runner.py "вопрос"  — быстрый тест без сервера
    q = " ".join(sys.argv[1:]) or sys.stdin.read()
    for piece in GigaCode().stream(q):
        sys.stdout.write(piece)
        sys.stdout.flush()
    print()
