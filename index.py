"""
Telegram-бот для подсчёта калорий на Yandex Cloud Functions.

Архитектура (синхронная):
    Telegram → API Gateway → Cloud Function → Yandex AI Studio → YDB → Telegram

Необходимые переменные окружения:
    TELEGRAM_TOKEN        — токен от @BotFather
    YC_API_KEY            — API-ключ из Yandex AI Studio
    AI_AGENT_ID           — ID агента из AI Studio
    YDB_ENDPOINT          — например: grpcs://ydb.serverless.yandexcloud.net:2135
    YDB_DATABASE          — путь к БД, например /ru-central1/.../...
    ALLOWED_USERS         — Telegram user_id через запятую
    MAX_REQUESTS_PER_DAY  — лимит на пользователя в день (по умолчанию 20)
    WEBHOOK_SECRET        — секрет для верификации webhook

Сервисный аккаунт функции должен иметь роли:
    - ydb.editor                (запись/чтение в YDB)
    - ai.languageModels.user    (вызов AI Studio)
"""

import json
import os
import sys
import uuid
import logging
import requests
import openai
from datetime import datetime, timezone, timedelta

import ydb
import ydb.iam


# ============================================================================
# ЛОГИРОВАНИЕ
# ============================================================================
# В Yandex Cloud Functions Python-runtime может предварительно настроить
# свои хендлеры — тогда logging.basicConfig() становится no-op и логи
# пропадают. Полностью пересоздаём root-логгер с явным StreamHandler.

def _setup_logging() -> logging.Logger:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return root

logger = _setup_logging()


# ============================================================================
# КОНФИГ
# ============================================================================

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_API    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "")

AI_API_KEY      = os.getenv("YC_API_KEY", "")
AI_AGENT_ID     = os.getenv("AI_AGENT_ID", "")

YDB_ENDPOINT    = os.getenv("YDB_ENDPOINT", "")
YDB_DATABASE    = os.getenv("YDB_DATABASE", "")

ALLOWED_USERS = set(
    int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
)
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "20"))

# Таймауты:
# - AI агент в интерфейсе отвечает <15с. 25с — буфер, при этом весь вызов
#   укладывается в окно ожидания Telegram-webhook (~60с), иначе Telegram
#   рвёт соединение ("Connection timed out") и ответ до пользователя не доходит.
# - Telegram HTTP вызовы: 10с (на самом деле меньше типично).
# - YDB connect: 10с.
AI_TIMEOUT       = 25
TG_TIMEOUT       = 10
YDB_CONNECT      = 10

MAX_MESSAGE_LENGTH = 500
MAX_DAY_ENTRIES    = 20


# Валидируем критичные env-переменные при импорте — лучше упасть на старте
# с понятным сообщением, чем ловить странные ошибки во время выполнения.
def _validate_env():
    missing = []
    for name, value in [
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
        ("YC_API_KEY",     AI_API_KEY),
        ("AI_AGENT_ID",    AI_AGENT_ID),
        ("YDB_ENDPOINT",   YDB_ENDPOINT),
        ("YDB_DATABASE",   YDB_DATABASE),
    ]:
        if not value:
            missing.append(name)
    if missing:
        logger.error(f"Missing required env vars: {missing}")
    if "?database=" in YDB_ENDPOINT:
        logger.error("YDB_ENDPOINT contains '?database=' — это должно быть в YDB_DATABASE")

_validate_env()


# ============================================================================
# AI КЛИЕНТ
# ============================================================================

ai_client = openai.OpenAI(
    api_key=AI_API_KEY,
    base_url="https://ai.api.cloud.yandex.net/v1",
    # max_retries=0: свой ретрай уже есть в call_ai. Дефолтный SDK-ретрай (2)
    # умножал таймаут (3 попытки × AI_TIMEOUT) и подвешивал функцию на минуты —
    # Telegram не дожидался ответа и писал "Connection timed out".
    max_retries=0,
    timeout=AI_TIMEOUT,
)


# ============================================================================
# YDB (синглтон с ленивой инициализацией)
# ============================================================================

_ydb_driver = None
_ydb_pool   = None

def _get_pool() -> ydb.SessionPool:
    global _ydb_driver, _ydb_pool
    if _ydb_pool is not None:
        return _ydb_pool

    creds  = ydb.iam.MetadataUrlCredentials()
    config = ydb.DriverConfig(
        endpoint=YDB_ENDPOINT,
        database=YDB_DATABASE,
        credentials=creds,
    )
    _ydb_driver = ydb.Driver(config)
    _ydb_driver.wait(fail_fast=True, timeout=YDB_CONNECT)
    _ydb_pool = ydb.SessionPool(_ydb_driver)
    return _ydb_pool


def _ydb_exec(query: str, params: dict = None) -> list:
    """Готовит и выполняет YQL-запрос в одной транзакции с авто-ретраем.

    Снимает boilerplate (pool → session → prepare → transaction → commit →
    retry), который раньше дублировался в каждой функции работы с БД.
    Возвращает список result-set'ов; $-параметры передаются через params.
    """
    pool = _get_pool()

    def _op(session):
        prepared = session.prepare(query)
        return session.transaction().execute(prepared, params or {}, commit_tx=True)

    return pool.retry_operation_sync(_op)


