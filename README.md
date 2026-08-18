# Calories Bot на VPS

Telegram-бот считает калории и БЖУ, хранит историю в SQLite на VPS и вызывает модель через OpenAI-совместимый API. В проекте больше нет зависимости от Yandex Cloud Functions, API Gateway или YDB.

```
Telegram → Caddy (HTTPS) → FastAPI/Uvicorn → модель по API
                                      └────→ SQLite на VPS
```

## Быстрый запуск

Нужен VPS с Debian/Ubuntu и Python 3.9+, публичный IPv4 и домен. До запуска создай у домена A-запись на IP VPS и дождись её распространения. Если есть AAAA-запись, она тоже должна вести на этот VPS. Во внешнем firewall провайдера должны быть открыты TCP `80` и `443`; при активном UFW скрипт откроет их сам. Caddy сам выпустит TLS-сертификат.

```bash
git clone <URL_РЕПОЗИТОРИЯ> calories-bot
cd calories-bot
chmod +x deploy.sh
sudo ./deploy.sh setup
```

`setup` прямо в консоли спросит токен Telegram, домен, API endpoint, ключ и модель. Он установит зависимости, Caddy и systemd, сохранит конфиг в `/etc/calories-bot/calories-bot.env`, включит HTTPS и зарегистрирует webhook.

Поля вводятся как обычные значения: ключ вставляй **без** `Bearer`, `export` и кавычек. Например, в поле API key вставляется `sk-or-v1-...`, а не `Authorization: Bearer sk-or-v1-...`. `WEBHOOK_SECRET` можно оставить пустым: setup сам создаст криптографически случайный секрет.

После этого обновление одной командой:

```bash
git pull && sudo ./deploy.sh deploy
```

Скрипт копирует новый код в `/opt/calories-bot/app`, обновляет зависимости и перезапускает сервис. Перед каждым обновлением он создаёт согласованный backup SQLite. Затем проверяет настоящий HTTPS endpoint и только после этого регистрирует webhook. Конфиг и база находятся вне репозитория, поэтому `git pull` не стирает токены и историю.

## Выбор модели или внешнего «агента» по API

В коде нет привязки к поставщику. Нужен API, совместимый с OpenAI Chat Completions; это поддерживают OpenAI, OpenRouter, Ollama, vLLM и многие AI-шлюзы. При первоначальном setup укажи:

| Что спросит setup | Пример |
|---|---|
| URL API | `https://api.openai.com/v1`, `https://openrouter.ai/api/v1`, `http://127.0.0.1:11434/v1` |
| API key | ключ выбранного сервиса |
| ID модели | `gpt-4o-mini`, `google/gemini-2.5-flash`, имя локальной модели |

Чтобы сменить источник позже, отредактируй `/etc/calories-bot/calories-bot.env` и запусти `sudo ./deploy.sh deploy`. Файл `config.example.env` показывает все переменные, но не содержит секретов.

Формат реального файла — ровно `ИМЯ=значение`, по одной переменной на строку. Не добавляй `export` или `Bearer`; обычные ключи не требуют кавычек. После правки всегда запускай deploy, а не перезапускай Uvicorn вручную:

```bash
sudoedit /etc/calories-bot/calories-bot.env
sudo ./deploy.sh deploy
```

### Готовые конфиги провайдеров

**OpenRouter** — ключ начинается с `sk-or-`; endpoint и OpenAI SDK совместимы. [Документация OpenRouter](https://openrouter.ai/docs/quickstart)

```env
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-v1_ВСТАВЬ_СВОЙ_КЛЮЧ
LLM_MODEL=~openai/gpt-latest
LLM_STRUCTURED_OUTPUT=auto
LLM_TEMPERATURE=
```

**Gemini API напрямую** — ключ из Google AI Studio; это именно OpenAI-compatible endpoint, включая суффикс `/openai/`. [Документация Gemini](https://ai.google.dev/gemini-api/docs/openai)

```env
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=AIza_ВСТАВЬ_СВОЙ_КЛЮЧ
LLM_MODEL=gemini-3.6-flash
LLM_STRUCTURED_OUTPUT=auto
LLM_TEMPERATURE=
```

`auto` — рекомендуемый режим: бот сначала требует JSON Schema, но автоматически переходит на JSON-only, если конкретный провайдер или модель не поддерживает этот вариант Schema; серверная проверка остаётся строгой в обоих случаях.

`LLM_TEMPERATURE` по умолчанию пустой: так приложение совместимо с моделями, которые не принимают этот параметр. Для обычной генеративной модели, где параметр поддерживается, можно поставить `0` ради повторяемости расчётов.

Если у внешнего сервиса нет OpenAI-совместимого API, поставь перед ним маленький совместимый gateway. Так бот остаётся одинаковым, а меняется только `LLM_BASE_URL`.

## Защита от неверного формата модели

Бот сначала отправляет JSON Schema вместе с запросом (`LLM_STRUCTURED_OUTPUT=strict`). Это заставляет поддерживающие провайдеры вернуть ровно заданный объект. До сохранения ответ ещё раз строго проверяется сервером: лишние поля, строка вместо числа, `null`, `NaN`, неверный `meal_type` или неполная структура отклоняются. Затем бот делает один запрос на исправление формата; плохой результат никогда не попадает в SQLite.

Режим `auto` удобен для локальных и старых API: если они не знают JSON Schema, бот переходит на JSON-only запрос, но серверная строгая проверка всё равно остаётся. Для максимальной надёжности выбери `strict` и модель с поддержкой structured outputs.

Встроенный шаблон в `DEFAULT_SYSTEM_PROMPT` — единственный канонический prompt: он запрещает следовать инструкциям из сообщения пользователя, задаёт точный JSON-шаблон item и требует внутреннюю проверку сумм до ответа. Оставь `LLM_SYSTEM_PROMPT` пустым, если не нужна дополнительная бизнес-логика.

Сервер дополнительно сверяет итоги с позициями, округление, невозможный вес, БЖУ больше веса, нереалистичную калорийность, лишние поля, слишком длинный текст и слишком большой список позиций. Некорректный ответ повторно запрашивается и никогда не сохраняется в SQLite. Это ловит ошибки модели, но не заменяет живую базу пищевой ценности: для точного бренда или ресторанного блюда лучше указывать марку, вес и данные с упаковки.

## Данные, резервные копии и логи

- База: `/var/lib/calories-bot/calories.sqlite3`
- Конфиг с правами `600`: `/etc/calories-bot/calories-bot.env`
- Статус: `sudo ./deploy.sh status`
- Логи: `journalctl -u calories-bot -f`

Перед обновлением VPS можно сделать резервную копию без остановки бота:

```bash
sudo install -d -m 700 /root/calories-backups
sudo sqlite3 /var/lib/calories-bot/calories.sqlite3 ".backup '/root/calories-backups/calories-$(date +%F).sqlite3'"
```

## Команды бота

`/start`, `/add`, `/today`, `/history`, `/day ГГГГ-ММ-ДД`, `/rewrite`, `/cancel`.

## Важное при миграции

История из YDB автоматически не переносится: у неё другая схема доступа и новый бот использует SQLite на VPS. Старый Yandex webhook будет перезаписан на новый HTTPS-адрес во время `setup`. Не удаляй YDB и Cloud Function, пока не проверишь, что новый бот отвечает и история тебе больше не нужна.
