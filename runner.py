"""Запускает webhook-сервер или Telegram long polling по конфигурации."""

import os


def main() -> None:
    mode = os.getenv("TELEGRAM_MODE", "webhook").lower()
    if mode == "polling":
        from polling import run

        run()
        return
    if mode != "webhook":
        raise SystemExit("TELEGRAM_MODE must be webhook or polling")

    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8080, workers=1)


if __name__ == "__main__":
    main()
