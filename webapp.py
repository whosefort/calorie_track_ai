"""Telegram Mini App API. Identity is verified on every request."""
import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from pathlib import Path
from uuid import UUID

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


@router.delete('/web/api/food/{record_id}')
def delete_food(record_id: UUID, user_id: int = Depends(telegram_user)):
    with bot._db() as conn:
        deleted = conn.execute(
            'DELETE FROM calories_log WHERE user_id = ? AND record_id = ?',
            (user_id, str(record_id)),
        ).rowcount
    if not deleted:
        raise HTTPException(404, 'Запись уже удалена или недоступна')
    return {'ok': True}


class Food(BaseModel):
    text: str = Field(default="", max_length=bot.MAX_MESSAGE_LENGTH)
    photo: str = Field(default="", max_length=1_400_000)
    meal_type: str = ''


@router.post('/web/api/food')
def food(body: Food, user_id: int = Depends(telegram_user)):
    text = body.text.strip()
    if (not text and not body.photo) or (body.meal_type and body.meal_type not in bot.MEAL_AI_PREFIX):
        raise HTTPException(400, 'Некорректное описание или приём пищи')
    prompt = bot.MEAL_AI_PREFIX.get(body.meal_type, '') + text
    if body.photo:
        try:
            image = base64.b64decode(body.photo, validate=True)
            if not image.startswith(b"\xff\xd8\xff") or len(image) > 1_000_000:
                raise ValueError()
        except (ValueError, binascii.Error):
            raise HTTPException(400, 'Нужен снимок JPEG размером до 1 МБ')
        prompt = [
            {"type": "text", "text": prompt + "\nОцени видимые блюда и порции по фото. Укажи предположения о размере порции."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + body.photo}},
        ]
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    reservation = bot.reserve_food_request(user_id, day)
    if reservation is None:
        raise HTTPException(429, 'Дневной лимит запросов исчерпан')
    try:
        data = bot.call_ai(prompt, extra_system=bot._ai_extra_context(user_id))
        if not data['items']:
            raise HTTPException(422, 'Не удалось распознать продукты. Уточните описание или смените фото.')
        with bot._db() as conn:
            conn.execute(
                'DELETE FROM web_food_drafts WHERE request_id NOT IN '
                '(SELECT request_id FROM pending_food_requests)'
            )
            conn.execute(
                'INSERT INTO web_food_drafts (request_id, ai_json) VALUES (?, ?)',
                (reservation, json.dumps(data, ensure_ascii=False)),
            )
        return {'id': reservation, 'date_utc': day, 'items': data['items']}
    except HTTPException:
        bot.release_food_request(reservation)
        raise
    except Exception:
        bot.release_food_request(reservation)
        bot.logger.exception('Mini App food analysis failed')
        raise HTTPException(502, 'Не удалось рассчитать КБЖУ. Попробуйте ещё раз.')


class Portion(BaseModel):
    index: int = Field(ge=0, lt=bot.MAX_AI_ITEMS)
    grams: float = Field(ge=0.1, le=10000)


class FoodConfirmation(BaseModel):
    items: list[Portion] = Field(min_length=1, max_length=bot.MAX_AI_ITEMS)


def remove_draft(conn, user_id, draft_id):
    conn.execute(
        'DELETE FROM web_food_drafts WHERE request_id = ? AND EXISTS '
        '(SELECT 1 FROM pending_food_requests WHERE request_id = ? AND user_id = ?)',
        (draft_id, draft_id, user_id),
    )
    conn.execute(
        'DELETE FROM pending_food_requests WHERE request_id = ? AND user_id = ?',
        (draft_id, user_id),
    )


@router.delete('/web/api/food/draft/{draft_id}')
def cancel_food(draft_id: UUID, user_id: int = Depends(telegram_user)):
    with bot._db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        remove_draft(conn, user_id, str(draft_id))
    return {'ok': True}


@router.post('/web/api/food/draft/{draft_id}')
def confirm_food(draft_id: UUID, body: FoodConfirmation, user_id: int = Depends(telegram_user)):
    key = str(draft_id)
    macros = ('protein_g', 'fat_g', 'carb_g')
    with bot._db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        saved = conn.execute(
            'SELECT kcal FROM calories_log WHERE user_id = ? AND record_id = ?',
            (user_id, key),
        ).fetchone()
        if saved:
            return {'total_kcal': saved['kcal']}
        draft = conn.execute(
            'SELECT d.ai_json, p.date_utc FROM web_food_drafts d '
            'JOIN pending_food_requests p USING (request_id) '
            'WHERE request_id = ? AND user_id = ? AND created_at >= ?',
            (key, user_id, int(time.time()) - 600),
        ).fetchone()
        if not draft:
            raise HTTPException(410, 'Черновик истёк. Рассчитайте приём пищи заново.')
        original = json.loads(draft['ai_json'])['items']
        items = []
        seen = set()
        for portion in body.items:
            if portion.index in seen or portion.index >= len(original) or round(portion.grams, 1) != portion.grams:
                raise HTTPException(422, 'Проверьте вес продуктов (до одного знака после запятой).')
            seen.add(portion.index)
            item = original[portion.index].copy()
            ratio = portion.grams / item['weight_g']
            if portion.grams != item['weight_g']:
                item['portion_note'] = ''
            item['weight_g'] = portion.grams
            item['kcal'] = int(item['kcal'] * ratio + 0.5)
            for field in macros:
                item[field] = int(item[field] * ratio * 10 + 0.5) / 10
            items.append(item)
        kcal = sum(item['kcal'] for item in items)
        total = {field: round(sum(item[field] for item in items), 1) for field in macros}
        data = {'items': items, 'total_kcal': kcal, 'total': total, 'tip': '', 'note': ''}
        description = ', '.join(f"{item['name']} ({item['weight_g']:g} г)" for item in items)
        bot._insert_record(conn, user_id, description, json.dumps(data, ensure_ascii=False),
                           {'kcal': kcal, **total}, draft['date_utc'], record_id=key)
        remove_draft(conn, user_id, key)
    return {'total_kcal': kcal}
