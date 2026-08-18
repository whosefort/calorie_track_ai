"""ASGI-вход для VPS. Caddy проксирует сюда только Telegram webhook."""

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request

from index import WEBHOOK_BODY_LIMIT, process_telegram_update, verify_webhook_headers

app = FastAPI(docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        verify_webhook_headers(dict(request.headers))
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > WEBHOOK_BODY_LIMIT:
                raise HTTPException(status_code=413, detail="request too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content length")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > WEBHOOK_BODY_LIMIT:
            raise HTTPException(status_code=413, detail="request too large")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    try:
        # Бизнес-логика синхронная (SQLite и Telegram HTTP), поэтому не
        # блокируем event loop Uvicorn на время обработки запроса.
        await asyncio.to_thread(process_telegram_update, payload, dict(request.headers))
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except Exception:
        # Отдаём 200: Telegram не должен бесконечно ретраить update и создавать
        # дубли. Сама причина остаётся в journalctl.
        logger.exception("Telegram update processing failed")
    return {"ok": True}
