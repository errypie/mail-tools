#!/usr/bin/env bash
# Установка gigacode-serve и почты на Linux без root: сервисы работают как systemd --user.
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_DIR/systemd/user"
GIGA_PORT=8765
MAIL_PORT=8770

usage() {
    cat <<'EOF'
Использование: ./install.sh [параметры]

Запуск из клона mail-tools: gigacode ставится рядом, адрес берётся из origin.
Запуск отдельным файлом: нужен --git-base, оба репозитория ставятся в --root.

  --git-base URL      адрес группы с репозиториями, например https://git.example.com/team
  --root DIR          куда ставить всё (по умолчанию рядом с mail-tools или /home/work/<пользователь>)
  --workdir DIR       рабочий каталог агента (по умолчанию <root>/workspace)
  --gigacode-cmd CMD  команда запуска CLI, если gigacode нет в PATH
  --pip-index URL     внутреннее зеркало пакетов
  --update            обновить репозитории и зависимости
  --uninstall         остановить и удалить сервисы, код и данные остаются
  -h, --help          эта справка
EOF
}

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'Внимание: %s\n' "$*" >&2; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

# Доменное имя бывает вида DOMAIN\user или user@domain, каталог называется просто user.
default_root() {
    local name
    name=$(id -un)
    name=${name##*\\}
    name=${name%%@*}
    if [ -d "/home/work/$name" ]; then
        printf '%s' "/home/work/$name"
    else
        printf '%s' "$HOME/gigacode-mail"
    fi
}

# Кавычки для ExecStart: у systemd свои спецсимволы % и $.
unit_quote() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//%/%%}
    s=${s//\$/\$\$}
    printf '"%s"' "$s"
}

env_line() {
    local s=$2
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    printf '%s="%s"\n' "$1" "$s"
}

check_systemd() {
    command -v systemctl >/dev/null || die "нужен Linux с systemd"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    systemctl --user show-environment >/dev/null 2>&1 ||
        die "systemd --user недоступен. Запустите скрипт в обычном входе в систему, а не через su или sudo"
}

# Пароль git спрашивается один раз и живёт только в памяти, на диск не попадает.
start_credential_cache() {
    GIT_OPTS=()
    if [ -z "$(git config --get-all credential.helper || true)" ]; then
        GIT_OPTS=(-c "credential.helper=cache --timeout=900")
        trap 'git credential-cache exit >/dev/null 2>&1 || true' EXIT
    fi
}

fetch_repo() {
    local name=$1 dir=$2
    if [ -d "$dir/.git" ]; then
        if [ "$UPDATE" = 1 ]; then
            say "Обновляю $name"
            git ${GIT_OPTS[@]+"${GIT_OPTS[@]}"} -C "$dir" pull --ff-only
        fi
    elif [ -e "$dir" ]; then
        die "$dir уже существует, но это не git-репозиторий"
    else
        [ -n "$GIT_BASE" ] || die "не знаю, откуда скачать $name: укажите --git-base"
        say "Скачиваю $name"
        git ${GIT_OPTS[@]+"${GIT_OPTS[@]}"} clone "$GIT_BASE/$name.git" "$dir"
    fi
}

make_venv() {
    local venv=$1
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null || die "нужен python3 версии 3.9 или новее"
    [ -x "$venv/bin/python" ] && return
    say "Создаю окружение Python"
    python3 -m venv "$venv" >/dev/null 2>&1 && return
    rm -rf "$venv"  # без python3-venv остаётся неполный каталог
    python3 -m pip install --user virtualenv && python3 -m virtualenv "$venv" ||
        die "не удалось создать окружение: нет python3-venv и virtualenv"
}

write_env() {
    mkdir -p "$ENV_DIR"
    chmod 700 "$ENV_DIR"
    local name value bypass
    (
        umask 077
        {
            echo "# Создаётся install.sh и перезаписывается при каждом запуске. Свои значения пишите в local.env."
            env_line PATH "$PATH"
            for name in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NODE_EXTRA_CA_CERTS SSL_CERT_FILE REQUESTS_CA_BUNDLE; do
                value=${!name:-}
                [ -z "$value" ] || env_line "$name" "$value"
            done
            # Сервисы ходят друг к другу напрямую, мимо прокси
            bypass=${NO_PROXY:-${no_proxy:-}}
            bypass="${bypass:+$bypass,}127.0.0.1,localhost"
            env_line NO_PROXY "$bypass"
            env_line no_proxy "$bypass"
        } > "$ENV_DIR/install.env"
        [ -e "$ENV_DIR/local.env" ] || echo "# Свои переменные окружения для сервисов: ИМЯ=\"значение\"" > "$ENV_DIR/local.env"
    )
}

write_units() {
    local python=$1 giga_dir=$2 mail_dir=$3 workdir=$4
    local giga_exec
    giga_exec="$(unit_quote "$python") server.py --workdir $(unit_quote "$workdir")"
    [ -z "$GIGACODE_CMD" ] || giga_exec="$giga_exec --cmd $(unit_quote "$GIGACODE_CMD")"
    mkdir -p "$UNIT_DIR" || die "нет прав на $UNIT_DIR: systemd --user читает сервисы только оттуда"

    cat > "$UNIT_DIR/gigacode-serve.service" <<EOF
[Unit]
Description=GigaCode Serve

[Service]
WorkingDirectory=$giga_dir
EnvironmentFile=$ENV_DIR/install.env
EnvironmentFile=-$ENV_DIR/local.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$giga_exec
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
EOF

    cat > "$UNIT_DIR/mailbot.service" <<EOF
[Unit]
Description=GigaMail
Wants=gigacode-serve.service
After=gigacode-serve.service

[Service]
WorkingDirectory=$mail_dir
EnvironmentFile=$ENV_DIR/install.env
EnvironmentFile=-$ENV_DIR/local.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$(unit_quote "$python") -m mailbot
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
EOF
}

wait_port() {
    local python=$1 port=$2
    "$python" - "$port" <<'PY'
import socket, sys, time
for _ in range(40):
    try:
        socket.create_connection(("127.0.0.1", int(sys.argv[1])), 1).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit(1)
PY
}

uninstall() {
    check_systemd
    systemctl --user disable --now mailbot.service gigacode-serve.service 2>/dev/null || true
    rm -f "$UNIT_DIR/mailbot.service" "$UNIT_DIR/gigacode-serve.service"
    systemctl --user daemon-reload
    say "Сервисы удалены. Код, письма и секреты остались на месте"
}

main() {
    GIT_BASE="" ROOT="" WORKDIR="" GIGACODE_CMD="" UPDATE=0
    while [ $# -gt 0 ]; do
        case $1 in
            --git-base|--root|--workdir|--gigacode-cmd|--pip-index)
                [ $# -ge 2 ] || die "для $1 нужно значение"
                case $1 in
                    --git-base) GIT_BASE=${2%/} ;;
                    --root) ROOT=$2 ;;
                    --workdir) WORKDIR=$2 ;;
                    --gigacode-cmd) GIGACODE_CMD=$2 ;;
                    --pip-index) export PIP_INDEX_URL=$2 ;;
                esac
                shift 2 ;;
            --update) UPDATE=1; shift ;;
            --uninstall) uninstall; return ;;
            -h|--help) usage; return ;;
            *) usage >&2; die "неизвестный параметр $1" ;;
        esac
    done

    [ "$(id -u)" != 0 ] || die "запускайте без sudo: root не нужен"
    check_systemd
    command -v git >/dev/null || die "нужен git"

    local self_dir mail_dir giga_dir origin
    self_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    if [ -f "$self_dir/mailbot/__main__.py" ]; then
        mail_dir=$self_dir
        ROOT=${ROOT:-$(dirname "$self_dir")}
        origin=$(git -C "$self_dir" remote get-url origin 2>/dev/null || true)
        GIT_BASE=${GIT_BASE:-${origin%/*}}
    else
        ROOT=${ROOT:-$(default_root)}
        mail_dir=$ROOT/mail-tools
    fi
    mkdir -p "$ROOT"
    ROOT=$(cd "$ROOT" && pwd)
    giga_dir=$ROOT/gigacode
    ENV_DIR=$ROOT/config
    WORKDIR=${WORKDIR:-$ROOT/workspace}
    mkdir -p "$WORKDIR"
    WORKDIR=$(cd "$WORKDIR" && pwd)

    start_credential_cache
    fetch_repo mail-tools "$mail_dir"
    fetch_repo gigacode "$giga_dir"

    local venv=$mail_dir/.venv
    make_venv "$venv"
    say "Ставлю зависимости"
    "$venv/bin/python" -m pip install --quiet --no-cache-dir -r "$mail_dir/mailbot/requirements.txt"

    if [ -z "$GIGACODE_CMD" ] && ! command -v gigacode >/dev/null; then
        warn "gigacode не найден в PATH. Агент не запустится, пока вы не повторите установку с --gigacode-cmd"
    fi

    say "Настраиваю systemd --user"
    write_env
    write_units "$venv/bin/python" "$giga_dir" "$mail_dir" "$WORKDIR"
    systemctl --user daemon-reload
    systemctl --user enable gigacode-serve.service mailbot.service
    systemctl --user restart gigacode-serve.service mailbot.service

    wait_port "$venv/bin/python" "$GIGA_PORT" ||
        die "gigacode-serve не запустился. Журнал: journalctl --user -u gigacode-serve -n 50"
    wait_port "$venv/bin/python" "$MAIL_PORT" ||
        die "GigaMail не запустился. Журнал: journalctl --user -u mailbot -n 50"

    cat <<EOF

Готово.
  GigaMail:  http://127.0.0.1:$MAIL_PORT
  GigaCode:  http://127.0.0.1:$GIGA_PORT
  Код:       $ROOT
  Данные:    $mail_dir/mail_data

После перезапуска сервиса войдите в веб-интерфейс: без этого почта не опрашивается.
Журнал:    journalctl --user -u mailbot -f
Состояние: systemctl --user status mailbot gigacode-serve
EOF
}

main "$@"
