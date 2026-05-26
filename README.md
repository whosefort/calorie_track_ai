# Calories Bot — Telegram бот для подсчёта калорий

Бот считает калории и БЖУ по описанию еды в свободной форме.
Работает на Yandex Cloud: Cloud Functions + AI Studio + YDB.

---

## Как это работает

```
Ты пишешь боту "съел гречку 200г и курицу"
    → Telegram отправляет запрос в API Gateway
    → Cloud Function обрабатывает запрос
    → AI агент считает калории и БЖУ
    → Результат сохраняется в YDB
    → Бот отвечает в Telegram
```

---

## Структура проекта

```
calories-bot/
├── index.py           — код бота
├── requirements.txt   — зависимости Python
├── deploy.sh          — скрипт деплоя (заполни переменные)
└── README.md          — эта инструкция
```

---

## Первоначальная настройка (один раз)

### Шаг 1 — Установи Yandex Cloud CLI

```bash
curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash
```

Перезапусти терминал, затем:

```bash
yc init
```

Проверь:
```bash
yc config list
```

---

### Шаг 2 — Создай Telegram бота

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot`
2. Придумай имя и username
3. Сохрани токен — это `TELEGRAM_TOKEN`
4. Свой `user_id` узнай у [@userinfobot](https://t.me/userinfobot) — это `ALLOWED_USERS`

---

### Шаг 3 — Создай AI агента в Yandex AI Studio

1. Зайди в [console.yandex.cloud](https://console.yandex.cloud) → **AI Studio**
2. Создай нового агента
3. Вставь системный промпт (см. раздел «Промпт агента» ниже)
4. Включи **инструмент поиска** (веб-поиск)
5. Сохрани — скопируй **ID агента** → это `AI_AGENT_ID`
6. Создай **API ключ** в настройках AI Studio → это `YC_API_KEY`

---

### Шаг 4 — Создай базу данных YDB

1. Консоль → **Managed Service for YDB** → **Создать базу данных**
2. Тип: **Serverless**, дай имя `calories-db`
3. После создания → вкладка **Обзор**:
   - **Эндпоинт** (начинается с `grpcs://`) → `YDB_ENDPOINT`
   - **Путь к базе данных** (начинается с `/ru-central1/`) → `YDB_DATABASE`

> ⚠️ Эндпоинт и путь — это разные поля. Не копируй строку целиком с `?database=`.

---

### Шаг 5 — Создай таблицы в YDB

YDB → твоя БД → **Навигация** → **Новый запрос**. Выполни два запроса:

```sql
CREATE TABLE calories_log (
    user_id     Int64,
    record_id   Utf8,
    ts          Int64,
    date_utc    Utf8,
    user_text   Utf8,
    ai_json     Utf8,
    kcal        Double,
    protein_g   Double,
    fat_g       Double,
    carb_g      Double,
    PRIMARY KEY (user_id, record_id)
);
```

```sql
CREATE TABLE pending_rewrite (
    user_id  Int64,
    date_utc Utf8,
    ts       Int64,
    PRIMARY KEY (user_id)
);
```

```sql
CREATE TABLE pending_meal (
    user_id   Int64,
    meal_type Utf8,
    ts        Int64,
    PRIMARY KEY (user_id)
);
```

---

### Шаг 6 — Создай сервисный аккаунт

1. Консоль → **IAM** → **Сервисные аккаунты** → **Создать**
2. Имя: `calories-agent-sa`
3. Роли:
   - `ydb.editor` — для записи/чтения в YDB
   - `ai.languageModels.user` — для вызова AI Studio агента

> ⚠️ Сервисный аккаунт нужно **привязать к функции** (Cloud Functions → твоя функция → новая версия → поле "Сервисный аккаунт"). При каждом создании новой версии этот выбор нужно делать заново — он не наследуется.

---

### Шаг 7 — Создай Cloud Function

1. Консоль → **Cloud Functions** → **Создать функцию**
2. Имя: `calories-bot`
3. Сервисный аккаунт: `calories-agent-sa`
4. Код загрузим через `deploy.sh`

---

### Шаг 8 — Создай API Gateway

1. Консоль → **API Gateway** → **Создать API шлюз**
2. Имя: `calories-bot-gateway`
3. Спецификация (замени ID):

