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
# - AI агент в интерфейсе отвечает <15с. 60с — щедрый буфер.
# - Telegram HTTP вызовы: 10с (на самом деле меньше типично).
# - YDB connect: 10с.
AI_TIMEOUT       = 60
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


def save_record(user_id: int, user_text: str, ai_json: str, totals: dict,
                date_utc: str = None) -> None:
    pool = _get_pool()
    now  = datetime.now(timezone.utc)
    record_id = str(uuid.uuid4())
    if date_utc is None:
        date_utc = now.strftime("%Y-%m-%d")

    def _upsert(session):
        prepared = session.prepare("""
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
        """)
        session.transaction().execute(prepared, {
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
        }, commit_tx=True)

    pool.retry_operation_sync(_upsert)
    logger.info(f"saved record {record_id} user={user_id} date={date_utc}")


def get_history(user_id: int, date_utc: str = None, limit: int = 10) -> list:
    pool = _get_pool()

    def _select(session):
        if date_utc:
            prepared = session.prepare("""
                DECLARE $user_id  AS Int64;
                DECLARE $date_utc AS Utf8;
                DECLARE $limit    AS Uint64;

                SELECT record_id, ts, date_utc, user_text,
                       kcal, protein_g, fat_g, carb_g
                FROM calories_log
                WHERE user_id = $user_id AND date_utc = $date_utc
                ORDER BY ts
                LIMIT $limit;
            """)
            return session.transaction().execute(prepared, {
                "$user_id":  user_id,
                "$date_utc": date_utc,
                "$limit":    MAX_DAY_ENTRIES,
            }, commit_tx=True)
        else:
            prepared = session.prepare("""
                DECLARE $user_id AS Int64;
                DECLARE $limit   AS Uint64;

                SELECT record_id, ts, date_utc, user_text,
                       kcal, protein_g, fat_g, carb_g
                FROM calories_log
                WHERE user_id = $user_id
                ORDER BY ts DESC
                LIMIT $limit;
            """)
            return session.transaction().execute(prepared, {
                "$user_id": user_id,
                "$limit":   limit,
            }, commit_tx=True)

    rs = pool.retry_operation_sync(_select)
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


def delete_day(user_id: int, date_utc: str) -> int:
    pool = _get_pool()
    rows = get_history(user_id, date_utc=date_utc)
    if not rows:
        return 0
    record_ids = [r["record_id"] for r in rows]

    def _delete(session):
        tx = session.transaction()
        prepared = session.prepare("""
            DECLARE $user_id   AS Int64;
            DECLARE $record_id AS Utf8;
            DELETE FROM calories_log
            WHERE user_id = $user_id AND record_id = $record_id;
        """)
        for rid in record_ids:
            tx.execute(prepared, {"$user_id": user_id, "$record_id": rid})
        tx.commit()

    pool.retry_operation_sync(_delete)
    logger.info(f"deleted {len(record_ids)} records user={user_id} date={date_utc}")
    return len(record_ids)


def count_today_requests(user_id: int, date_utc: str) -> int:
    return len(get_history(user_id, date_utc=date_utc, limit=MAX_REQUESTS_PER_DAY + 1))


def save_pending_rewrite(user_id: int, date_str: str) -> None:
    pool = _get_pool()
    def _upsert(session):
        prepared = session.prepare("""
            DECLARE $user_id  AS Int64;
            DECLARE $date_utc AS Utf8;
            DECLARE $ts       AS Int64;
            UPSERT INTO pending_rewrite (user_id, date_utc, ts)
            VALUES ($user_id, $date_utc, $ts);
        """)
        session.transaction().execute(prepared, {
            "$user_id":  user_id,
            "$date_utc": date_str,
            "$ts":       int(datetime.now(timezone.utc).timestamp()),
        }, commit_tx=True)
    try:
        pool.retry_operation_sync(_upsert)
    except Exception as e:
        logger.error(f"save_pending_rewrite: {e}")


def get_pending_rewrite(user_id: int):
    pool = _get_pool()
    def _select(session):
        prepared = session.prepare("""
            DECLARE $user_id AS Int64;
            DECLARE $min_ts  AS Int64;
            SELECT date_utc FROM pending_rewrite
            WHERE user_id = $user_id AND ts > $min_ts;
        """)
        return session.transaction().execute(prepared, {
            "$user_id": user_id,
            "$min_ts":  int(datetime.now(timezone.utc).timestamp()) - 300,
        }, commit_tx=True)
    try:
        rs = pool.retry_operation_sync(_select)
        rows = rs[0].rows
        return rows[0].date_utc if rows else None
    except Exception as e:
        logger.error(f"get_pending_rewrite: {e}")
        return None


def clear_pending_rewrite(user_id: int) -> None:
    pool = _get_pool()
    def _delete(session):
        prepared = session.prepare("""
            DECLARE $user_id AS Int64;
            DELETE FROM pending_rewrite WHERE user_id = $user_id;
        """)
        session.transaction().execute(prepared, {"$user_id": user_id}, commit_tx=True)
    try:
        pool.retry_operation_sync(_delete)
    except Exception as e:
        logger.error(f"clear_pending_rewrite: {e}")


