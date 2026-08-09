# sf

Нейтральный builder подписок для V2Ray/Xray-клиентов.

## Что делает

- читает `sources.json`;
- собирает `vless://`, `vmess://`, `trojan://`, `ss://` конфиги;
- удаляет дубликаты;
- определяет гео;
- проверяет доступность через TCP и, если доступен `xray`, через реальный Xray-core;
- генерирует:
  - `output/subscription.txt`
  - `output/manifest.json`
  - `output/singbox.json`
  - `output/xray.json`
- при наличии `TURSO_URL` и `TURSO_TOKEN` публикует результат в Turso.

## Секреты

В исходниках секретов нет. Для публикации в БД задай переменные окружения:

```bash
export TURSO_URL="libsql://..."
export TURSO_TOKEN="..."
```

Опционально для production:

```bash
export PROJECT_NAME="sf"
export PROJECT_USER_AGENT="sf-builder/1.0"
export REQUIRE_XRAY="1"
```

## Локальный запуск

```bash
python main.py
```

Если `TURSO_URL/TURSO_TOKEN` не заданы, builder только создаст локальные файлы в `output/`.

## Публичный GitHub Actions

Workflow запускает builder каждый час. Секреты должны храниться только в GitHub Actions Secrets:

- `TURSO_URL`
- `TURSO_TOKEN`

## Cloudflare Worker

`worker.js` отдаёт подписки из Turso:

- `/sub/:token` — автоформат по User-Agent
- `/sub/:token/plain`
- `/sub/:token/base64`
- `/sub/:token/xray`
- `/sub/:token/singbox`
- `/sub/:token/manifest`
- `/status`

Бот, оплаты и админка должны жить в отдельном приватном репозитории.
