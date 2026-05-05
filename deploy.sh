#!/bin/bash
set -e

# =============================================================================
# Calories Bot — скрипт деплоя в Yandex Cloud
#
# Архитектура: одна функция с асинхронным самовызовом.
# Webhook-вызов возвращает 200 Telegram за ~1 сек, AI-вызов выполняется
# в фоне через ?integration=async и может работать до 300 сек.
#
# Использование:
#   1. Заполни переменные ниже
#   2. chmod +x deploy.sh
#   3. ./deploy.sh
# =============================================================================

# -----------------------------------------------------------------------------
# ЗАПОЛНИ ЭТИ ПЕРЕМЕННЫЕ
# -----------------------------------------------------------------------------

TELEGRAM_TOKEN=""          # Токен от @BotFather, например: 123456789:ABC-DEF...
YC_API_KEY=""              # API ключ из Yandex AI Studio
AI_AGENT_ID=""             # ID агента из AI Studio, например: fvtojhah0j4dlf0tfhdo
YDB_ENDPOINT=""            # Эндпоинт YDB, например: grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=""            # Путь к БД, например: /ru-central1/b1gf9k2b72hlkr0je1f9/etn7sqa790lp3g3mo0b7
ALLOWED_USERS=""           # Твой Telegram user_id (узнай у @userinfobot). Несколько через запятую: 123,456
MAX_REQUESTS_PER_DAY="20"  # Лимит AI-запросов на пользователя в день
WEBHOOK_SECRET=""          # Любая строка-секрет для защиты webhook, например: my-secret-42

# URL твоего API Gateway (из консоли: API Gateway → твой шлюз → Обзор → Адрес шлюза)
GATEWAY_URL=""             # например: https://d5dm9nhm5ph0p2q878sk.628pfjdx.apigw.yandexcloud.net

# Имя сервисного аккаунта, привязанного к функции.
# Он должен иметь роль serverless.functions.invoker — без этого самовызов не сработает.
# Если аккаунта ещё нет, скрипт создаст его и назначит нужные роли.
SERVICE_ACCOUNT_NAME="calories-bot-sa"

# -----------------------------------------------------------------------------
# Настройки функции (менять не нужно)
# -----------------------------------------------------------------------------

FUNCTION_NAME="calories-bot"
RUNTIME="python312"
ENTRYPOINT="index.handler"
MEMORY="256m"
TIMEOUT="300s"   # Webhook-ветка возвращает ответ за ~1 сек, AI-ветка может работать до 300 сек

# -----------------------------------------------------------------------------
# Вспомогательные функции
# -----------------------------------------------------------------------------

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
fail() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
step() { echo -e "\n${GREEN}━━━ $1 ━━━${NC}"; }

# -----------------------------------------------------------------------------
# Шаг 1: Проверки
# -----------------------------------------------------------------------------

step "Проверка окружения"

command -v yc  >/dev/null 2>&1 || fail "yc CLI не найден. Установи: https://cloud.yandex.ru/docs/cli/quickstart"
command -v zip >/dev/null 2>&1 || fail "zip не найден. Установи: brew install zip (Mac) или apt install zip (Linux)"

[ -f "index.py" ]         || fail "Файл index.py не найден. Запусти скрипт из папки с проектом."
[ -f "requirements.txt" ] || fail "Файл requirements.txt не найден."

[ -z "$TELEGRAM_TOKEN" ]  && fail "TELEGRAM_TOKEN не заполнен."
[ -z "$YC_API_KEY" ]      && fail "YC_API_KEY не заполнен."
[ -z "$AI_AGENT_ID" ]     && fail "AI_AGENT_ID не заполнен."
[ -z "$YDB_ENDPOINT" ]    && fail "YDB_ENDPOINT не заполнен."
[ -z "$YDB_DATABASE" ]    && fail "YDB_DATABASE не заполнен."
[ -z "$WEBHOOK_SECRET" ]  && fail "WEBHOOK_SECRET не заполнен. Придумай любую строку-секрет."
[ -z "$GATEWAY_URL" ]     && fail "GATEWAY_URL не заполнен. Найди в консоли: API Gateway → Обзор → Адрес шлюза."
[ -z "$ALLOWED_USERS" ]   && warn "ALLOWED_USERS не заполнен — бот будет открыт для всех пользователей."

if echo "$YDB_ENDPOINT" | grep -q "?database="; then
    fail "YDB_ENDPOINT не должен содержать '?database='. Это отдельная переменная YDB_DATABASE."
fi

log "Все проверки пройдены"