# ---------------------------------------------------------------------------
# pending_meal — временное состояние выбранного приёма пищи через /add
# TTL: 10 минут (600 сек)
# ---------------------------------------------------------------------------

_PENDING_MEAL_TTL = 600  # секунд


def save_pending_meal(user_id: int, meal_type: str) -> None:
    pool = _get_pool()
    def _upsert(session):
        prepared = session.prepare("""
            DECLARE $user_id   AS Int64;
            DECLARE $meal_type AS Utf8;
            DECLARE $ts        AS Int64;
            UPSERT INTO pending_meal (user_id, meal_type, ts)
            VALUES ($user_id, $meal_type, $ts);
        """)
        session.transaction().execute(prepared, {
            "$user_id":   user_id,
            "$meal_type": meal_type,
            "$ts":        int(datetime.now(timezone.utc).timestamp()),
        }, commit_tx=True)
    try:
        pool.retry_operation_sync(_upsert)
    except Exception as e:
        logger.error(f"save_pending_meal: {e}")


def get_pending_meal(user_id: int):
    """Возвращает meal_type если есть активный pending (< 10 мин), иначе None."""
    pool = _get_pool()
    def _select(session):
        prepared = session.prepare("""
            DECLARE $user_id AS Int64;
            DECLARE $min_ts  AS Int64;
            SELECT meal_type FROM pending_meal
            WHERE user_id = $user_id AND ts > $min_ts;
        """)
        return session.transaction().execute(prepared, {
            "$user_id": user_id,
            "$min_ts":  int(datetime.now(timezone.utc).timestamp()) - _PENDING_MEAL_TTL,
        }, commit_tx=True)
    try:
        rs = pool.retry_operation_sync(_select)
        rows = rs[0].rows
        return rows[0].meal_type if rows else None
    except Exception as e:
        logger.error(f"get_pending_meal: {e}")
        return None


def clear_pending_meal(user_id: int) -> None:
    pool = _get_pool()
    def _delete(session):
        prepared = session.prepare("""
            DECLARE $user_id AS Int64;
            DELETE FROM pending_meal WHERE user_id = $user_id;
        """)
        session.transaction().execute(prepared, {"$user_id": user_id}, commit_tx=True)
    try:
        pool.retry_operation_sync(_delete)
    except Exception as e:
        logger.error(f"clear_pending_meal: {e}")


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


# ============================================================================
# TELEGRAM
# ============================================================================

def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown") -> None:
    """Шлёт сообщение в Telegram. При ошибке Markdown — автоматический
    fallback на plain text, чтобы пользователь точно получил ответ."""
    if len(text) > 4000:
        text = text[:3990] + "\n…(обрезано)"

    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage",
                          json=payload, timeout=TG_TIMEOUT)
        if r.status_code == 400 and parse_mode:
            # Скорее всего, проблема с парсингом markdown. Шлём plain.
            logger.warning(f"TG 400 with markdown, retry plain: {r.text[:200]}")
            r2 = requests.post(f"{TELEGRAM_API}/sendMessage",
                               json={"chat_id": chat_id, "text": text},
                               timeout=TG_TIMEOUT)
            if not r2.ok:
                logger.error(f"TG plain retry failed: {r2.status_code} {r2.text[:200]}")
        elif not r.ok:
            logger.error(f"TG send failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"TG send exception: {e}")


def tg_send_keyboard(chat_id: int, text: str, buttons: list) -> None:
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
    save_pending_rewrite(user_id, date_str)


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
    # Сбрасываем rewrite-ожидание если было
    clear_pending_rewrite(user_id)
    save_pending_meal(user_id, meal_type)
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


def _fmt(v) -> str:
    """Форматирует число: убирает лишние .0  (650.0 → '650', 13.5 → '13.5')."""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.1f}"
    except (TypeError, ValueError):
        return str(v)


def _macros_line(kcal, protein_g, fat_g, carb_g) -> str:
    return (
        f"{_fmt(kcal)} ккал  "
        f"Б {_fmt(protein_g)}  "
        f"Ж {_fmt(fat_g)}  "
        f"У {_fmt(carb_g)}"
    )


def format_log_entry(entry: dict) -> str:
    return (
        f"*{entry['date_utc']}*  •  {_fmt(entry['kcal'])} ккал\n"
        f"  Б {_fmt(entry['protein_g'])}  "
        f"Ж {_fmt(entry['fat_g'])}  "
        f"У {_fmt(entry['carb_g'])}\n"
        f"  _{entry['user_text'][:60]}_"
    )


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