```yaml
openapi: "3.0.0"
info:
  title: calories-bot-gateway
  version: "1.0.0"
paths:
  /:
    post:
      x-yc-apigateway-integration:
        type: cloud_functions
        function_id: ВАШ_ID_ФУНКЦИИ
        service_account_id: ВАШ_ID_СЕРВИСНОГО_АККАУНТА
```

4. После создания скопируй **Адрес шлюза** → это `GATEWAY_URL`

---

## Деплой

### Заполни deploy.sh

Открой `deploy.sh` и заполни все переменные в начале файла:

```bash
TELEGRAM_TOKEN=""     # токен от @BotFather
YC_API_KEY=""         # API ключ из AI Studio
AI_AGENT_ID=""        # ID агента из AI Studio
YDB_ENDPOINT=""       # grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=""       # /ru-central1/b1g.../etn...
ALLOWED_USERS=""      # твой Telegram user_id
WEBHOOK_SECRET=""     # любая строка, например: my-secret-42
GATEWAY_URL=""        # https://xxx.apigw.yandexcloud.net
MAX_REQUESTS_PER_DAY="20"
```

### Запусти

```bash
chmod +x deploy.sh
./deploy.sh
```

Скрипт сам:
- Проверит все переменные и файлы
- Соберёт zip-архив
- Задеплоит новую версию функции
- Откроет публичный доступ
- Зарегистрирует Telegram webhook с секретом
- Покажет статус webhook

При каждом обновлении кода — просто `./deploy.sh`.

---

## Команды бота

| Команда | Что делает |
|---|---|
| `/start` | Приветствие и список команд |
| `/add` | Добавить приём пищи — выбор типа кнопками (завтрак / обед / ужин / перекус) |
| `/today` | Калории за сегодня |
| `/history` | Последние 10 записей |
| `/day 2026-05-01` | Записи за конкретный день |
| `/rewrite` | Переписать рацион — выбор даты кнопками |
| `/rewrite гречка 200г` | Переписать рацион за сегодня |
| `/rewrite 2026-05-01 гречка 200г` | Переписать рацион за конкретный день |
| `/cancel` | Отменить ожидающее действие |
| _(любой текст)_ | Подсчёт калорий и БЖУ (meal_type определяет агент) |

---

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `TELEGRAM_TOKEN` | да | Токен бота от @BotFather |
| `YC_API_KEY` | да | API ключ Yandex AI Studio |
| `AI_AGENT_ID` | да | ID агента в AI Studio |
| `YDB_ENDPOINT` | да | gRPC эндпоинт YDB (без `?database=`) |
| `YDB_DATABASE` | да | Путь к базе данных YDB |
| `WEBHOOK_SECRET` | да | Секрет для верификации запросов от Telegram |
| `ALLOWED_USERS` | нет | user_id через запятую. Если пусто — бот открыт для всех |
| `MAX_REQUESTS_PER_DAY` | нет | Лимит AI-запросов в день (по умолчанию: 20) |

---

## Промпт агента

Вставь в AI Studio → твой агент → System prompt:

```
Ты — калькулятор питания. Твой ответ — ТОЛЬКО валидный JSON. Никакого текста до или после JSON. Никаких пояснений. Никаких ```json блоков.

ФОРМАТ ОТВЕТА (строго):
{"items":[{"meal_type":"breakfast","name":"Яйцо (3 шт)","weight_g":180,"kcal":234,"protein_g":18.9,"fat_g":15.9,"carb_g":1.8,"portion_note":"~60г каждое"}],"total_kcal":234,"total":{"protein_g":18.9,"fat_g":15.9,"carb_g":1.8},"tip":"Хороший завтрак."}

ПОЛЯ:
- meal_type: "breakfast" / "lunch" / "dinner" / "snack" / null
- name: название блюда/продукта
- weight_g: вес в граммах (число)
- kcal, protein_g, fat_g, carb_g: числа (не строки, не null)
- portion_note: короткая заметка о порции или источнике, либо ""
- tip: одна фраза-комментарий к рациону, либо ""

