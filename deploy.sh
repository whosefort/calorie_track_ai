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

ensure_base_dependencies() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates openssl sqlite3
}

ensure_webhook_dependencies() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y caddy
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
  # Только webhook требует входящие порты для Caddy/ACME и Telegram.
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

  local token mode secret users base_url api_key model schema domain
  read -r -s -p "Telegram bot token: " token; printf '\n'
  read -r -p "Режим Telegram [polling без домена / webhook с доменом, polling]: " mode
  mode="${mode:-polling}"
  [[ "$mode" == "webhook" || "$mode" == "polling" ]] || fail "Допустимо webhook или polling"
  if [[ "$mode" == "webhook" ]]; then
    read -r -s -p "Webhook secret (Enter = сгенерировать): " secret; printf '\n'
    [[ -n "$secret" ]] || secret="$(openssl rand -hex 24)"
  fi
  read -r -p "Разрешённые Telegram user_id (через запятую, Enter = все): " users
  read -r -p "URL API [Gemini: https://generativelanguage.googleapis.com/v1beta/openai/]: " base_url
  base_url="${base_url:-https://generativelanguage.googleapis.com/v1beta/openai/}"
  read -r -s -p "API key (Gemini по умолчанию; только ключ, без Bearer): " api_key; printf '\n'
  read -r -p "ID модели [gemini-3.5-flash-lite]: " model
  model="${model:-gemini-3.5-flash-lite}"
  read -r -p "JSON Schema mode [auto recommended/strict, auto]: " schema
  schema="${schema:-auto}"
  if [[ "$mode" == "webhook" ]]; then
    read -r -p "Домен с A/AAAA-записью на этот VPS: " domain
  fi

  [[ -n "$token" ]] || fail "Telegram token не может быть пустым"
  [[ -n "$api_key" ]] || fail "API key не может быть пустым"
  [[ "$users" =~ ^([0-9]+(,[0-9]+)*)?$ ]] || fail "user_id должны быть числами через запятую"
  [[ "$schema" == "auto" || "$schema" == "strict" ]] || fail "Допустимо auto или strict"
  [[ "$mode" != "webhook" ]] || valid_domain "$domain" || fail "Некорректный домен: $domain"

  (
    umask 077
    {
      write_env_line TELEGRAM_TOKEN "$token"
      write_env_line TELEGRAM_MODE "$mode"
      [[ "$mode" != "webhook" ]] || write_env_line WEBHOOK_SECRET "$secret"
      write_env_line ALLOWED_USERS "$users"
      write_env_line MAX_REQUESTS_PER_DAY "20"
      write_env_line LLM_BASE_URL "$base_url"
      write_env_line LLM_API_KEY "$api_key"
      write_env_line LLM_MODEL "$model"
      write_env_line LLM_STRUCTURED_OUTPUT "$schema"
      write_env_line AI_TIMEOUT "20"
      write_env_line DATABASE_PATH "$DATA_DIR/calories.sqlite3"
      [[ "$mode" != "webhook" ]] || write_env_line BOT_DOMAIN "$domain"
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

verify_telegram_token() {
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  local result ok
  result="$(curl --fail-with-body --silent --show-error \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMe")" || fail "Telegram token не принят"
  ok="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok", False))' <<<"$result")"
  [[ "$ok" == "True" ]] || fail "Telegram token не принят: $result"
  log "Telegram token проверен"
}

verify_default_gemini_config() {
  # Проверяем бесплатным запросом именно пару ключ+модель, которую setup
  # предлагает по умолчанию. Для другого OpenAI-compatible API не гадаем.
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  [[ "${LLM_BASE_URL:-}" == "https://generativelanguage.googleapis.com/v1beta/openai/" ]] || return
  local result
  result="$(curl --fail-with-body --silent --show-error \
    -H "Authorization: Bearer ${LLM_API_KEY}" \
    "${LLM_BASE_URL}models/${LLM_MODEL}")" || \
      fail "Gemini не принял ключ или модель '${LLM_MODEL}'"
  log "Gemini key и модель проверены: ${LLM_MODEL}"
}

disable_webhook() {
  # Telegram не разрешает getUpdates, пока активен webhook. Очередь не сбрасываем.
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  local result ok
  result="$(curl --fail-with-body --silent --show-error \
    --data-urlencode "drop_pending_updates=false" \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/deleteWebhook")" || fail "Telegram deleteWebhook не выполнился"
  ok="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok", False))' <<<"$result")"
  [[ "$ok" == "True" ]] || fail "Telegram не отключил webhook: $result"
  log "Webhook отключён; бот получает сообщения через polling"
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
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  local mode="${TELEGRAM_MODE:-webhook}"
  [[ "$mode" == "webhook" || "$mode" == "polling" ]] || fail "TELEGRAM_MODE: webhook или polling"
  check_runtime
  backup_database
  sync_code
  configure_systemd
  verify_telegram_token
  verify_default_gemini_config
  if [[ "$mode" == "webhook" ]]; then
    ensure_webhook_dependencies
    ensure_firewall
    configure_caddy
  else
    disable_webhook
  fi
  systemctl restart "$APP_NAME"
  systemctl --no-pager --full status "$APP_NAME"
  if [[ "$mode" == "webhook" ]]; then
    wait_for_https
    register_webhook
  fi
  log "Готово. Логи: journalctl -u ${APP_NAME} -f"
}

COMMAND="${1:-deploy}"
case "$COMMAND" in
  setup)
    require_root
    id "$APP_USER" &>/dev/null || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
    ensure_base_dependencies
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
