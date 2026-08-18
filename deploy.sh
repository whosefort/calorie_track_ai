#!/usr/bin/env bash
set -Eeuo pipefail

# First run: sudo ./deploy.sh setup
# Updates:   git pull && sudo ./deploy.sh deploy
# Secrets and SQLite live outside git, so a pull never overwrites them.

APP_NAME="calories-bot"
APP_USER="calories-bot"
APP_DIR="/opt/${APP_NAME}"
APP_CODE_DIR="${APP_DIR}/app"
VENV_DIR="${APP_DIR}/venv"
CONFIG_DIR="/etc/${APP_NAME}"
CONFIG_FILE="${CONFIG_DIR}/${APP_NAME}.env"
DATA_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
CADDY_FILE="/etc/caddy/Caddyfile"
CADDY_SITE_DIR="/etc/caddy/sites-enabled"
CADDY_SITE_FILE="${CADDY_SITE_DIR}/${APP_NAME}.caddy"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[0;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

require_root() { [[ "${EUID}" -eq 0 ]] || fail "Запусти: sudo ./deploy.sh $COMMAND"; }
valid_domain() { [[ "$1" =~ ^[A-Za-z0-9.-]+$ && "$1" == *.* ]]; }
write_env_line() { printf '%s=%q\n' "$1" "$2"; }

ensure_dependencies() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates caddy openssl sqlite3
}

check_runtime() {
  [[ -r /etc/os-release ]] || fail "Поддерживаются только Debian/Ubuntu VPS"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "debian" || "${ID:-}" == "ubuntu" ]] || \
    fail "Поддерживаются только Debian/Ubuntu VPS"
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || \
    fail "Нужен Python 3.9 или новее"
}

ensure_firewall() {
  # Не трогаем firewall, если UFW не установлен или выключен. Если он активен,
  # открываем только два порта, нужные Caddy для ACME и Telegram.
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp
    ufw allow 443/tcp
    log "UFW: открыты TCP 80 и 443"
  fi
}

setup_config() {
  mkdir -p "$CONFIG_DIR" "$DATA_DIR"
  chown "$APP_USER:$APP_USER" "$DATA_DIR"
  chmod 700 "$DATA_DIR"
  if [[ -f "$CONFIG_FILE" ]]; then
    warn "Конфиг уже существует: $CONFIG_FILE"
    read -r -p "Перезаписать его? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || return
  fi

  local token secret users base_url api_key model schema domain
  read -r -s -p "Telegram bot token: " token; printf '\n'
  read -r -s -p "Webhook secret (Enter = сгенерировать): " secret; printf '\n'
  [[ -n "$secret" ]] || secret="$(openssl rand -hex 24)"
  read -r -p "Разрешённые Telegram user_id (через запятую, Enter = все): " users
  read -r -p "URL OpenAI-compatible API [https://api.openai.com/v1]: " base_url
  base_url="${base_url:-https://api.openai.com/v1}"
  read -r -s -p "API key (только ключ, без Bearer): " api_key; printf '\n'
  read -r -p "ID модели (например gpt-4o-mini): " model
  read -r -p "JSON Schema mode [auto recommended/strict, auto]: " schema
  schema="${schema:-auto}"
  read -r -p "Домен с A-записью на этот VPS: " domain

  [[ -n "$token" ]] || fail "Telegram token не может быть пустым"
  [[ -n "$api_key" ]] || fail "API key не может быть пустым"
  [[ -n "$model" ]] || fail "ID модели не может быть пустым"
  [[ "$users" =~ ^([0-9]+(,[0-9]+)*)?$ ]] || fail "user_id должны быть числами через запятую"
  [[ "$schema" == "auto" || "$schema" == "strict" ]] || fail "Допустимо auto или strict"
  valid_domain "$domain" || fail "Некорректный домен: $domain"

  (
    umask 077
    {
      write_env_line TELEGRAM_TOKEN "$token"
      write_env_line WEBHOOK_SECRET "$secret"
      write_env_line ALLOWED_USERS "$users"
      write_env_line MAX_REQUESTS_PER_DAY "20"
      write_env_line LLM_BASE_URL "$base_url"
      write_env_line LLM_API_KEY "$api_key"
      write_env_line LLM_MODEL "$model"
      write_env_line LLM_STRUCTURED_OUTPUT "$schema"
      write_env_line AI_TIMEOUT "20"
      write_env_line DATABASE_PATH "$DATA_DIR/calories.sqlite3"
      write_env_line BOT_DOMAIN "$domain"
    } > "$CONFIG_FILE"
  )
  chmod 600 "$CONFIG_FILE"
  log "Конфиг сохранён вне git: $CONFIG_FILE"
}

