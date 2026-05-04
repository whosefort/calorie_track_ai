#!/bin/bash
set -e

# =============================================================================
# Calories Bot — скрипт деплоя асинхронной архитектуры в Yandex Cloud
#
# Архитектура:
#   Telegram → API Gateway → Function A (webhook.py) → YMQ → Function B (worker.py)
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
YDB_ENDPOINT=""            # Эндпоинт YDB, например: grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=""            # Путь к БД, например: /ru-central1/b1g.../etn...
ALLOWED_USERS=""           # Telegram user_id через запятую: 123,456
MAX_REQUESTS_PER_DAY="20"
WEBHOOK_SECRET=""          # Любая строка-секрет для защиты webhook

# URL твоего API Gateway (консоль: API Gateway → твой шлюз → Обзор → Адрес шлюза)
GATEWAY_URL=""

# Статический ключ сервисного аккаунта для доступа к YMQ
# Создаётся в консоли: IAM → Сервисные аккаунты → [аккаунт] → Статические ключи доступа → Создать
YMQ_KEY_ID=""
YMQ_SECRET_KEY=""

# ID сервисного аккаунта — нужен для создания триггера YMQ → Function B
# Найти: IAM → Сервисные аккаунты → [аккаунт] → скопировать ID
SA_ID=""

# -----------------------------------------------------------------------------
# Настройки (менять не нужно)
# -----------------------------------------------------------------------------

QUEUE_NAME="calories-bot-queue"
FUNCTION_A_NAME="calories-bot"
FUNCTION_B_NAME="calories-bot-worker"
RUNTIME="python312"
MEMORY="256m"
TIMEOUT_A="15s"    # Function A: только YDB + очередь, без AI
TIMEOUT_B="120s"   # Function B: AI агент может отвечать долго

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
command -v zip >/dev/null 2>&1 || fail "zip не найден."

[ -f "webhook.py" ]       || fail "Файл webhook.py не найден."
[ -f "worker.py" ]        || fail "Файл worker.py не найден."
[ -f "common.py" ]        || fail "Файл common.py не найден."
[ -f "requirements.txt" ] || fail "Файл requirements.txt не найден."

[ -z "$TELEGRAM_TOKEN" ]  && fail "TELEGRAM_TOKEN не заполнен."
[ -z "$YC_API_KEY" ]      && fail "YC_API_KEY не заполнен."
[ -z "$AI_AGENT_ID" ]     && fail "AI_AGENT_ID не заполнен."
[ -z "$YDB_ENDPOINT" ]    && fail "YDB_ENDPOINT не заполнен."
[ -z "$YDB_DATABASE" ]    && fail "YDB_DATABASE не заполнен."
[ -z "$WEBHOOK_SECRET" ]  && fail "WEBHOOK_SECRET не заполнен."
[ -z "$GATEWAY_URL" ]     && fail "GATEWAY_URL не заполнен."
[ -z "$YMQ_KEY_ID" ]      && fail "YMQ_KEY_ID не заполнен. Создай статический ключ сервисного аккаунта."
[ -z "$YMQ_SECRET_KEY" ]  && fail "YMQ_SECRET_KEY не заполнен."
[ -z "$SA_ID" ]           && fail "SA_ID не заполнен. Нужен для создания триггера."
[ -z "$ALLOWED_USERS" ]   && warn "ALLOWED_USERS не заполнен — бот будет открыт для всех."

if echo "$YDB_ENDPOINT" | grep -q "?database="; then
    fail "YDB_ENDPOINT не должен содержать '?database='. Это отдельная переменная YDB_DATABASE."
fi

log "Все проверки пройдены"

# -----------------------------------------------------------------------------
# Шаг 2: Создание / получение очереди YMQ
# -----------------------------------------------------------------------------

step "Очередь Yandex Message Queue"

echo "Создаю очередь '$QUEUE_NAME' (если уже существует — пропускаю)..."
yc message-queue create-queue \
    --name "$QUEUE_NAME" \
    --visibility-timeout 30 \
    --message-retention-period 3600 \
    2>/dev/null && log "Очередь создана" || warn "Очередь уже существует или ошибка создания"

echo "Получаю URL очереди..."
YMQ_QUEUE_URL=$(yc message-queue get-queue-url \
    --name "$QUEUE_NAME" \
    --format json 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('QueueUrl',''))" 2>/dev/null || echo "")

if [ -z "$YMQ_QUEUE_URL" ]; then
    fail "Не удалось получить URL очереди. Проверь что очередь '$QUEUE_NAME' существует и у аккаунта есть права."
fi

log "URL очереди: $YMQ_QUEUE_URL"