ИСТОЧНИКИ ДАННЫХ (приоритет):
1. Официальный сайт сети (McDonald's, KFC, Burger King, Subway, Вкусно и точка, Додо Пицца и др.) — для блюд сетей
2. Данные производителя — для брендовых продуктов (Activia, Lay's и др.)
3. USDA FoodData Central или табличные значения — для остального

ПРАВИЛА РАСЧЁТА:
- Считай на указанную граммовку, не на 100г
- Если вес не указан — используй стандартную порцию (суп 300мл, сок 200мл, яйцо 60г). Добавь * к названию
- Несколько штук (3 яйца) → один item с суммарным весом: "Яйцо (3 шт)", weight_g: 180
- kcal округляй до целых, БЖУ до одного знака
- Если данные приблизительные — укажи это в tip
- Поиск для обычных продуктов (крупы, мясо, овощи) не нужен

ПРИЁМЫ ПИЩИ:
- Если пользователь указывает приём — проставляй meal_type всем items этого приёма
- Продукты без явного указания приёма → meal_type: null

ЕСЛИ СООБЩЕНИЕ НЕ ПРО ЕДУ:
{"items":[],"total_kcal":0,"total":{"protein_g":0,"fat_g":0,"carb_g":0},"tip":"Напиши что ел — я посчитаю калории."}

ЗАПРЕЩЕНО:
- Любой текст вне JSON (в т.ч. после использования поиска)
- Markdown внутри строк JSON (**, *, #)
- Комментарии внутри JSON
- null или строки в числовых полях (weight_g, kcal, protein_g, fat_g, carb_g)
- Невалидный JSON
```

---

## Чек-лист после деплоя (если бот молчит)

Если задеплоил, но бот ничего не отвечает — проверяй по списку:

1. **Сервисный аккаунт привязан к актуальной версии функции?**
   Cloud Functions → calories-bot → последняя версия → поле «Сервисный аккаунт». Должен стоять `calories-agent-sa`. **Это самая частая причина «тишины»** — каждая новая версия теряет SA если не указать его явно.

2. **У сервисного аккаунта есть нужные роли?**
   IAM → calories-agent-sa → Права доступа в каталоге. Должны быть:
   - `ydb.editor`
   - `ai.languageModels.user`

3. **Все переменные окружения заполнены в актуальной версии?**
   Cloud Functions → версия → Переменные окружения. Должны быть все 8: `TELEGRAM_TOKEN`, `YC_API_KEY`, `AI_AGENT_ID`, `YDB_ENDPOINT`, `YDB_DATABASE`, `ALLOWED_USERS`, `MAX_REQUESTS_PER_DAY`, `WEBHOOK_SECRET`.

4. **YDB_ENDPOINT не содержит `?database=`?**
   Если содержит — это две разные переменные, разнеси.

5. **API Gateway указывает на правильную функцию?**
   API Gateway → шлюз → спецификация → проверь `function_id` и `service_account_id`.

6. **Webhook зарегистрирован с правильным секретом?**
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
   Поле `url` должно совпадать с `GATEWAY_URL`. Поле `last_error_message` должно быть пусто или null.

7. **Логи показывают вызов?**
   Cloud Logging → группа default. Должны быть строки `=== START ===` от твоего user_id.
   Если пусто — webhook не приходит до функции (проверь Gateway/секрет).
   Если есть, но без `[INFO]` — где-то ошибка в самом начале (см. логи).

8. **AI Studio API ключ актуален?**
   AI Studio → API-ключи. Если истёк — пересоздай и обнови `YC_API_KEY` в переменных функции (создаст новую версию — не забудь снова привязать SA).

9. **YDB-таблицы созданы?**
   YDB → твоя БД → Навигация → должны быть `calories_log` и `pending_rewrite`.

---

## Полезные команды

```bash
# Смотреть логи в реальном времени
yc logging read --group-name default --limit 50 --follow

# Статус webhook
curl "https://api.telegram.org/botТОКЕН/getWebhookInfo"

# Перерегистрировать webhook вручную
curl "https://api.telegram.org/botТОКЕН/setWebhook?url=GATEWAY_URL&secret_token=СЕКРЕТ"

# Удалить webhook
curl "https://api.telegram.org/botТОКЕН/deleteWebhook"

# Список версий функции
yc serverless function version list --function-name calories-bot
```

---

## Стоимость (личное использование, ~100 запросов/месяц)

| Сервис | Бесплатная квота | Итого |
|---|---|---|
| Cloud Functions | 1 млн вызовов/мес | 0 ₽ |
| Serverless YDB | 1 млн RU/мес | 0 ₽ |
| AI Studio | зависит от модели | до ~50 ₽ |
| API Gateway | 1 млн запросов/мес | 0 ₽ |
| **Итого** | | **0 ₽/мес** |