def save_record(user_id: int, user_text: str, ai_json: str, totals: dict,
                date_utc: str = None) -> None:
    now = datetime.now(timezone.utc)
    record_id = str(uuid.uuid4())
    if date_utc is None:
        date_utc = now.strftime("%Y-%m-%d")

    _ydb_exec("""
        DECLARE $user_id   AS Int64;
        DECLARE $record_id AS Utf8;
        DECLARE $ts        AS Int64;
        DECLARE $date_utc  AS Utf8;
        DECLARE $user_text AS Utf8;
        DECLARE $ai_json   AS Utf8;
        DECLARE $kcal      AS Double;
        DECLARE $protein_g AS Double;
        DECLARE $fat_g     AS Double;
        DECLARE $carb_g    AS Double;

        UPSERT INTO calories_log
            (user_id, record_id, ts, date_utc, user_text, ai_json,
             kcal, protein_g, fat_g, carb_g)
        VALUES
            ($user_id, $record_id, $ts, $date_utc, $user_text, $ai_json,
             $kcal, $protein_g, $fat_g, $carb_g);
    """, {
        "$user_id":   user_id,
        "$record_id": record_id,
        "$ts":        int(now.timestamp()),
        "$date_utc":  date_utc,
        "$user_text": user_text[:500],
        "$ai_json":   ai_json,
        "$kcal":      float(totals.get("kcal", 0)),
        "$protein_g": float(totals.get("protein_g", 0)),
        "$fat_g":     float(totals.get("fat_g", 0)),
        "$carb_g":    float(totals.get("carb_g", 0)),
    })
    logger.info(f"saved record {record_id} user={user_id} date={date_utc}")


def get_history(user_id: int, date_utc: str = None, limit: int = 10) -> list:
    if date_utc:
        rs = _ydb_exec("""
            DECLARE $user_id  AS Int64;
            DECLARE $date_utc AS Utf8;
            DECLARE $limit    AS Uint64;

            SELECT record_id, ts, date_utc, user_text,
                   kcal, protein_g, fat_g, carb_g
            FROM calories_log
            WHERE user_id = $user_id AND date_utc = $date_utc
            ORDER BY ts
            LIMIT $limit;
        """, {
            "$user_id":  user_id,
            "$date_utc": date_utc,
            "$limit":    MAX_DAY_ENTRIES,
        })
    else:
        rs = _ydb_exec("""
            DECLARE $user_id AS Int64;
            DECLARE $limit   AS Uint64;

            SELECT record_id, ts, date_utc, user_text,
                   kcal, protein_g, fat_g, carb_g
            FROM calories_log
            WHERE user_id = $user_id
            ORDER BY ts DESC
            LIMIT $limit;
        """, {
            "$user_id": user_id,
            "$limit":   limit,
        })
    return [{
        "record_id": row.record_id,
        "ts":        row.ts,
        "date_utc":  row.date_utc,
        "user_text": row.user_text,
        "kcal":      row.kcal,
        "protein_g": row.protein_g,
        "fat_g":     row.fat_g,
        "carb_g":    row.carb_g,
    } for row in rs[0].rows]


def count_day(user_id: int, date_utc: str) -> int:
    """Число записей за день одним COUNT(*) — без вытягивания строк целиком."""
    rs = _ydb_exec("""
        DECLARE $user_id  AS Int64;
        DECLARE $date_utc AS Utf8;
        SELECT COUNT(*) AS cnt FROM calories_log
        WHERE user_id = $user_id AND date_utc = $date_utc;
    """, {"$user_id": user_id, "$date_utc": date_utc})
    return rs[0].rows[0].cnt if rs[0].rows else 0


def delete_day(user_id: int, date_utc: str) -> int:
    """Удаляет все записи за день одним DELETE. Возвращает сколько удалил."""
    deleted = count_day(user_id, date_utc)
    if deleted == 0:
        return 0
    _ydb_exec("""
        DECLARE $user_id  AS Int64;
        DECLARE $date_utc AS Utf8;
        DELETE FROM calories_log
        WHERE user_id = $user_id AND date_utc = $date_utc;
    """, {"$user_id": user_id, "$date_utc": date_utc})
    logger.info(f"deleted {deleted} records user={user_id} date={date_utc}")
    return deleted


# ---------------------------------------------------------------------------
# pending_state — единое временное состояние ожидаемого ввода пользователя.
# Заменяет прежние таблицы pending_rewrite и pending_meal: одна строка на
# пользователя (PK user_id) → одна проверка вместо двух SELECT'ов на сообщение.
#   kind='rewrite' → payload = date_utc (перезапись дня),    TTL 300с
#   kind='meal'    → payload = meal_type (приём пищи /add),   TTL 600с
# UPSERT перезаписывает строку целиком, поэтому отдельный clear перед
# сменой ожидания не нужен.
# ---------------------------------------------------------------------------

_PENDING_TTL = {"rewrite": 300, "meal": 600}


