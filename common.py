import json
import os
import uuid
import logging
import requests
import openai
from datetime import datetime, timezone, timedelta

import ydb
import ydb.iam

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
WEBHOOK_SECRET       = os.getenv("WEBHOOK_SECRET", "")
AI_API_KEY           = os.getenv("YC_API_KEY")
AI_AGENT_ID          = os.getenv("AI_AGENT_ID")
AI_TIMEOUT           = 90
YDB_ENDPOINT         = os.getenv("YDB_ENDPOINT")
YDB_DATABASE         = os.getenv("YDB_DATABASE")
ALLOWED_USERS        = set(
    int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
)
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "20"))
MAX_MESSAGE_LENGTH   = 500
MAX_DAY_ENTRIES      = 20

ai_client = openai.OpenAI(
    api_key=AI_API_KEY,
    base_url="https://ai.api.cloud.yandex.net/v1",
)

# -- YDB singleton -----------------------------------------------------------

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
    now = datetime.now(timezone.utc)
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
    logger.info(f"Saved record {record_id} for user {user_id} date {date_utc}")


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
    logger.info(f"delete_day: deleted {len(record_ids)} records for user {user_id} date {date_utc}")
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
        session.transaction().execute(prepared, {"$user_id": user_id}, commit_tx=True)
    try:
        pool.retry_operation_sync(_delete)
    except Exception as e:
        logger.error(f"clear_pending_rewrite error: {e}")


# -- AI ----------------------------------------------------------------------

def validate_ai_response(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if "items" not in data or not isinstance(data["items"], list):
        return False
    if "total_kcal" not in data:
        return False
    if "total" not in data or not isinstance(data["total"], dict):
        return False
    required_fields = {"name", "weight_g", "kcal", "protein_g", "fat_g", "carb_g"}
    for item in data["items"]:
        if not isinstance(item, dict):
            return False
        if not required_fields.issubset(item.keys()):
            return False
        for field in ("weight_g", "kcal", "protein_g", "fat_g", "carb_g"):
            if not isinstance(item[field], (int, float)):
                return False
    return True


def call_ai(user_message: str) -> dict:
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

    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error(f"JSON parse failed. Content: {content[:500]}")
        raise

    if not validate_ai_response(data):
        logger.error(f"AI response failed validation: {str(data)[:300]}")
        raise ValueError("AI response has invalid structure")

    return data


# -- Telegram helpers --------------------------------------------------------

def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown"):
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
