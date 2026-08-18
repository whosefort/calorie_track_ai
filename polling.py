"""Long polling для Telegram: режим запуска без домена и входящих портов."""

import logging
import time
from typing import Optional

import requests

from index import TELEGRAM_API, process_polled_update

logger = logging.getLogger(__name__)
POLL_TIMEOUT = 50
POLL_RETRY_DELAY = 3


def get_updates(offset: Optional[int]) -> list[dict]:
    payload = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    response = requests.post(
        f"{TELEGRAM_API}/getUpdates",
        json=payload,
        timeout=POLL_TIMEOUT + 10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("Telegram getUpdates returned an invalid response")
    updates = payload.get("result")
    if not isinstance(updates, list) or not all(isinstance(update, dict) for update in updates):
        raise ValueError("Telegram getUpdates returned invalid updates")
    return updates


def process_updates(updates: list[dict], offset: Optional[int]) -> Optional[int]:
    """Возвращает offset, подтверждающий каждый полученный update."""
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            logger.warning("ignored polled update without a valid update_id")
            continue
        try:
            process_polled_update(update)
        except Exception:
            logger.exception("polled Telegram update processing failed")
        next_offset = max(next_offset or update_id + 1, update_id + 1)
    return next_offset


def run() -> None:
    """Бесконечно получает updates; дубли защищены SQLite claim_update."""
    offset = None
    while True:
        try:
            offset = process_updates(get_updates(offset), offset)
        except Exception:
            logger.exception("Telegram polling failed; retrying in %s seconds", POLL_RETRY_DELAY)
            time.sleep(POLL_RETRY_DELAY)