def save_pending(user_id: int, kind: str, payload: str) -> None:
    try:
        _ydb_exec("""
            DECLARE $user_id AS Int64;
            DECLARE $kind    AS Utf8;
            DECLARE $payload AS Utf8;
            DECLARE $ts      AS Int64;
            UPSERT INTO pending_state (user_id, kind, payload, ts)
            VALUES ($user_id, $kind, $payload, $ts);
        """, {
            "$user_id": user_id,
            "$kind":    kind,
            "$payload": payload,
            "$ts":      int(datetime.now(timezone.utc).timestamp()),
        })
    except Exception as e:
        logger.error(f"save_pending: {e}")


def get_pending(user_id: int):
    """Возвращает (kind, payload) активного ожидания или None.
    TTL зависит от kind (см. _PENDING_TTL)."""
    try:
        rs = _ydb_exec("""
            DECLARE $user_id AS Int64;
            SELECT kind, payload, ts FROM pending_state
            WHERE user_id = $user_id;
        """, {"$user_id": user_id})
        rows = rs[0].rows
        if not rows:
            return None
        row = rows[0]
        ttl = _PENDING_TTL.get(row.kind, 300)
        if int(datetime.now(timezone.utc).timestamp()) - row.ts > ttl:
            return None
        return (row.kind, row.payload)
    except Exception as e:
        logger.error(f"get_pending: {e}")
        return None


def clear_pending(user_id: int) -> None:
    try:
        _ydb_exec("""
            DECLARE $user_id AS Int64;
            DELETE FROM pending_state WHERE user_id = $user_id;
        """, {"$user_id": user_id})
    except Exception as e:
        logger.error(f"clear_pending: {e}")


# ============================================================================
# AI
# ============================================================================

