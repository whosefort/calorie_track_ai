import json
import os
import logging
import boto3
from botocore.config import Config
from datetime import datetime, timezone, timedelta

import common

logger = logging.getLogger()
logger.setLevel(logging.INFO)

YMQ_QUEUE_URL  = os.getenv("YMQ_QUEUE_URL")
YMQ_KEY_ID     = os.getenv("YMQ_KEY_ID")
YMQ_SECRET_KEY = os.getenv("YMQ_SECRET_KEY")

_sqs = None


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client(
            service_name="sqs",
            endpoint_url="https://message-queue.api.cloud.yandex.net",
            region_name="ru-central1",
            aws_access_key_id=YMQ_KEY_ID,
            aws_secret_access_key=YMQ_SECRET_KEY,
            config=Config(connect_timeout=5, read_timeout=10),
        )
    return _sqs


def enqueue(task: dict):
    _get_sqs().send_message(
        QueueUrl=YMQ_QUEUE_URL,
        MessageBody=json.dumps(task, ensure_ascii=False),
    )
    logger.info(f"Enqueued {task.get('task_type')} for user {task.get('user_id')}")


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
    common.tg_send_keyboard(chat_id, "Выбери дату для перезаписи рациона:", buttons)


def handler(event, context):
    try:
        logger.info(f"=== WEBHOOK START === event keys: {list(event.keys())}")

        # Верификация webhook
        if common.WEBHOOK_SECRET:
            headers = event.get("headers") or {}
            incoming_secret = headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                headers.get("x-telegram-bot-api-secret-token", "")
            )
            if incoming_secret != common.WEBHOOK_SECRET:
                logger.warning("Invalid webhook secret")
                return {"statusCode": 403, "body": "forbidden"}

        body = event.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)

        logger.info(f"body: {str(body)[:200]}")

        # Обработка inline-кнопок
        callback_query = body.get("callback_query")
        if callback_query:
            cq_id   = callback_query["id"]
            user_id = callback_query["from"]["id"]
            chat_id = callback_query["message"]["chat"]["id"]
            data    = callback_query.get("data", "")

            if common.ALLOWED_USERS and user_id not in common.ALLOWED_USERS:
                common.tg_answer_callback(cq_id, "Нет доступа.")
                return {"statusCode": 200, "body": "ok"}

            if data.startswith("rewrite_date:"):
                date_str = data.split(":")[1]
                common.tg_answer_callback(cq_id)
                today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
                if date_str == today:
                    label = f"сегодня ({date_str})"
                elif date_str == yesterday:
                    label = f"вчера ({date_str})"
                else:
                    label = date_str
                common.tg_send(chat_id,
                    f"Выбрана дата: *{label}*\n\n"
                    f"Напиши что ел в этот день — я пересчитаю и заменю рацион.\n"
                    f"Или напиши /cancel чтобы отменить."
                )
                common.save_pending_rewrite(user_id, date_str)

            return {"statusCode": 200, "body": "ok"}

        # edited_message игнорируем — избегаем дублей
        message = body.get("message")
        if not message:
            return {"statusCode": 200, "body": "ok"}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text    = (message.get("text") or "").strip()

        logger.info(f"user={user_id} chat={chat_id} text={text[:80]}")

        if not text:
            return {"statusCode": 200, "body": "ok"}

        # Whitelist
        if common.ALLOWED_USERS and user_id not in common.ALLOWED_USERS:
            logger.info(f"Blocked user {user_id}")
            common.tg_send(chat_id, "Нет доступа.")
            return {"statusCode": 200, "body": "ok"}

        # Лимит длины сообщения
        if not text.startswith("/") and len(text) > common.MAX_MESSAGE_LENGTH:
            common.tg_send(chat_id,
                f"Сообщение слишком длинное. "
                f"Максимум {common.MAX_MESSAGE_LENGTH} символов, у тебя {len(text)}."
            )
            return {"statusCode": 200, "body": "ok"}

        # Проверка pending rewrite (до роутинга команд)
        if not text.startswith("/"):
            pending_date = common.get_pending_rewrite(user_id)
            if pending_date:
                common.clear_pending_rewrite(user_id)
                common.tg_send(chat_id, f"Считаю калории для {pending_date}...")
                enqueue({
                    "task_type": "rewrite",
                    "chat_id":   chat_id,
                    "user_id":   user_id,
                    "text":      text,
                    "date_utc":  pending_date,
                })
                logger.info("=== WEBHOOK DONE (pending rewrite queued) ===")
                return {"statusCode": 200, "body": "ok"}

        # Роутинг команд
        if text.startswith("/start"):
            common.tg_send(chat_id, (
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

        elif text == "/cancel":
            common.clear_pending_rewrite(user_id)
            common.tg_send(chat_id, "Отменено.")

        elif text.startswith("/today"):
            date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = common.get_history(user_id, date_utc=date_utc)
            if not rows:
                common.tg_send(chat_id, "За сегодня записей нет. Расскажи что ел.")
            else:
                common.tg_send(chat_id, common.format_day_summary(f"Сегодня, {date_utc}", rows))

        elif text.startswith("/history"):
            rows = common.get_history(user_id, limit=10)
            if not rows:
                common.tg_send(chat_id, "История пуста.")
            else:
                lines = ["*Последние записи:*\n"]
                for r in rows:
                    lines.append(common.format_log_entry(r))
                common.tg_send(chat_id, "\n".join(lines))

        elif text.startswith("/day"):
            parts = text.split()
            date_str = parts[1] if len(parts) > 1 else ""
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                common.tg_send(chat_id, "Формат даты: ГГГГ-ММ-ДД, например /day 2026-05-01")
                return {"statusCode": 200, "body": "ok"}
            rows = common.get_history(user_id, date_utc=date_str)
            if not rows:
                common.tg_send(chat_id, f"За {date_str} записей нет.")
            else:
                common.tg_send(chat_id, common.format_day_summary(date_str, rows))

        elif text.startswith("/rewrite"):
            parts = text.split(None, 2)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if len(parts) == 1:
                # /rewrite — показываем клавиатуру с датами
                show_rewrite_keyboard(chat_id)
            elif len(parts) == 2:
                # /rewrite <text> — перезаписать сегодня
                new_text = parts[1]
                if len(new_text) > common.MAX_MESSAGE_LENGTH:
                    common.tg_send(chat_id, f"Сообщение слишком длинное. Максимум {common.MAX_MESSAGE_LENGTH} символов.")
                    return {"statusCode": 200, "body": "ok"}
                common.tg_send(chat_id, f"Считаю калории для {today}...")
                enqueue({
                    "task_type": "rewrite",
                    "chat_id":   chat_id,
                    "user_id":   user_id,
                    "text":      new_text,
                    "date_utc":  today,
                })
            else:
                # /rewrite [DATE] <text>
                try:
                    datetime.strptime(parts[1], "%Y-%m-%d")
                    date_str = parts[1]
                    new_text = parts[2]
                except ValueError:
                    date_str = today
                    new_text = parts[1] + " " + parts[2]
                if len(new_text) > common.MAX_MESSAGE_LENGTH:
                    common.tg_send(chat_id, f"Сообщение слишком длинное. Максимум {common.MAX_MESSAGE_LENGTH} символов.")
                    return {"statusCode": 200, "body": "ok"}
                common.tg_send(chat_id, f"Считаю калории для {date_str}...")
                enqueue({
                    "task_type": "rewrite",
                    "chat_id":   chat_id,
                    "user_id":   user_id,
                    "text":      new_text,
                    "date_utc":  date_str,
                })

        else:
            # Сообщение с едой — проверяем лимит, ставим в очередь
            date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            count = common.count_today_requests(user_id, date_utc)
            if count >= common.MAX_REQUESTS_PER_DAY:
                common.tg_send(chat_id,
                    f"Лимит {common.MAX_REQUESTS_PER_DAY} запросов в день исчерпан.\n"
                    f"Используй /rewrite чтобы скорректировать уже добавленное."
                )
                return {"statusCode": 200, "body": "ok"}

            common.tg_send(chat_id, "Считаю калории...")
            enqueue({
                "task_type": "food",
                "chat_id":   chat_id,
                "user_id":   user_id,
                "text":      text,
                "date_utc":  date_utc,
            })

        logger.info("=== WEBHOOK DONE ===")
        return {"statusCode": 200, "body": "ok"}

    except Exception as e:
        logger.error(f"Webhook handler error: {e}", exc_info=True)
        return {"statusCode": 200, "body": "ok"}
