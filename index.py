import json
import os
import uuid
import logging
import requests
import openai
from datetime import datetime, timezone, timedelta

import ydb
import ydb.iam

# -- Логгер ------------------------------------------------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# -- Конфиг ------------------------------------------------------------------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Секрет для верификации webhook — одна строка, задаётся при регистрации webhook
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")

AI_API_KEY       = os.getenv("YC_API_KEY")
AI_AGENT_ID      = os.getenv("AI_AGENT_ID")
AI_TIMEOUT       = 25.0   # секунд — максимум ждём ответа от AI

ai_client = openai.OpenAI(
    api_key=AI_API_KEY,
    base_url="https://ai.api.cloud.yandex.net/v1",
)

YDB_ENDPOINT = os.getenv("YDB_ENDPOINT")
YDB_DATABASE = os.getenv("YDB_DATABASE")

ALLOWED_USERS = set(
    int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
)

MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "20"))
MAX_MESSAGE_LENGTH   = 500   # символов — защита от длинных сообщений
MAX_DAY_ENTRIES      = 20    # максимум записей в одном дне для /today и /day

# -- YDB: singleton ----------------------------------------------------------
_ydb_driver = None
_ydb_pool   = None

def _get_pool():
    global _ydb_driver, _ydb_pool
    if _ydb_pool is not None:
        return _ydb_pool
    credentials = ydb.iam.MetadataUrlCredentials()
    config = ydb.DriverConfig(
        endpoint=YDB_ENDPOINT,
        database=YDB_DATABASE,
        credentials=credentials,
    )
    _ydb_driver = ydb.Driver(config)
    _ydb_driver.wait(fail_fast=True, timeout=5)
    _ydb_pool = ydb.SessionPool(_ydb_driver)
    return _ydb_pool


# -- YDB: запись и чтение ----------------------------------------------------

def save_record(user_id: int, user_text: str, ai_json: str, totals: dict,
                date_utc: str = None):
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
            "$user_text": user_text[:500],   # обрезаем на всякий случай
            "$ai_json":   ai_json,
            "$kcal":      float(totals.get("kcal", 0)),
            "$protein_g": float(totals.get("protein_g", 0)),
            "$fat_g":     float(totals.get("fat_g", 0)),
            "$carb_g":    float(totals.get("carb_g", 0)),
        }, commit_tx=True)

    pool.retry_operation_sync(_upsert)
    logger.info(f"Saved record {record_id} for user {user_id} date {date_utc}")


def delete_day(user_id: int, date_utc: str) -> int:
    """Удаляет все записи пользователя за указанную дату одной транзакцией."""
    pool = _get_pool()

    rows = get_history(user_id, date_utc=date_utc)
    if not rows:
        logger.info(f"delete_day: no records for user {user_id} date {date_utc}")
        return 0

    record_ids = [r["record_id"] for r in rows]
    logger.info(f"delete_day: deleting {len(record_ids)} records for user {user_id} date {date_utc}")

    def _delete(session):
        tx = session.transaction()
        prepared = session.prepare("""
            DECLARE $user_id   AS Int64;
            DECLARE $record_id AS Utf8;
            DELETE FROM calories_log
            WHERE user_id = $user_id AND record_id = $record_id;
        """)
        for rid in record_ids:
            tx.execute(prepared, {
                "$user_id":   user_id,
                "$record_id": rid,
            })
        tx.commit()

    pool.retry_operation_sync(_delete)
    logger.info(f"delete_day: done, deleted {len(record_ids)} records")
    return len(record_ids)


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
    results = []
    for row in rs[0].rows:
        results.append({
            "record_id": row.record_id,
            "ts":        row.ts,
            "date_utc":  row.date_utc,
            "user_text": row.user_text,
            "kcal":      row.kcal,
            "protein_g": row.protein_g,
            "fat_g":     row.fat_g,
            "carb_g":    row.carb_g,
        })
    return results