def format_day_summary(title: str, rows: list) -> str:
    total_k = sum(r["kcal"] for r in rows)
    total_p = sum(r["protein_g"] for r in rows)
    total_f = sum(r["fat_g"] for r in rows)
    total_c = sum(r["carb_g"] for r in rows)
    lines = [f"*{title}*\n"]
    for r in rows:
        lines.append(format_log_entry(r))
    if len(rows) >= MAX_DAY_ENTRIES:
        lines.append(f"\n_Показаны первые {MAX_DAY_ENTRIES} записей_")
    lines.append(
        f"\n*Итого:* {_fmt(total_k)} ккал  "
        f"Б {_fmt(total_p)}  Ж {_fmt(total_f)}  У {_fmt(total_c)}"
    )
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
        count = count_today_requests(user_id, date_utc)
    except Exception as e:
        logger.error(f"count_today_requests: {e}", exc_info=True)
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

    try:
        data = call_ai(ai_input)
    except json.JSONDecodeError:
        tg_send(chat_id, "Не смог распознать ответ агента. Переформулируй.")
        return
    except ValueError:
        tg_send(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
        return
    except Exception as e:
        logger.error(f"AI error: {e}", exc_info=True)
        tg_send(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
        return

    tg_send(chat_id, format_ai_response(data))

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
        tg_send(chat_id, "⚠️ Посчитал, но не смог сохранить в историю.")


def process_rewrite(chat_id: int, user_id: int, text: str, date_utc: str) -> None:
    """Перезаписывает рацион за указанный день."""
    if len(text) > MAX_MESSAGE_LENGTH:
        tg_send(chat_id, f"Сообщение длиннее {MAX_MESSAGE_LENGTH} символов.")
        return

    tg_send(chat_id, f"Считаю калории для {date_utc}...")

    try:
        data = call_ai(text)
    except json.JSONDecodeError:
        tg_send(chat_id, "Не смог распознать ответ агента. Переформулируй.")
        return
    except ValueError:
        tg_send(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
        return
    except Exception as e:
        logger.error(f"AI error in rewrite: {e}", exc_info=True)
        tg_send(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
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
        tg_send(chat_id, "⚠️ Посчитал, но не смог переписать в БД.")
        return

    note = f"\n\n✏️ Рацион за {date_utc} обновлён"
    if deleted > 0:
        note += f" (удалено старых записей: {deleted})"
    tg_send(chat_id, format_ai_response(data) + note)


# ============================================================================
# КОМАНДЫ
# ============================================================================

def handle_start(chat_id: int) -> None:
    tg_send(chat_id, (
        "Привет! Я помогу отслеживать калории.\n\n"
        "Просто напиши что ел, например:\n"
        "_«съел гречку 200г и куриную грудку 150г»_\n\n"
        "*Команды:*\n"
        "/add — добавить приём пищи (завтрак / обед / ужин / перекус)\n"
        "/today — сводка за сегодня\n"
        "/history — последние 10 записей\n"
        "/day 2026-05-01 — записи за конкретный день\n"
        "/rewrite — переписать рацион (выбор даты кнопками)\n"
        "/rewrite 2026-05-01 гречка 200г — переписать конкретный день\n"
        "/cancel — отменить ожидающее действие"
    ))


def handle_today(chat_id: int, user_id: int) -> None:
    date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = get_history(user_id, date_utc=date_utc)
    if not rows:
        tg_send(chat_id, "За сегодня записей нет. Расскажи что ел.")
    else:
        tg_send(chat_id, format_day_summary(f"Сегодня, {date_utc}", rows))


def handle_history(chat_id: int, user_id: int) -> None:
    rows = get_history(user_id, limit=10)
    if not rows:
        tg_send(chat_id, "История пуста.")
    else:
        lines = ["*Последние записи:*\n"] + [format_log_entry(r) for r in rows]
        tg_send(chat_id, "\n".join(lines))


def handle_day(chat_id: int, user_id: int, date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        tg_send(chat_id, "Формат даты: ГГГГ-ММ-ДД, например /day 2026-05-01")
        return
    rows = get_history(user_id, date_utc=date_str)
    if not rows:
        tg_send(chat_id, f"За {date_str} записей нет.")
    else:
        tg_send(chat_id, format_day_summary(date_str, rows))


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

    # Pending-состояния: rewrite и add_meal
    if not text.startswith("/"):
        # Rewrite имеет приоритет (явное действие пользователя)
        pending_date = get_pending_rewrite(user_id)
        if pending_date:
            clear_pending_rewrite(user_id)
            process_rewrite(chat_id, user_id, text, pending_date)
            return

        # Если был выбран приём пищи через /add
        pending_meal = get_pending_meal(user_id)
        if pending_meal:
            clear_pending_meal(user_id)
            process_food_message(chat_id, user_id, text, meal_type=pending_meal)
            return

    # Команды
    if text.startswith("/start"):
        handle_start(chat_id)
        return

    if text == "/cancel":
        clear_pending_rewrite(user_id)
        clear_pending_meal(user_id)
        tg_send(chat_id, "Отменено.")
        return

    if text.startswith("/add"):
        # Сбрасываем rewrite-ожидание если было, показываем меню приёмов пищи
        clear_pending_rewrite(user_id)
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