def _to_number(v) -> float:
    """Конвертирует значение в float. Поддерживает строки ('185', '13.0').
    Возвращает None если конвертация невозможна."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except (ValueError, TypeError):
            return None
    return None


def _coerce_item_numbers(item: dict) -> bool:
    """Приводит числовые поля item к float in-place. Возвращает False если
    хотя бы одно поле не конвертируется."""
    for f in ("weight_g", "kcal", "protein_g", "fat_g", "carb_g"):
        v = _to_number(item.get(f))
        if v is None:
            logger.error(f"item field '{f}' cannot convert: {item.get(f)!r}")
            return False
        item[f] = v
    return True


def validate_ai_response(data: dict) -> bool:
    if not isinstance(data, dict):
        logger.error("AI response is not a dict")
        return False
    if "items" not in data or not isinstance(data["items"], list):
        logger.error(f"AI response missing 'items' list, keys={list(data.keys())}")
        return False
    if "total_kcal" not in data:
        logger.error(f"AI response missing 'total_kcal', keys={list(data.keys())}")
        return False
    if "total" not in data or not isinstance(data["total"], dict):
        logger.error(f"AI response missing 'total' dict, keys={list(data.keys())}")
        return False
    required = {"name", "weight_g", "kcal", "protein_g", "fat_g", "carb_g"}
    for i, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            logger.error(f"item[{i}] is not a dict: {item!r}")
            return False
        missing = required - item.keys()
        if missing:
            logger.error(f"item[{i}] missing fields: {missing}, item={item}")
            return False
        if not _coerce_item_numbers(item):
            return False
    return True


def _extract_json(raw: str) -> str:
    """Извлекает JSON-строку из сырого ответа модели.
    Обрабатывает: чистый JSON, ```json...```, текст до/после объекта."""
    content = raw.strip()

    # Убираем ```json ... ``` блоки
    if "```" in content:
        parts = content.split("```")
        if len(parts) >= 3:
            block = parts[1]
            if block.startswith("json"):
                block = block[4:]
            content = block.strip()
        else:
            content = content.replace("```json", "").replace("```", "").strip()

    # Если есть текст до/после JSON — вырезаем объект
    if not content.startswith("{"):
        start = content.find("{")
        end   = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            content = content[start:end + 1]
        else:
            raise json.JSONDecodeError("no JSON object found", content, 0)

    return content


def _call_ai_once(user_message: str) -> dict:
    """Один вызов AI без ретрая. Возбуждает JSONDecodeError или ValueError."""
    response = ai_client.responses.create(
        prompt={"id": AI_AGENT_ID},
        input=user_message,
        timeout=AI_TIMEOUT,
    )
    raw = response.output_text
    logger.info(f"AI raw ({len(raw)} chars): {raw[:600]}")

    try:
        content = _extract_json(raw)
    except json.JSONDecodeError:
        logger.error(f"AI: JSON не найден в ответе: {raw[:500]}")
        raise

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error(f"AI non-JSON (len={len(content)}): {content[:500]}")
        raise

    if not validate_ai_response(data):
        try:
            full = json.dumps(data, ensure_ascii=False)
        except Exception:
            full = str(data)
        logger.error(f"AI invalid structure (len={len(full)}): {full[:1000]}")
        raise ValueError("AI response invalid")

    return data


# Префикс для ретрая — явно требует чистый JSON
_RETRY_PREFIX = (
    "ВАЖНО: твой ответ должен содержать ТОЛЬКО валидный JSON-объект. "
    "Никакого текста до или после. Никаких пояснений. Только JSON.\n\n"
)

def call_ai(user_message: str) -> dict:
    """Вызывает AI агента с автоматическим ретраем при ошибке парсинга."""
    logger.info(f"AI call: {user_message[:80]!r}")
    try:
        return _call_ai_once(user_message)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"AI attempt 1 failed ({type(e).__name__}), retrying with explicit JSON prompt")
        # На ретрае явно напоминаем модели про JSON-only формат
        return _call_ai_once(_RETRY_PREFIX + user_message)


def _safe_call_ai(chat_id: int, text: str, context: str = ""):
    """call_ai с единой обработкой ошибок. Возвращает dict, либо None —
    в этом случае пользователю уже отправлено сообщение об ошибке."""
    try:
        return call_ai(text)
    except json.JSONDecodeError:
        tg_send_mk(chat_id, "Не смог распознать ответ агента. Переформулируй.")
    except ValueError:
        tg_send_mk(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
    except Exception as e:
        where = f" in {context}" if context else ""
        logger.error(f"AI error{where}: {e}", exc_info=True)
        tg_send_mk(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
    return None


# ============================================================================
# TELEGRAM
# ============================================================================

# ---------------------------------------------------------------------------
# Постоянная Reply-клавиатура (всегда видна внизу чата)
# ---------------------------------------------------------------------------

BTN_ADD      = "➕ Добавить"       # выбор приёма через inline-кнопки
BTN_DAY_LOG  = "📝 Весь день"      # записать полный рацион за сегодня
BTN_TODAY    = "📊 Сегодня"        # сводка за сегодня
BTN_HISTORY  = "📋 История"        # последние 10 записей
BTN_REWRITE  = "✏️ Переписать"     # переписать рацион за выбранный день

MAIN_KEYBOARD = {
    "keyboard": [
        [BTN_ADD,    BTN_DAY_LOG],
        [BTN_TODAY,  BTN_HISTORY],
        [BTN_REWRITE],
    ],
    "resize_keyboard": True,
    "is_persistent":   True,   # клавиатура остаётся видимой между сообщениями
}

# Все тексты кнопок — для маршрутизации в route_message
_BUTTON_TEXTS = {BTN_ADD, BTN_DAY_LOG, BTN_TODAY, BTN_HISTORY, BTN_REWRITE}


def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown",
            reply_markup: dict = None) -> None:
    """Шлёт сообщение в Telegram. При ошибке Markdown — автоматический
    fallback на plain text, чтобы пользователь точно получил ответ."""
    if len(text) > 4000:
        text = text[:3990] + "\n…(обрезано)"

    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage",
                          json=payload, timeout=TG_TIMEOUT)
        if r.status_code == 400 and parse_mode:
            logger.warning(f"TG 400 with markdown, retry plain: {r.text[:200]}")
            plain_payload = {"chat_id": chat_id, "text": text}
            if reply_markup:
                plain_payload["reply_markup"] = reply_markup
            r2 = requests.post(f"{TELEGRAM_API}/sendMessage",
                               json=plain_payload, timeout=TG_TIMEOUT)
            if not r2.ok:
                logger.error(f"TG plain retry failed: {r2.status_code} {r2.text[:200]}")
        elif not r.ok:
            logger.error(f"TG send failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"TG send exception: {e}")


def tg_send_mk(chat_id: int, text: str, parse_mode: str = "Markdown") -> None:
    """Шлёт сообщение с постоянной главной клавиатурой."""
    tg_send(chat_id, text, parse_mode=parse_mode, reply_markup=MAIN_KEYBOARD)


def tg_send_keyboard(chat_id: int, text: str, buttons: list) -> None:
    """Шлёт сообщение с inline-клавиатурой (для выбора из вариантов)."""
    payload = {
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   "Markdown",
        "reply_markup": {"inline_keyboard": buttons},
    }
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage",
                      json=payload, timeout=TG_TIMEOUT)
    except Exception as e:
        logger.error(f"TG keyboard exception: {e}")


def tg_answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      json={"callback_query_id": callback_query_id, "text": text},
                      timeout=TG_TIMEOUT)
    except Exception as e:
        logger.error(f"TG callback exception: {e}")


def show_rewrite_keyboard(chat_id: int) -> None:
    now = datetime.now(timezone.utc)
    weekday_ru = {"Mon": "пн", "Tue": "вт", "Wed": "ср", "Thu": "чт",
                  "Fri": "пт", "Sat": "сб", "Sun": "вс"}
    buttons, row = [], []
    for i in range(7):
        day = now - timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        if i == 0:
            label = f"Сегодня {day.strftime('%d.%m')}"
        elif i == 1:
            label = f"Вчера {day.strftime('%d.%m')}"
        else:
            wd = weekday_ru.get(day.strftime("%a"), day.strftime("%a"))
            label = f"{day.strftime('%d.%m')} ({wd})"
        row.append({"text": label, "callback_data": f"rewrite_date:{date_str}"})
        if len(row) == 2 or i == 6:
            buttons.append(row)
            row = []
    tg_send_keyboard(chat_id, "Выбери дату для перезаписи рациона:", buttons)


def handle_rewrite_callback(chat_id: int, user_id: int,
                            callback_query_id: str, date_str: str) -> None:
    tg_answer_callback(callback_query_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yest  = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == today:
        label = f"сегодня ({date_str})"
    elif date_str == yest:
        label = f"вчера ({date_str})"
    else:
        label = date_str
    tg_send(chat_id,
        f"Выбрана дата: *{label}*\n\n"
        f"Напиши что ел в этот день — я пересчитаю и заменю рацион.\n"
        f"Или /cancel чтобы отменить."
    )
    save_pending(user_id, "rewrite", date_str)


def show_add_keyboard(chat_id: int) -> None:
    """Показывает клавиатуру выбора приёма пищи для /add."""
    buttons = [
        [
            {"text": "🌅 Завтрак", "callback_data": "add_meal:breakfast"},
            {"text": "☀️ Обед",   "callback_data": "add_meal:lunch"},
        ],
        [
            {"text": "🌙 Ужин",   "callback_data": "add_meal:dinner"},
            {"text": "🍎 Перекус","callback_data": "add_meal:snack"},
        ],
    ]
    tg_send_keyboard(chat_id, "Выбери приём пищи:", buttons)


def handle_add_meal_callback(chat_id: int, user_id: int,
                             callback_query_id: str, meal_type: str) -> None:
    """Обрабатывает нажатие кнопки приёма пищи — сохраняет pending и просит текст."""
    tg_answer_callback(callback_query_id)
    if meal_type not in MEAL_AI_PREFIX:
        logger.warning(f"unknown meal_type in callback: {meal_type!r}")
        return
    # UPSERT перезапишет любое прежнее ожидание — отдельный clear не нужен
    save_pending(user_id, "meal", meal_type)
    prompt = MEAL_PROMPT_TEXT.get(meal_type, "Пиши что ел:")
    tg_send(chat_id, f"{prompt}\n\n_Или /cancel чтобы отменить_")


# ============================================================================
# ФОРМАТИРОВАНИЕ
# ============================================================================

MEAL_LABELS = {
    "breakfast": "🌅 Завтрак",
    "lunch":     "☀️ Обед",
    "dinner":    "🌙 Ужин",
    "snack":     "🍎 Перекус",
    None:        "🍽 Прочее",
}
MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack", None]

# Префикс, который prepend'ится к запросу пользователя чтобы AI
# проставил нужный meal_type всем items
MEAL_AI_PREFIX = {
    "breakfast": "Это завтрак. ",
    "lunch":     "Это обед. ",
    "dinner":    "Это ужин. ",
    "snack":     "Это перекус. ",
}

# Сообщение-подсказка для каждого приёма после нажатия кнопки
MEAL_PROMPT_TEXT = {
    "breakfast": "Пиши что ел на *завтрак* 🌅",
    "lunch":     "Пиши что ел на *обед* ☀️",
    "dinner":    "Пиши что ел на *ужин* 🌙",
    "snack":     "Пиши что ел на *перекус* 🍎",
}

_DIVIDER = "─" * 22

# Русские названия для человекочитаемых дат
_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
_WEEKDAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}


def _fmt(v) -> str:
    """Форматирует число: убирает лишние .0  (650.0 → '650', 13.5 → '13.5')."""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.1f}"
    except (TypeError, ValueError):
        return str(v)


def _truncate(text: str, limit: int = 70) -> str:
    """Обрезает текст по границе слова, добавляет … если обрезано."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def _friendly_date(date_str: str) -> str:
    """'2026-05-29' → 'Сегодня · 29 мая' / 'Вчера · 28 мая' / 'Ср · 27 мая'."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date_str
    today = datetime.now(timezone.utc).date()
    delta = (today - d).days
    day_month = f"{d.day} {_MONTHS_RU.get(d.month, '')}"
    if delta == 0:
        prefix = "Сегодня"
    elif delta == 1:
        prefix = "Вчера"
    else:
        prefix = _WEEKDAYS_RU.get(d.weekday(), "")
    return f"{prefix} · {day_month}" if prefix else day_month


def _macros_line(kcal, protein_g, fat_g, carb_g) -> str:
    return (
        f"{_fmt(kcal)} ккал  "
        f"Б {_fmt(protein_g)}  "
        f"Ж {_fmt(fat_g)}  "
        f"У {_fmt(carb_g)}"
    )


def _macros_compact(protein_g, fat_g, carb_g) -> str:
    """Компактная строка БЖУ: 'Б 32 · Ж 29 · У 50'."""
    return f"Б {_fmt(protein_g)} · Ж {_fmt(fat_g)} · У {_fmt(carb_g)}"


def format_log_entry(entry: dict, show_date: bool = True) -> str:
    """Карточка одной записи. show_date=False когда дата уже в заголовке дня."""
    head = f"{_fmt(entry['kcal'])} ккал · {_macros_compact(entry['protein_g'], entry['fat_g'], entry['carb_g'])}"
    body = f"_{_truncate(entry['user_text'])}_"
    if show_date:
        return f"*{_friendly_date(entry['date_utc'])}*\n{head}\n{body}"
    return f"• {head}\n  {body}"


def format_ai_response(data: dict) -> str:
    """Форматирует ответ AI в читаемое сообщение для Telegram.

    Формат:
        🌅 Завтрак — 535 ккал
        ──────────────────────
        Ролл с омлетом · 150г
        375 ккал  Б 12  Ж 18  У 40

        ☀️ Обед — 2620 ккал
        ...

        📊 Итого: 3155 ккал
        Б 137  Ж 143  У 341

        💡 tip
    """
    groups: dict = {}
    for item in data.get("items", []):
        groups.setdefault(item.get("meal_type"), []).append(item)

    lines = []

    for meal in MEAL_ORDER:
        if meal not in groups:
            continue
        items = groups[meal]

        # Итого по приёму пищи
        meal_kcal = sum(float(i.get("kcal", 0)) for i in items)
        lines.append(f"*{MEAL_LABELS[meal]} — {_fmt(meal_kcal)} ккал*")
        lines.append(_DIVIDER)

        for item in items:
            # Имя: убираем * (маркер оценённой порции) — он нужен для логики,
            # не для отображения; * в Markdown-тексте сломает форматирование.
            raw_name = item.get("name", "")
            estimated = "*" in raw_name
            display_name = raw_name.replace("*", "").strip()

            weight_line = f"{display_name} · {_fmt(item.get('weight_g', 0))}г"
            # Заметка о порции — только для оценённых (был *)
            if estimated and item.get("portion_note"):
                weight_line += f"  _({item['portion_note']})_"

            lines.append(weight_line)
            lines.append(_macros_line(
                item.get("kcal", 0),
                item.get("protein_g", 0),
                item.get("fat_g", 0),
                item.get("carb_g", 0),
            ))
            lines.append("")  # пустая строка между блюдами

        # Убираем лишнюю пустую строку в конце группы
        if lines and lines[-1] == "":
            lines.pop()
        lines.append("")  # отступ между приёмами пищи

    # Итого за день
    t = data.get("total", {})
    lines.append(
        f"*📊 Итого: {_fmt(data.get('total_kcal', 0))} ккал*\n"
        f"Б {_fmt(t.get('protein_g', 0))}  "
        f"Ж {_fmt(t.get('fat_g', 0))}  "
        f"У {_fmt(t.get('carb_g', 0))}"
    )

    if data.get("tip"):
        lines.append(f"\n💡 {data['tip']}")

    return "\n".join(lines)


def format_day_summary(date_str: str, rows: list) -> str:
    """Сводка за один день: заголовок-дата + список записей + итог.

    Записи не дублируют дату (она в заголовке), текст обрезается аккуратно.
    """
    total_k = sum(r["kcal"] for r in rows)
    total_p = sum(r["protein_g"] for r in rows)
    total_f = sum(r["fat_g"] for r in rows)
    total_c = sum(r["carb_g"] for r in rows)

    lines = [f"📅 *{_friendly_date(date_str)}*", ""]
    for r in rows:
        lines.append(format_log_entry(r, show_date=False))
        lines.append("")
    if len(rows) >= MAX_DAY_ENTRIES:
        lines.append(f"_Показаны первые {MAX_DAY_ENTRIES} записей_")
    lines.append(_DIVIDER)
    lines.append(
        f"*📊 Итого: {_fmt(total_k)} ккал*\n"
        f"{_macros_compact(total_p, total_f, total_c)}"
    )
    return "\n".join(lines)


def format_history(rows: list, max_days: int = 7) -> str:
    """История, сгруппированная по дням. Для каждого дня — итог и число записей.

    Решает проблему 'бардака': несколько записей за день больше не выглядят
    как дубликаты, дни показаны компактными карточками сверху вниз.
    """
    # Группируем по дате, сохраняя порядок (rows приходят ts DESC)
    days: dict = {}
    for r in rows:
        days.setdefault(r["date_utc"], []).append(r)

    # Самые свежие дни сверху
    sorted_dates = sorted(days.keys(), reverse=True)[:max_days]

    lines = ["*📋 История*", ""]
    for date_str in sorted_dates:
        day_rows = days[date_str]
        day_k = sum(r["kcal"] for r in day_rows)
        day_p = sum(r["protein_g"] for r in day_rows)
        day_f = sum(r["fat_g"] for r in day_rows)
        day_c = sum(r["carb_g"] for r in day_rows)

        lines.append(f"*🗓 {_friendly_date(date_str)}*")
        lines.append(f"{_fmt(day_k)} ккал · {_macros_compact(day_p, day_f, day_c)}")
        if len(day_rows) > 1:
            lines.append(f"_{len(day_rows)} записи_")
        lines.append("")

    lines.append("_Нажми «📊 Сегодня» или /day ГГГГ-ММ-ДД для деталей дня_")
    return "\n".join(lines)


# ============================================================================
# БИЗНЕС-ЛОГИКА: подсчёт и перезапись
# ============================================================================

def process_food_message(chat_id: int, user_id: int, text: str,
                         meal_type: str = None) -> None:
    """Считает калории нового приёма пищи и сохраняет в БД.

    meal_type — если передан, prepend'ится к AI-запросу чтобы все items
    получили нужный meal_type. Передаётся когда пользователь выбрал приём
    через /add.
    """
    date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Лимит запросов
    try:
        count = count_day(user_id, date_utc)
    except Exception as e:
        logger.error(f"count_day: {e}", exc_info=True)
        tg_send(chat_id, "Ошибка БД. Попробуй позже.")
        return

    if count >= MAX_REQUESTS_PER_DAY:
        tg_send(chat_id,
            f"Лимит {MAX_REQUESTS_PER_DAY} запросов в день исчерпан.\n"
            f"Используй /rewrite чтобы скорректировать уже добавленное."
        )
        return

    tg_send(chat_id, "Считаю калории...")

    # Добавляем контекст приёма пищи к запросу если выбран через /add
    ai_input = text
    if meal_type and meal_type in MEAL_AI_PREFIX:
        ai_input = MEAL_AI_PREFIX[meal_type] + text
        logger.info(f"meal_type hint: {meal_type!r}")

    data = _safe_call_ai(chat_id, ai_input)
    if data is None:
        return

    tg_send_mk(chat_id, format_ai_response(data))

    try:
        t = data.get("total", {})
        save_record(
            user_id=user_id,
            user_text=text,
            ai_json=json.dumps(data, ensure_ascii=False),
            totals={
                "kcal":      data.get("total_kcal", 0),
                "protein_g": t.get("protein_g", 0),
                "fat_g":     t.get("fat_g", 0),
                "carb_g":    t.get("carb_g", 0),
            },
            date_utc=date_utc,
        )
    except Exception as e:
        logger.error(f"YDB save error: {e}", exc_info=True)
        tg_send_mk(chat_id, "⚠️ Посчитал, но не смог сохранить в историю.")


def process_rewrite(chat_id: int, user_id: int, text: str, date_utc: str) -> None:
    """Перезаписывает рацион за указанный день."""
    if len(text) > MAX_MESSAGE_LENGTH:
        tg_send(chat_id, f"Сообщение длиннее {MAX_MESSAGE_LENGTH} символов.")
        return

    tg_send(chat_id, f"Считаю калории для {date_utc}...")

    data = _safe_call_ai(chat_id, text, context="rewrite")
    if data is None:
        return

    try:
        deleted = delete_day(user_id, date_utc)
        t = data.get("total", {})
        save_record(
            user_id=user_id,
            user_text=text,
            ai_json=json.dumps(data, ensure_ascii=False),
            totals={
                "kcal":      data.get("total_kcal", 0),
                "protein_g": t.get("protein_g", 0),
                "fat_g":     t.get("fat_g", 0),
                "carb_g":    t.get("carb_g", 0),
            },
            date_utc=date_utc,
        )
    except Exception as e:
        logger.error(f"YDB rewrite error: {e}", exc_info=True)
        tg_send_mk(chat_id, "⚠️ Посчитал, но не смог переписать в БД.")
        return

    note = f"\n\n✏️ Рацион за {date_utc} обновлён"
    if deleted > 0:
        note += f" (удалено старых записей: {deleted})"
    tg_send_mk(chat_id, format_ai_response(data) + note)


# ============================================================================
# КОМАНДЫ
# ============================================================================

def handle_start(chat_id: int) -> None:
    tg_send_mk(chat_id, (
        "Привет! Я помогу отслеживать калории.\n\n"
        "Используй кнопки внизу или просто напиши что ел:\n"
        "_«съел гречку 200г и куриную грудку 150г»_\n\n"
        "*Кнопки:*\n"
        "➕ *Добавить* — один приём пищи (завтрак/обед/ужин/перекус)\n"
        "📝 *Весь день* — записать всё что ел сегодня одним сообщением\n"
        "📊 *Сегодня* — сводка за сегодня\n"
        "📋 *История* — последние 10 записей\n"
        "✏️ *Переписать* — исправить рацион за выбранный день\n\n"
        "_/cancel — отменить ожидающее действие_"
    ))


def handle_today(chat_id: int, user_id: int) -> None:
    date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = get_history(user_id, date_utc=date_utc)
    if not rows:
        tg_send_mk(chat_id, "За сегодня записей нет. Расскажи что ел.")
    else:
        tg_send_mk(chat_id, format_day_summary(date_utc, rows))


def handle_history(chat_id: int, user_id: int) -> None:
    # Тянем больше записей чтобы покрыть несколько дней (по 3-5 записей в день)
    rows = get_history(user_id, limit=100)
    if not rows:
        tg_send_mk(chat_id, "История пуста.")
    else:
        tg_send_mk(chat_id, format_history(rows))


def handle_day(chat_id: int, user_id: int, date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        tg_send_mk(chat_id, "Формат даты: ГГГГ-ММ-ДД, например /day 2026-05-01")
        return
    rows = get_history(user_id, date_utc=date_str)
    if not rows:
        tg_send_mk(chat_id, f"За {_friendly_date(date_str)} записей нет.")
    else:
        tg_send_mk(chat_id, format_day_summary(date_str, rows))


# ============================================================================
# РОУТЕР И HANDLER
# ============================================================================

def _get_header_ci(headers: dict, name: str) -> str:
    """Case-insensitive поиск заголовка."""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return ""


def route_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text    = (message.get("text") or "").strip()

    logger.info(f"msg user={user_id} text={text[:80]!r}")

    if not text:
        return

    # Whitelist
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        logger.info(f"blocked user={user_id}")
        tg_send(chat_id, "Нет доступа.")
        return

    # Длина
    if not text.startswith("/") and len(text) > MAX_MESSAGE_LENGTH:
        tg_send(chat_id, f"Сообщение длиннее {MAX_MESSAGE_LENGTH} символов "
                         f"(у тебя {len(text)}).")
        return

    # ── Кнопки главной клавиатуры ─────────────────────────────────────────
    # Проверяем ДО pending-состояний: если пользователь нажал кнопку меню
    # пока ждали ввода — выполняем действие кнопки, pending сбрасывается.
    if text in _BUTTON_TEXTS:
        # Нажатие кнопки отменяет любое прежнее ожидание ввода.
        # BTN_DAY_LOG сразу ставит новое ожидание (UPSERT перезапишет строку),
        # поэтому для него отдельный clear не нужен — у остальных делаем clear.
        if text == BTN_ADD:
            clear_pending(user_id)
            show_add_keyboard(chat_id)
            return

        if text == BTN_DAY_LOG:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            save_pending(user_id, "rewrite", today)
            tg_send_mk(chat_id,
                "Напиши всё что ел сегодня одним сообщением — "
                "я запишу как дневной рацион и заменю старые данные.\n\n"
                "_Или нажми «✏️ Переписать» чтобы выбрать другой день_"
            )
            return

        if text == BTN_TODAY:
            clear_pending(user_id)
            handle_today(chat_id, user_id)
            return

        if text == BTN_HISTORY:
            clear_pending(user_id)
            handle_history(chat_id, user_id)
            return

        if text == BTN_REWRITE:
            clear_pending(user_id)
            show_rewrite_keyboard(chat_id)
            return

    # ── Pending-состояние ─────────────────────────────────────────────────
    # Один SELECT вместо двух: get_pending возвращает (kind, payload).
    if not text.startswith("/"):
        pending = get_pending(user_id)
        if pending:
            kind, payload = pending
            clear_pending(user_id)
            if kind == "rewrite":
                process_rewrite(chat_id, user_id, text, payload)
            else:  # meal
                process_food_message(chat_id, user_id, text, meal_type=payload)
            return

    # ── Команды ───────────────────────────────────────────────────────────
    if text.startswith("/start"):
        handle_start(chat_id)
        return

    if text == "/cancel":
        clear_pending(user_id)
        tg_send_mk(chat_id, "Отменено.")
        return

    if text.startswith("/add"):
        clear_pending(user_id)
        show_add_keyboard(chat_id)
        return

    if text.startswith("/today"):
        handle_today(chat_id, user_id)
        return

    if text.startswith("/history"):
        handle_history(chat_id, user_id)
        return

    if text.startswith("/day"):
        parts = text.split()
        date_str = parts[1] if len(parts) > 1 else ""
        handle_day(chat_id, user_id, date_str)
        return

    if text.startswith("/rewrite"):
        parts = text.split(None, 2)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if len(parts) == 1:
            show_rewrite_keyboard(chat_id)
        elif len(parts) == 2:
            process_rewrite(chat_id, user_id, parts[1], today)
        else:
            try:
                datetime.strptime(parts[1], "%Y-%m-%d")
                process_rewrite(chat_id, user_id, parts[2], parts[1])
            except ValueError:
                process_rewrite(chat_id, user_id, parts[1] + " " + parts[2], today)
        return

    # Обычное сообщение с едой
    process_food_message(chat_id, user_id, text)


def handler(event, context):
    """Точка входа Yandex Cloud Function."""
    try:
        logger.info(f"=== START === keys={list(event.keys())}")

        # Верификация webhook (case-insensitive header lookup)
        if WEBHOOK_SECRET:
            headers = event.get("headers") or {}
            incoming = _get_header_ci(headers, "X-Telegram-Bot-Api-Secret-Token")
            if incoming != WEBHOOK_SECRET:
                logger.warning(f"invalid webhook secret: {incoming[:10]!r}...")
                return {"statusCode": 403, "body": "forbidden"}

        body = event.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)

        # Inline-кнопки
        cb = body.get("callback_query")
        if cb:
            cq_id   = cb["id"]
            user_id = cb["from"]["id"]
            chat_id = cb["message"]["chat"]["id"]
            data    = cb.get("data", "")

            if ALLOWED_USERS and user_id not in ALLOWED_USERS:
                tg_answer_callback(cq_id, "Нет доступа.")
                return {"statusCode": 200, "body": "ok"}

            if data.startswith("rewrite_date:"):
                handle_rewrite_callback(chat_id, user_id, cq_id, data.split(":", 1)[1])
            elif data.startswith("add_meal:"):
                handle_add_meal_callback(chat_id, user_id, cq_id, data.split(":", 1)[1])
            return {"statusCode": 200, "body": "ok"}

        # Обычное сообщение (edited_message игнорируем — избегаем дублей)
        message = body.get("message")
        if message:
            route_message(message)

        logger.info("=== DONE ===")
        return {"statusCode": 200, "body": "ok"}

    except Exception as e:
        logger.error(f"handler error: {e}", exc_info=True)
        # Всегда возвращаем 200 — иначе Telegram будет ретраить и засирать БД дублями
        return {"statusCode": 200, "body": "ok"}