def count_today_requests(user_id: int, date_utc: str) -> int:
    """Считает количество записей пользователя за сегодня."""
    rows = get_history(user_id, date_utc=date_utc, limit=MAX_REQUESTS_PER_DAY + 1)
    return len(rows)


# -- Pending rewrite ---------------------------------------------------------

def save_pending_rewrite(user_id: int, date_str: str):
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
        logger.error(f"save_pending_rewrite error: {e}")


def get_pending_rewrite(user_id: int) -> str | None:
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
        logger.error(f"get_pending_rewrite error: {e}")
        return None


def clear_pending_rewrite(user_id: int):
    pool = _get_pool()
    def _delete(session):
        prepared = session.prepare("""
            DECLARE $user_id AS Int64;
            DELETE FROM pending_rewrite WHERE user_id = $user_id;
        """)
        session.transaction().execute(prepared, {
            "$user_id": user_id,
        }, commit_tx=True)
    try:
        pool.retry_operation_sync(_delete)
    except Exception as e:
        logger.error(f"clear_pending_rewrite error: {e}")


# -- AI ----------------------------------------------------------------------

def validate_ai_response(data: dict) -> bool:
    """Проверяет что ответ AI имеет ожидаемую структуру."""
    if not isinstance(data, dict):
        return False
    if "items" not in data or not isinstance(data["items"], list):
        return False
    if "total_kcal" not in data:
        return False
    if "total" not in data or not isinstance(data["total"], dict):
        return False
    # Проверяем каждый item
    required_fields = {"name", "weight_g", "kcal", "protein_g", "fat_g", "carb_g"}
    for item in data["items"]:
        if not isinstance(item, dict):
            return False
        if not required_fields.issubset(item.keys()):
            return False
        # Проверяем числовые поля
        for field in ("weight_g", "kcal", "protein_g", "fat_g", "carb_g"):
            if not isinstance(item[field], (int, float)):
                return False
    return True


def call_ai(user_message: str) -> dict:
    """Вызывает AI агента с таймаутом и валидацией ответа."""
    try:
        response = ai_client.responses.create(
            prompt={"id": AI_AGENT_ID},
            input=user_message,
            timeout=AI_TIMEOUT,
        )
    except Exception as e:
        logger.error(f"AI API call failed: {type(e).__name__}: {e}")
        raise

    content = response.output_text.strip()
    logger.info(f"AI raw response: {content[:300]}")

    # Убираем markdown-блоки если агент их добавил
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed. Content: {content[:500]}")
        raise

    if not validate_ai_response(data):
        logger.error(f"AI response failed validation: {str(data)[:300]}")
        raise ValueError("AI response has invalid structure")

    return data


# -- Telegram helpers --------------------------------------------------------

def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown"):
    # Telegram ограничивает сообщения 4096 символами
    if len(text) > 4000:
        text = text[:3990] + "\n...(обрезано)"
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
    }, timeout=10)


def tg_send_keyboard(chat_id: int, text: str, buttons: list):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   "Markdown",
        "reply_markup": {"inline_keyboard": buttons},
    }, timeout=10)


def tg_answer_callback(callback_query_id: str, text: str = ""):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={
        "callback_query_id": callback_query_id,
        "text": text,
    }, timeout=5)


def show_rewrite_keyboard(chat_id: int):
    now = datetime.now(timezone.utc)
    buttons = []
    row = []
    for i in range(7):
        day = now - timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        if i == 0:
            label = f"Сегодня {day.strftime('%d.%m')}"
        elif i == 1:
            label = f"Вчера {day.strftime('%d.%m')}"
        else:
            label = day.strftime("%d.%m (%a)").replace(
                "Mon", "пн").replace("Tue", "вт").replace("Wed", "ср").replace(
                "Thu", "чт").replace("Fri", "пт").replace("Sat", "сб").replace("Sun", "вс")
        row.append({"text": label, "callback_data": f"rewrite_date:{date_str}"})
        if len(row) == 2 or i == 6:
            buttons.append(row)
            row = []
    tg_send_keyboard(chat_id, "Выбери дату для перезаписи рациона:", buttons)


