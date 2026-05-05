#!/bin/bash
set -e

# =============================================================================
# Calories Bot — скрипт деплоя в Yandex Cloud
#
# Архитектура: синхронная одна функция.
# Webhook → функция → AI Studio → YDB → ответ → 200 Telegram.
# Типичная длительность вызова: 5-20 сек (модель отвечает <15 сек).
#
# Использование:
#   1. Заполни переменные ниже
#   2. chmod +x deploy.sh
#   3. ./deploy.sh
# =============================================================================

# -----------------------------------------------------------------------------
# ЗАПОЛНИ ЭТИ ПЕРЕМЕННЫЕ
# -----------------------------------------------------------------------------

TELEGRAM_TOKEN=""          # Токен от @BotFather
YC_API_KEY=""              # API ключ из Yandex AI Studio
AI_AGENT_ID=""             # ID агента из AI Studio
YDB_ENDPOINT=""            # grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=""            # /ru-central1/b1g.../etn...
ALLOWED_USERS=""           # Твой Telegram user_id (через запятую если несколько)
MAX_REQUESTS_PER_DAY="20"
WEBHOOK_SECRET=""          # Любая строка для верификации webhook

GATEWAY_URL=""             # https://xxx.apigw.yandexcloud.net

# Сервисный аккаунт функции — нужен для доступа к YDB и AI Studio.
# Должен иметь роли: ydb.editor, ai.languageModels.user
SERVICE_ACCOUNT_NAME="calories-agent-sa"

# -----------------------------------------------------------------------------
# Настройки функции
# -----------------------------------------------------------------------------

FUNCTION_NAME="calories-bot"
RUNTIME="python312"
ENTRYPOINT="index.handler"
MEMORY="256m"
TIMEOUT="120s"             # AI обычно <15с, но даём запас на ретраи и сеть

# -----------------------------------------------------------------------------
# Вспомогательные
# -----------------------------------------------------------------------------

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
fail() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
step() { echo -e "\n${GREEN}━━━ $1 ━━━${NC}"; }

# -----------------------------------------------------------------------------
# Шаг 1: Проверки
# -----------------------------------------------------------------------------

step "Проверка окружения"

command -v yc  >/dev/null 2>&1 || fail "yc CLI не найден"
command -v zip >/dev/null 2>&1 || fail "zip не найден"

[ -f "index.py" ]         || fail "index.py не найден"
[ -f "requirements.txt" ] || fail "requirements.txt не найден"

[ -z "$TELEGRAM_TOKEN" ]  && fail "TELEGRAM_TOKEN пустой"
[ -z "$YC_API_KEY" ]      && fail "YC_API_KEY пустой"
[ -z "$AI_AGENT_ID" ]     && fail "AI_AGENT_ID пустой"
[ -z "$YDB_ENDPOINT" ]    && fail "YDB_ENDPOINT пустой"
[ -z "$YDB_DATABASE" ]    && fail "YDB_DATABASE пустой"
[ -z "$WEBHOOK_SECRET" ]  && fail "WEBHOOK_SECRET пустой"
[ -z "$GATEWAY_URL" ]     && fail "GATEWAY_URL пустой"
[ -z "$ALLOWED_USERS" ]   && warn "ALLOWED_USERS пустой — бот открыт для всех"

if echo "$YDB_ENDPOINT" | grep -q "?database="; then
    fail "YDB_ENDPOINT не должен содержать '?database=' — это отдельная переменная"
fi

log "Все проверки пройдены"

# -----------------------------------------------------------------------------
# Шаг 2: Сервисный аккаунт
# -----------------------------------------------------------------------------

step "Сервисный аккаунт"

FOLDER_ID=$(yc config get folder-id)

if ! yc iam service-account get --name "$SERVICE_ACCOUNT_NAME" &>/dev/null; then
    yc iam service-account create --name "$SERVICE_ACCOUNT_NAME"
    log "Создан сервисный аккаунт $SERVICE_ACCOUNT_NAME"
else
    log "Сервисный аккаунт $SERVICE_ACCOUNT_NAME уже существует"
fi

SA_ID=$(yc iam service-account get --name "$SERVICE_ACCOUNT_NAME" --format json \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Назначаем роли (idempotent)
for ROLE in ydb.editor ai.languageModels.user; do
    yc resource-manager folder add-access-binding \
        --id "$FOLDER_ID" \
        --role "$ROLE" \
        --service-account-id "$SA_ID" 2>/dev/null || true
done
log "Роли назначены: ydb.editor, ai.languageModels.user"

# -----------------------------------------------------------------------------
# Шаг 3: Сборка архива
# -----------------------------------------------------------------------------

step "Сборка архива"

rm -f function.zip
zip -j function.zip index.py requirements.txt
log "function.zip собран ($(du -sh function.zip | cut -f1))"

# -----------------------------------------------------------------------------
# Шаг 4: Деплой функции (с привязкой SA)
# -----------------------------------------------------------------------------

step "Деплой Cloud Function"

yc serverless function version create \
  --function-name "$FUNCTION_NAME" \
  --runtime "$RUNTIME" \
  --entrypoint "$ENTRYPOINT" \
  --memory "$MEMORY" \
  --execution-timeout "$TIMEOUT" \
  --source-path function.zip \
  --service-account-id "$SA_ID" \
  --environment "TELEGRAM_TOKEN=${TELEGRAM_TOKEN},YC_API_KEY=${YC_API_KEY},AI_AGENT_ID=${AI_AGENT_ID},YDB_ENDPOINT=${YDB_ENDPOINT},YDB_DATABASE=${YDB_DATABASE},ALLOWED_USERS=${ALLOWED_USERS},MAX_REQUESTS_PER_DAY=${MAX_REQUESTS_PER_DAY},WEBHOOK_SECRET=${WEBHOOK_SECRET}"

log "Версия функции задеплоена"

# -----------------------------------------------------------------------------
# Шаг 5: Публичный доступ
# -----------------------------------------------------------------------------

step "Доступ"

yc serverless function allow-unauthenticated-invoke --name "$FUNCTION_NAME" 2>/dev/null || true
log "Публичный доступ открыт"

# -----------------------------------------------------------------------------
# Шаг 6: Webhook
# -----------------------------------------------------------------------------

step "Регистрация Telegram webhook"

WEBHOOK_RESULT=$(curl -s \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${GATEWAY_URL}&secret_token=${WEBHOOK_SECRET}")

OK=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok',''))" 2>/dev/null || echo "")

if [ "$OK" = "True" ]; then
    log "Webhook зарегистрирован: $GATEWAY_URL"
else
    DESC=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('description',''))" 2>/dev/null || echo "ошибка")
    warn "Не удалось: $DESC"
fi

step "Статус webhook"

curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo" | python3 -c "
import sys, json
r = json.load(sys.stdin).get('result', {})
print('  URL:             ', r.get('url', '—'))
print('  Pending updates: ', r.get('pending_update_count', 0))
print('  Last error:      ', r.get('last_error_message', '—'))
" 2>/dev/null || warn "Не удалось получить статус"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Готово. Напиши боту в Telegram."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Логи в реальном времени:"
echo "  yc logging read --group-name default --limit 30 --follow"
