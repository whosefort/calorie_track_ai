"""Telegram Mini App API. Identity is verified on every request."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import index as bot

router = APIRouter()


def telegram_user(x_telegram_init_data: str = Header(default="")) -> int:
    try:
        pairs = parse_qsl(x_telegram_init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs) or not bot.TELEGRAM_TOKEN:
            raise ValueError()
        signature = values.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
        secret = hmac.new(b"WebAppData", bot.TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        age = time.time() - int(values["auth_date"])
        user_id = json.loads(values["user"])["id"]
        if not hmac.compare_digest(signature, expected) or not 0 <= age <= 86400 or type(user_id) is not int or user_id <= 0:
            raise ValueError()
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Откройте приложение из Telegram заново")
    if bot.ALLOWED_USERS_INVALID or (bot.ALLOWED_USERS and user_id not in bot.ALLOWED_USERS):
        raise HTTPException(403, "Нет доступа")
    return user_id


@router.get('/web')
def page():
    return FileResponse(Path(__file__).parent / 'web' / 'index.html', headers={'Cache-Control': 'no-store'})


@router.get('/web/api/history')
def history(user_id: int = Depends(telegram_user)):
    return bot.get_history(user_id, limit=100)


class Food(BaseModel):
    text: str = Field(min_length=1, max_length=bot.MAX_MESSAGE_LENGTH)
    meal_type: str = ''


@router.post('/web/api/food')
def food(body: Food, user_id: int = Depends(telegram_user)):
    text = body.text.strip()
    if not text or (body.meal_type and body.meal_type not in bot.MEAL_AI_PREFIX):
        raise HTTPException(400, 'Некорректное описание или приём пищи')
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    reservation = bot.reserve_food_request(user_id, day)
    if reservation is None:
        raise HTTPException(429, 'Дневной лимит запросов исчерпан')
    try:
        data = bot.call_ai(bot.MEAL_AI_PREFIX.get(body.meal_type, '') + text,
                           extra_system=bot._ai_extra_context(user_id))
        total = data['total']
        bot.save_record(user_id, text, json.dumps(data, ensure_ascii=False),
                        {'kcal': data['total_kcal'], 'protein_g': total['protein_g'],
                         'fat_g': total['fat_g'], 'carb_g': total['carb_g']},
                        date_utc=day, reservation_id=reservation)
        return data
    except Exception:
        bot.logger.exception('Mini App food request failed')
        raise HTTPException(502, 'Не удалось рассчитать или сохранить запись. Попробуйте ещё раз.')
    finally:
        bot.release_food_request(reservation)