# Получаем ARN очереди для триггера (нужен yc serverless trigger)
QUEUE_ID=$(yc message-queue get-queue-attributes \
    --queue-url "$YMQ_QUEUE_URL" \
    --format json 2>/dev/null \
    | python3 -c "
import sys, json
attrs = json.load(sys.stdin).get('attributes', {})
arn = attrs.get('QueueArn', '')
print(arn)
" 2>/dev/null || echo "")

if [ -z "$QUEUE_ID" ]; then
    warn "Не удалось получить ARN очереди — триггер нужно будет создать вручную."
fi

# -----------------------------------------------------------------------------
# Шаг 3: Сборка архивов
# -----------------------------------------------------------------------------

step "Сборка архивов"

rm -f function_a.zip function_b.zip

zip -j function_a.zip webhook.py common.py requirements.txt
SIZE_A=$(du -sh function_a.zip | cut -f1)
log "function_a.zip ($SIZE_A) — webhook + common"

zip -j function_b.zip worker.py common.py requirements.txt
SIZE_B=$(du -sh function_b.zip | cut -f1)
log "function_b.zip ($SIZE_B) — worker + common"

# -----------------------------------------------------------------------------
# Шаг 4: Деплой Function A (webhook)
# -----------------------------------------------------------------------------

step "Деплой Function A: $FUNCTION_A_NAME (webhook)"

yc serverless function version create \
  --function-name "$FUNCTION_A_NAME" \
  --runtime "$RUNTIME" \
  --entrypoint "webhook.handler" \
  --memory "$MEMORY" \
  --execution-timeout "$TIMEOUT_A" \
  --source-path function_a.zip \
  --environment "TELEGRAM_TOKEN=${TELEGRAM_TOKEN},YC_API_KEY=${YC_API_KEY},YDB_ENDPOINT=${YDB_ENDPOINT},YDB_DATABASE=${YDB_DATABASE},ALLOWED_USERS=${ALLOWED_USERS},MAX_REQUESTS_PER_DAY=${MAX_REQUESTS_PER_DAY},WEBHOOK_SECRET=${WEBHOOK_SECRET},YMQ_QUEUE_URL=${YMQ_QUEUE_URL},YMQ_KEY_ID=${YMQ_KEY_ID},YMQ_SECRET_KEY=${YMQ_SECRET_KEY}"

log "Function A задеплоена"

# -----------------------------------------------------------------------------
# Шаг 5: Деплой Function B (worker)
# -----------------------------------------------------------------------------

step "Деплой Function B: $FUNCTION_B_NAME (worker)"

yc serverless function version create \
  --function-name "$FUNCTION_B_NAME" \
  --runtime "$RUNTIME" \
  --entrypoint "worker.handler" \
  --memory "$MEMORY" \
  --execution-timeout "$TIMEOUT_B" \
  --source-path function_b.zip \
  --environment "TELEGRAM_TOKEN=${TELEGRAM_TOKEN},YC_API_KEY=${YC_API_KEY},AI_AGENT_ID=${AI_AGENT_ID},YDB_ENDPOINT=${YDB_ENDPOINT},YDB_DATABASE=${YDB_DATABASE},ALLOWED_USERS=${ALLOWED_USERS},MAX_REQUESTS_PER_DAY=${MAX_REQUESTS_PER_DAY}"

log "Function B задеплоена"

# -----------------------------------------------------------------------------
# Шаг 6: Публичный доступ для Function A
# -----------------------------------------------------------------------------

step "Настройка доступа"

yc serverless function allow-unauthenticated-invoke --name "$FUNCTION_A_NAME" 2>/dev/null || true
log "Публичный доступ для $FUNCTION_A_NAME открыт"

# Function B вызывается только через триггер — публичный доступ не нужен

# -----------------------------------------------------------------------------
# Шаг 7: Создание / обновление триггера YMQ → Function B
# -----------------------------------------------------------------------------

step "Триггер YMQ → $FUNCTION_B_NAME"

TRIGGER_NAME="calories-bot-worker-trigger"

if [ -n "$QUEUE_ID" ]; then
    # Удаляем старый триггер, если есть (игнорируем ошибку если не существует)
    yc serverless trigger delete --name "$TRIGGER_NAME" 2>/dev/null && \
        warn "Старый триггер удалён" || true

    yc serverless trigger create message-queue \
        --name "$TRIGGER_NAME" \
        --queue "$QUEUE_ID" \
        --queue-service-account-id "$SA_ID" \
        --invoke-function-name "$FUNCTION_B_NAME" \
        --invoke-function-service-account-id "$SA_ID" \
        --batch-size 1 \
        --batch-cutoff 10s

    log "Триггер '$TRIGGER_NAME' создан"
else
    warn "Пропускаю создание триггера — ARN очереди не получен."
    echo ""
    echo "  Создай триггер вручную в консоли:"
    echo "  Serverless → Триггеры → Создать триггер"
    echo "  Тип: Message Queue, очередь: $QUEUE_NAME"
    echo "  Функция: $FUNCTION_B_NAME, сервисный аккаунт: SA_ID=$SA_ID"
fi

# -----------------------------------------------------------------------------
# Шаг 8: Регистрация Telegram webhook
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
echo "Архитектура:"
echo "  Telegram → API Gateway → $FUNCTION_A_NAME (webhook, до ${TIMEOUT_A})"
echo "              ↓ YMQ: $QUEUE_NAME"
echo "           $FUNCTION_B_NAME (worker, AI, до ${TIMEOUT_B})"
echo ""
echo "Логи:"
echo "  Function A:  yc logging read --group-name default --filter 'resource_id=\"'$(yc serverless function get --name $FUNCTION_A_NAME --format json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','<id>'))"):\"' --limit 20 --follow"
echo "  Function B:  yc logging read --group-name default --filter 'resource_id=\"'$(yc serverless function get --name $FUNCTION_B_NAME --format json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','<id>'))"):\"' --limit 20 --follow"
echo ""
echo "Или проще — смотри все логи:"
echo "  yc logging read --group-name default --limit 30 --follow"
echo ""
