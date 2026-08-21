"""Long polling для Telegram: режим запуска без домена и входящих портов."""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from index import TELEGRAM_API, process_polled_update

logger = logging.getLogger(__name__)
POLL_TIMEOUT = 50
POLL_RETRY_DELAY = 3

# Отдельная сессия именно для long-poll: соединение держится открытым до 50с,
# и мешать его в общий пул с исходящими sendMessage не стоит. Плюс keep-alive
# избавляет от DNS+TLS на каждой итерации цикла.
_poll_session = requests.Session()
# Обработка сообщения может ждать AI (до ~2×AI_TIMEOUT секунд на ретрай).
# Без пула это ждали бы ВСЕ остальные updates — включая команды без AI
# (кнопки, /today, /history) и сообщения других чатов — потому что
# следующий getUpdates() не вызывался бы, пока не обработан текущий.
# Пул небольшой: SQLite и бесплатный лимит LLM API не рассчитаны на десятки
# параллельных запросов личного бота.
WORKER_THREADS = 8

# У ThreadPoolExecutor внутренняя очередь заданий НЕ ограничена. getUpdates
# может вернуть до 100 апдейтов за раз; если их закидывать в пул без счёта,
# а обрабатывать они успевают медленнее, чем прилетают (всплеск активности,
# спам, просто медленный AI) — очередь и память растут без предела, и это
# именно то, что раньше сдерживалось последовательной обработкой. Семафор
# ограничивает число задач "в полёте": когда лимит достигнут, process_updates
# (единственный поток опроса) сам ждёт на acquire() перед тем как принять
# больше работы — то есть backpressure возвращается, но не ценой блокировки
# на КАЖДОМ отдельном update, как было в исходном баге.
_MAX_IN_FLIGHT = WORKER_THREADS * 4
_in_flight = threading.BoundedSemaphore(_MAX_IN_FLIGHT)

_executor = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="update")


def get_updates(offset: Optional[int]) -> list[dict]:
    payload = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    response = _poll_session.post(
        f"{TELEGRAM_API}/getUpdates",
        json=payload,
        timeout=POLL_TIMEOUT + 10,
    )
    # 409 = "terminated by other getUpdates request": тот же токен опрашивают
    # два процесса (например, не остановленный старый инстанс или вторая
    # копия сервиса). Они перехватывают updates друг у друга, из-за чего
    # сообщения приходят рывками и с задержкой. Это не сетевой сбой —
    # называем причину прямо, иначе её ищут часами.
    if response.status_code == 409:
        logger.error(
            "Telegram 409 Conflict: этот бот-токен опрашивает ещё один процесс. "
            "Оставь только один: sudo systemctl status calories-bot, "
            "и проверь, не запущена ли вторая копия бота или webhook."
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("Telegram getUpdates returned an invalid response")
    updates = payload.get("result")
    if not isinstance(updates, list) or not all(isinstance(update, dict) for update in updates):
        raise ValueError("Telegram getUpdates returned invalid updates")
    return updates


def _process_update_safe(update: dict) -> None:
    try:
        process_polled_update(update)
    except Exception:
        logger.exception("polled Telegram update processing failed")
    finally:
        _in_flight.release()


def process_updates(updates: list[dict], offset: Optional[int]) -> Optional[int]:
    """Ставит каждый update в пул воркеров и сразу возвращает offset для
    следующего getUpdates — не дожидаясь завершения обработки. Иначе один
    медленный AI-запрос (еда) блокировал бы доставку всех прочих updates,
    даже тех, которым AI вообще не требуется (кнопки, /today, /history).

    claim_update (SQLite) страхует от повторной обработки, поэтому продвигать
    offset до завершения обработки безопасно: подтверждение update Telegram'у
    и его идемпотентная обработка — разные, независимые гарантии.

    _in_flight.acquire() ограничивает число одновременно поставленных задач:
    если лимит достигнут, этот вызов блокируется до освобождения места — это
    и есть backpressure, не позволяющий очереди пула расти без ограничений
    под нагрузкой (см. комментарий у _MAX_IN_FLIGHT).
    """
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            logger.warning("ignored polled update without a valid update_id")
            continue
        _in_flight.acquire()
        _executor.submit(_process_update_safe, update)
        next_offset = max(next_offset or update_id + 1, update_id + 1)
    return next_offset


def run() -> None:
    """Бесконечно получает updates; дубли защищены SQLite claim_update."""
    # Стартовая строка с PID: если процесс падает (OOM-kill, краш) и systemd
    # поднимает его заново, в journalctl видно новый PID. Это отличает
    # "бот молчал" от "бот перезапустился": после рестарта offset снова None,
    # Telegram отдаёт весь накопленный хвост одной пачкой — и сообщения
    # выглядят так, будто бот "проснулся и выплюнул всё разом".
    logger.info("polling started (pid=%s, workers=%s)", os.getpid(), WORKER_THREADS)
    offset = None
    while True:
        try:
            offset = process_updates(get_updates(offset), offset)
        except Exception:
            logger.exception("Telegram polling failed; retrying in %s seconds", POLL_RETRY_DELAY)
            time.sleep(POLL_RETRY_DELAY)