def handle_rewrite_callback(chat_id: int, user_id: int,
                             callback_query_id: str, date_str: str):
    tg_answer_callback(callback_query_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == today:
        label = f"сегодня ({date_str})"
    elif date_str == yesterday:
        label = f"вчера ({date_str})"
    else:
        label = date_str
    tg_send(chat_id,
        f"Выбрана дата: *{label}*\n\n"
        f"Напиши что ел в этот день — я пересчитаю и заменю рацион.\n"
        f"Или напиши /cancel чтобы отменить."
    )
    save_pending_rewrite(user_id, date_str)


# -- Форматирование ----------------------------------------------------------

MEAL_LABELS = {
    "breakfast": "🌅 Завтрак",
    "lunch":     "☀️ Обед",
    "dinner":    "🌙 Ужин",
    "snack":     "🍎 Перекус",
    None:        "🍽 Прочее",
}
MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack", None]


def format_log_entry(entry: dict) -> str:
    return (
        f"*{entry['date_utc']}*  •  {entry['kcal']:.0f} ккал\n"
        f"  Б {entry['protein_g']:.1f}г  "
        f"Ж {entry['fat_g']:.1f}г  "
        f"У {entry['carb_g']:.1f}г\n"
        f"  _{entry['user_text'][:60]}_"
    )


def format_ai_response(data: dict) -> str:
    groups = {}
    for item in data.get("items", []):
        meal = item.get("meal_type")
        groups.setdefault(meal, []).append(item)

    lines = ["*Подсчитано:*\n"]
    for meal in MEAL_ORDER:
        if meal not in groups:
            continue
        lines.append(f"\n{MEAL_LABELS[meal]}")
        for item in groups[meal]:
            portion = f"{item['weight_g']}г"
            if item.get("portion_note"):
                portion += f" ({item['portion_note']})"
            lines.append(
                f"• *{item['name']}* {portion}\n"
                f"  {item['kcal']} ккал  |  "
                f"Б {item['protein_g']}г  Ж {item['fat_g']}г  У {item['carb_g']}г"
            )

    t = data.get("total", {})
    lines.append(
        f"\n*Итого: {data.get('total_kcal', 0)} ккал*\n"
        f"Б {t.get('protein_g', 0)}г  |  "
        f"Ж {t.get('fat_g', 0)}г  |  "
        f"У {t.get('carb_g', 0)}г"
    )
    if data.get("tip"):
        lines.append(f"\n💡 {data['tip']}")
    return "\n".join(lines)


def format_day_summary(title: str, rows: list) -> str:
    total_kcal = sum(r["kcal"] for r in rows)
    total_p    = sum(r["protein_g"] for r in rows)
    total_f    = sum(r["fat_g"] for r in rows)
    total_c    = sum(r["carb_g"] for r in rows)
    lines = [f"*{title}*\n"]
    for r in rows:
        lines.append(format_log_entry(r))
    if len(rows) >= MAX_DAY_ENTRIES:
        lines.append(f"\n_Показаны первые {MAX_DAY_ENTRIES} записей_")
    lines.append(
        f"\n*Итого:* {total_kcal:.0f} ккал | "
        f"Б {total_p:.1f}г | Ж {total_f:.1f}г | У {total_c:.1f}г"
    )
    return "\n".join(lines)


# -- Команды -----------------------------------------------------------------

def handle_start(chat_id: int):
    tg_send(chat_id, (
        "Привет! Я помогу отслеживать калории.\n\n"
        "Просто напиши что ел, например:\n"
        "_«съел гречку 200г и куриную грудку 150г»_\n\n"
        "*Команды:*\n"
        "/today — сводка за сегодня\n"
        "/history — последние 10 записей\n"
        "/day 2026-05-01 — записи за конкретный день\n"
        "/rewrite — переписать рацион (выбор даты кнопками)\n"
        "/rewrite 2026-05-01 гречка 200г — переписать конкретный день"
    ))


def handle_today(chat_id: int, user_id: int):
    date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = get_history(user_id, date_utc=date_utc)
    if not rows:
        tg_send(chat_id, "За сегодня записей нет. Расскажи что ел.")
        return
    tg_send(chat_id, format_day_summary(f"Сегодня, {date_utc}", rows))


def handle_history(chat_id: int, user_id: int):
    rows = get_history(user_id, limit=10)
    if not rows:
        tg_send(chat_id, "История пуста.")
        return
    lines = ["*Последние записи:*\n"]
    for r in rows:
        lines.append(format_log_entry(r))
    tg_send(chat_id, "\n".join(lines))


def handle_day(chat_id: int, user_id: int, date_str: str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        tg_send(chat_id, "Формат даты: ГГГГ-ММ-ДД, например /day 2026-05-01")
        return
    rows = get_history(user_id, date_utc=date_str)
    if not rows:
        tg_send(chat_id, f"За {date_str} записей нет.")
        return
    tg_send(chat_id, format_day_summary(date_str, rows))


def handle_rewrite(chat_id: int, user_id: int, date_str: str, new_text: str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        tg_send(chat_id, "Формат: /rewrite ГГГГ-ММ-ДД что ел\nИли /rewrite что ел — за сегодня")
        return

    if not new_text.strip():
        tg_send(chat_id, "Укажи что ел после даты.")
        return

    # Лимит длины и для rewrite тоже
    if len(new_text) > MAX_MESSAGE_LENGTH:
        tg_send(chat_id, f"Сообщение слишком длинное. Максимум {MAX_MESSAGE_LENGTH} символов.")
        return

    tg_send(chat_id, f"Считаю калории для {date_str}...")

    try:
        data = call_ai(new_text)
    except json.JSONDecodeError:
        tg_send(chat_id, "Не смог распознать ответ от агента. Попробуй переформулировать.")
        return
    except ValueError as e:
        logger.error(f"AI validation error in rewrite: {e}")
        tg_send(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
        return
    except Exception as e:
        logger.error(f"AI error in rewrite: {e}", exc_info=True)
        tg_send(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
        return

    deleted = delete_day(user_id, date_str)
    t = data.get("total", {})
    save_record(
        user_id=user_id,
        user_text=new_text,
        ai_json=json.dumps(data, ensure_ascii=False),
        totals={
            "kcal":      data.get("total_kcal", 0),
            "protein_g": t.get("protein_g", 0),
            "fat_g":     t.get("fat_g", 0),
            "carb_g":    t.get("carb_g", 0),
        },
        date_utc=date_str,
    )
    reply = format_ai_response(data)
    note = f"\n\n✏️ Рацион за {date_str} обновлён"
    if deleted > 0:
        note += f" (удалено старых записей: {deleted})"
    tg_send(chat_id, reply + note)


# -- Основной handler --------------------------------------------------------

def handler(event, context):
    try:
        logger.info(f"=== START === event keys: {list(event.keys())}")

        # -- Верификация webhook ---------------------------------------------
        # Проверяем секретный токен который Telegram присылает в заголовке.
        # Если WEBHOOK_SECRET задан — запросы без правильного токена отклоняем.
        if WEBHOOK_SECRET:
            headers = event.get("headers") or {}
            incoming_secret = headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                headers.get("x-telegram-bot-api-secret-token", "")
            )
            if incoming_secret != WEBHOOK_SECRET:
                logger.warning(f"Invalid webhook secret: '{incoming_secret[:20]}'")
                return {"statusCode": 403, "body": "forbidden"}

        body = event.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)

        logger.info(f"body parsed: {str(body)[:200]}")

        # -- Обработка inline-кнопок -----------------------------------------
        callback_query = body.get("callback_query")
        if callback_query:
            cq_id   = callback_query["id"]
            user_id = callback_query["from"]["id"]
            chat_id = callback_query["message"]["chat"]["id"]
            data    = callback_query.get("data", "")

            if ALLOWED_USERS and user_id not in ALLOWED_USERS:
                tg_answer_callback(cq_id, "Нет доступа.")
                return {"statusCode": 200, "body": "ok"}

            if data.startswith("rewrite_date:"):
                date_str = data.split(":")[1]
                handle_rewrite_callback(chat_id, user_id, cq_id, date_str)

            return {"statusCode": 200, "body": "ok"}

        # -- Обычное сообщение -----------------------------------------------
        # edited_message игнорируем — не хотим дублировать записи
        message = body.get("message")
        if not message:
            return {"statusCode": 200, "body": "ok"}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text    = (message.get("text") or "").strip()

        logger.info(f"user={user_id} chat={chat_id} text={text[:80]}")

        if not text:
            return {"statusCode": 200, "body": "ok"}

        # -- Проверка whitelist ----------------------------------------------
        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            logger.info(f"Blocked user {user_id}")
            tg_send(chat_id, "Нет доступа.")
            return {"statusCode": 200, "body": "ok"}

        # -- Лимит длины сообщения -------------------------------------------
        if not text.startswith("/") and len(text) > MAX_MESSAGE_LENGTH:
            tg_send(chat_id,
                f"Сообщение слишком длинное. "
                f"Максимум {MAX_MESSAGE_LENGTH} символов, у тебя {len(text)}."
            )
            return {"statusCode": 200, "body": "ok"}

        # -- Проверка pending rewrite ----------------------------------------
        if not text.startswith("/"):
            pending_date = get_pending_rewrite(user_id)
            if pending_date:
                clear_pending_rewrite(user_id)
                handle_rewrite(chat_id, user_id, pending_date, text)
                logger.info("=== DONE (pending rewrite) ===")
                return {"statusCode": 200, "body": "ok"}

        # -- Роутинг команд --------------------------------------------------
        if text.startswith("/start"):
            handle_start(chat_id)

        elif text == "/cancel":
            clear_pending_rewrite(user_id)
            tg_send(chat_id, "Отменено.")

        elif text.startswith("/today"):
            handle_today(chat_id, user_id)

        elif text.startswith("/history"):
            handle_history(chat_id, user_id)

        elif text.startswith("/day"):
            parts = text.split()
            date_str = parts[1] if len(parts) > 1 else ""
            handle_day(chat_id, user_id, date_str)

        elif text.startswith("/rewrite"):
            parts = text.split(None, 2)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if len(parts) == 1:
                show_rewrite_keyboard(chat_id)
            elif len(parts) == 2:
                handle_rewrite(chat_id, user_id, today, parts[1])
            else:
                try:
                    datetime.strptime(parts[1], "%Y-%m-%d")
                    handle_rewrite(chat_id, user_id, parts[1], parts[2])
                except ValueError:
                    handle_rewrite(chat_id, user_id, today, parts[1] + " " + parts[2])

        else:
            # -- Сообщение с едой --------------------------------------------
            date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            count = count_today_requests(user_id, date_utc)
            if count >= MAX_REQUESTS_PER_DAY:
                tg_send(chat_id,
                    f"Лимит {MAX_REQUESTS_PER_DAY} запросов в день исчерпан.\n"
                    f"Используй /rewrite чтобы скорректировать уже добавленное."
                )
                return {"statusCode": 200, "body": "ok"}

            tg_send(chat_id, "Считаю калории...")
            try:
                data = call_ai(text)
                logger.info(f"AI returned: {str(data)[:200]}")
            except json.JSONDecodeError:
                tg_send(chat_id, "Не смог распознать ответ от агента. Попробуй переформулировать.")
                return {"statusCode": 200, "body": "ok"}
            except ValueError:
                tg_send(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
                return {"statusCode": 200, "body": "ok"}
            except Exception as e:
                logger.error(f"AI error: {e}", exc_info=True)
                tg_send(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
                return {"statusCode": 200, "body": "ok"}

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
                )
            except Exception as e:
                logger.error(f"YDB save error: {e}", exc_info=True)

        logger.info("=== DONE ===")
        return {"statusCode": 200, "body": "ok"}

    except Exception as e:
        logger.error(f"Handler error: {e}", exc_info=True)
        return {"statusCode": 200, "body": "ok"}
