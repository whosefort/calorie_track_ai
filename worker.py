import json
import logging

import common

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def process_food(chat_id: int, user_id: int, text: str, date_utc: str):
    try:
        data = common.call_ai(text)
    except json.JSONDecodeError:
        common.tg_send(chat_id, "Не смог распознать ответ от агента. Попробуй переформулировать.")
        return
    except ValueError:
        common.tg_send(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
        return
    except Exception as e:
        logger.error(f"AI error: {e}", exc_info=True)
        common.tg_send(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
        return

    common.tg_send(chat_id, common.format_ai_response(data))

    try:
        t = data.get("total", {})
        common.save_record(
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


def process_rewrite(chat_id: int, user_id: int, text: str, date_utc: str):
    try:
        data = common.call_ai(text)
    except json.JSONDecodeError:
        common.tg_send(chat_id, "Не смог распознать ответ от агента. Попробуй переформулировать.")
        return
    except ValueError:
        common.tg_send(chat_id, "Агент вернул неожиданный формат. Попробуй позже.")
        return
    except Exception as e:
        logger.error(f"AI error in rewrite: {e}", exc_info=True)
        common.tg_send(chat_id, "Ошибка при обращении к агенту. Попробуй позже.")
        return

    deleted = common.delete_day(user_id, date_utc)
    t = data.get("total", {})
    common.save_record(
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

    reply = common.format_ai_response(data)
    note  = f"\n\n✏️ Рацион за {date_utc} обновлён"
    if deleted > 0:
        note += f" (удалено старых записей: {deleted})"
    common.tg_send(chat_id, reply + note)


def handler(event, context):
    messages = event.get("messages", [])
    logger.info(f"=== WORKER START === messages: {len(messages)}")

    for msg_event in messages:
        try:
            body = msg_event["details"]["message"]["body"]
            task = json.loads(body)
            task_type = task.get("task_type")
            chat_id   = task["chat_id"]
            user_id   = task["user_id"]
            text      = task["text"]
            date_utc  = task["date_utc"]

            logger.info(f"Processing {task_type} for user {user_id}")

            if task_type == "food":
                process_food(chat_id, user_id, text, date_utc)
            elif task_type == "rewrite":
                process_rewrite(chat_id, user_id, text, date_utc)
            else:
                logger.warning(f"Unknown task type: {task_type}")

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            # Не перебрасываем исключение — возвращаем 200, чтобы YMQ не уходил в бесконечные ретраи

    logger.info("=== WORKER DONE ===")
    return {"statusCode": 200, "body": "ok"}