# -----------------------------------------------------------------------------
# Шаг 2: Сервисный аккаунт
# -----------------------------------------------------------------------------

step "Сервисный аккаунт"

FOLDER_ID=$(yc config get folder-id)

# Создаём аккаунт, если не существует
if ! yc iam service-account get --name "$SERVICE_ACCOUNT_NAME" &>/dev/null; then
    echo "Создаю сервисный аккаунт '$SERVICE_ACCOUNT_NAME'..."
    yc iam service-account create --name "$SERVICE_ACCOUNT_NAME"
    log "Сервисный аккаунт создан"
else
    log "Сервисный аккаунт '$SERVICE_ACCOUNT_NAME' уже существует"
fi

SA_ID=$(yc iam service-account get --name "$SERVICE_ACCOUNT_NAME" --format json \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Назначаем роли (idempotent — повторное назначение безвредно)
echo "Назначаю роли сервисному аккаунту..."
yc resource-manager folder add-access-binding \
    --id "$FOLDER_ID" \
    --role serverless.functions.invoker \
    --service-account-id "$SA_ID" 2>/dev/null || true

yc resource-manager folder add-access-binding \
    --id "$FOLDER_ID" \
    --role ydb.editor \
    --service-account-id "$SA_ID" 2>/dev/null || true

log "Роли назначены (serverless.functions.invoker, ydb.editor)"

# -----------------------------------------------------------------------------
# Шаг 3: Сборка архива
# -----------------------------------------------------------------------------

step "Сборка архива"

rm -f function.zip
zip -j function.zip index.py requirements.txt
SIZE=$(du -sh function.zip | cut -f1)
log "Архив собран: function.zip ($SIZE)"

# -----------------------------------------------------------------------------
# Шаг 4: Деплой функции
# -----------------------------------------------------------------------------

step "Деплой Cloud Function"

echo "Загружаю код в Yandex Cloud..."

yc serverless function version create \
  --function-name "$FUNCTION_NAME" \
  --runtime "$RUNTIME" \
  --entrypoint "$ENTRYPOINT" \
  --memory "$MEMORY" \
  --execution-timeout "$TIMEOUT" \
  --source-path function.zip \
  --service-account-id "$SA_ID" \
  --environment "TELEGRAM_TOKEN=${TELEGRAM_TOKEN},YC_API_KEY=${YC_API_KEY},AI_AGENT_ID=${AI_AGENT_ID},YDB_ENDPOINT=${YDB_ENDPOINT},YDB_DATABASE=${YDB_DATABASE},ALLOWED_USERS=${ALLOWED_USERS},MAX_REQUESTS_PER_DAY=${MAX_REQUESTS_PER_DAY},WEBHOOK_SECRET=${WEBHOOK_SECRET}"

log "Новая версия функции задеплоена"

# -----------------------------------------------------------------------------
# Шаг 5: Публичный доступ
# -----------------------------------------------------------------------------

step "Настройка доступа"

yc serverless function allow-unauthenticated-invoke --name "$FUNCTION_NAME" 2>/dev/null || true
log "Публичный доступ открыт"

# -----------------------------------------------------------------------------
# Шаг 6: Регистрация webhook
# -----------------------------------------------------------------------------

step "Регистрация Telegram webhook"

WEBHOOK_RESULT=$(curl -s \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${GATEWAY_URL}&secret_token=${WEBHOOK_SECRET}")

OK=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok',''))" 2>/dev/null || echo "")

if [ "$OK" = "True" ]; then
    log "Webhook зарегистрирован: $GATEWAY_URL"
else
    DESC=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('description',''))" 2>/dev/null || echo "неизвестная ошибка")
    warn "Ошибка регистрации webhook: $DESC"
fi

# -----------------------------------------------------------------------------
# Шаг 7: Проверка webhook
# -----------------------------------------------------------------------------

step "Статус webhook"

curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo" | python3 -c "
import sys, json
r = json.load(sys.stdin).get('result', {})
print('  URL:             ', r.get('url', 'не задан'))
print('  Pending updates: ', r.get('pending_update_count', 0))
print('  Последняя ошибка:', r.get('last_error_message', 'нет'))
" 2>/dev/null || warn "Не удалось получить информацию о webhook"

# -----------------------------------------------------------------------------
# Итог
# -----------------------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Деплой завершён!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Что делать дальше:"
echo "  1. Напиши боту /start в Telegram"
echo "  2. Если не отвечает — проверь логи:"
echo "     yc logging read --group-name default --limit 20 --follow"
echo "  3. При повторном деплое просто запусти ./deploy.sh снова"
echo ""