sync_code() {
  [[ "$SOURCE_DIR" != "$APP_CODE_DIR" ]] || \
    fail "Запускай deploy из git-клона, а не из $APP_CODE_DIR"
  mkdir -p "$APP_CODE_DIR" "$APP_DIR"
  rsync -a --delete --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    "$SOURCE_DIR/" "$APP_CODE_DIR/"
  chown -R "$APP_USER:$APP_USER" "$APP_CODE_DIR"
  [[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r "$APP_CODE_DIR/requirements.txt"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

configure_systemd() {
  install -m 644 "$APP_CODE_DIR/systemd/${APP_NAME}.service" "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable "$APP_NAME"
}

configure_caddy() {
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  valid_domain "${BOT_DOMAIN:-}" || fail "BOT_DOMAIN в конфиге некорректен"
  # Не заменяем основной Caddyfile: на VPS могут быть чужие сайты. Добавляем
  # единственный include один раз и ведём отдельный сайт-файл бота.
  mkdir -p "$CADDY_SITE_DIR"
  if ! grep -Fq '/etc/caddy/sites-enabled/*' "$CADDY_FILE"; then
    cp -n "$CADDY_FILE" "${CADDY_FILE}.before-${APP_NAME}" || true
    printf '\n# Managed site includes\nimport /etc/caddy/sites-enabled/*\n' >> "$CADDY_FILE"
  fi
  sed "s|__BOT_DOMAIN__|${BOT_DOMAIN}|g" "$APP_CODE_DIR/Caddyfile.template" > "$CADDY_SITE_FILE"
  chmod 644 "$CADDY_SITE_FILE"
  caddy validate --config "$CADDY_FILE" --adapter caddyfile
  systemctl enable --now caddy
  systemctl reload caddy
}

backup_database() {
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  [[ -f "${DATABASE_PATH:-}" ]] || return
  local backup_dir="${DATA_DIR}/backups"
  local backup_file="${backup_dir}/calories-$(date +%Y%m%d-%H%M%S).sqlite3"
  install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$backup_dir"
  sqlite3 "$DATABASE_PATH" ".backup '$backup_file'"
  chown "$APP_USER:$APP_USER" "$backup_file"
  log "Резервная копия SQLite: $backup_file"
}

wait_for_https() {
  # Проверяем фактический HTTPS маршрут до того, как Telegram начнёт слать
  # updates. Если DNS/firewall не готовы, deploy завершится с понятной точкой.
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  local url="https://${BOT_DOMAIN:-}/healthz"
  local attempt
  for attempt in {1..24}; do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 10 "$url" >/dev/null; then
      log "HTTPS доступен: $url"
      return
    fi
    sleep 5
  done
  fail "HTTPS не поднялся за 2 минуты. Проверь A/AAAA DNS, внешний firewall (80/443) и: journalctl -u caddy -n 100"
}

register_webhook() {
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  local webhook_url="https://${BOT_DOMAIN}/telegram/webhook" result ok
  result="$(curl --fail-with-body --silent --show-error \
    --data-urlencode "url=${webhook_url}" --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook")" || fail "Telegram setWebhook не выполнился"
  ok="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok", False))' <<<"$result")"
  [[ "$ok" == "True" ]] || fail "Telegram отклонил webhook: $result"
  log "Webhook зарегистрирован: $webhook_url"
}

deploy() {
  [[ -f "$CONFIG_FILE" ]] || fail "Нет конфига. Сначала: sudo ./deploy.sh setup"
  check_runtime
  ensure_firewall
  backup_database
  sync_code
  configure_systemd
  configure_caddy
  systemctl restart "$APP_NAME"
  systemctl --no-pager --full status "$APP_NAME"
  wait_for_https
  register_webhook
  log "Готово. Логи: journalctl -u ${APP_NAME} -f"
}

COMMAND="${1:-deploy}"
case "$COMMAND" in
  setup)
    require_root
    id "$APP_USER" &>/dev/null || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
    ensure_dependencies
    setup_config
    deploy
    ;;
  deploy)
    require_root
    id "$APP_USER" &>/dev/null || fail "Сначала выполни: sudo ./deploy.sh setup"
    deploy
    ;;
  status)
    require_root
    systemctl --no-pager status "$APP_NAME"
    ;;
  *) fail "Использование: sudo ./deploy.sh {setup|deploy|status}" ;;
esac
