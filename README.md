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

## Cloudflare Worker (`worker.js`) — бот ViraVPN + подписки

Один воркер делает всё:

- Telegram-бот: меню, триал 7 дней, покупка подписки, админка, рассылки;
- оплаты: **Telegram Stars**, **CryptoBot** (Crypto Pay API), **DonationAlerts** (донат с кодом);
- отдача подписок из Turso:
  - `/sub/:token` — автоформат по User-Agent
  - `/sub/:token/plain` / `/base64` / `/xray` / `/singbox` / `/manifest`
  - `/status`

Токен подписки — персональный, выдаётся ботом; при истёкшей подписке отдаётся 403.

### Деплой через дашборд Cloudflare

1. **Workers & Pages → Create → Worker**, вставь содержимое `worker.js`, Deploy.
2. **Settings → Variables and Secrets** — добавь переменные:

   | Переменная | Тип | Что это |
   |---|---|---|
   | `BOT_TOKEN` | secret | токен бота от @BotFather |
   | `TG_WEBHOOK_SECRET` | secret | любая случайная строка |
   | `TURSO_URL` | secret | `libsql://…` (тот же, что у билдера) |
   | `TURSO_TOKEN` | secret | токен Turso |
   | `ADMIN_IDS` | text | твой Telegram id (через запятую, если несколько) |
   | `CRYPTOBOT_TOKEN` | secret | опционально: @CryptoBot → Crypto Pay → Create App |
   | `DA_CLIENT_ID` / `DA_CLIENT_SECRET` | text/secret | опционально: DonationAlerts OAuth-приложение |
   | `DA_USERNAME` | text | опционально: ник DA для ссылки на страницу доната |

3. Открой `https://<worker>/init?secret=<TG_WEBHOOK_SECRET>` — создаст таблицы и поставит webhook Telegram.
4. **Settings → Triggers → Cron Triggers** — добавь `* * * * *` (нужен для DonationAlerts, рассылок и напоминаний).
5. CryptoBot: в @CryptoBot → Crypto Pay → My Apps → Webhooks укажи `https://<worker>/cryptobot/webhook`.
6. DonationAlerts: создай приложение на `donationalerts.com/application/clients` с redirect URI `https://<worker>/da/callback`, затем один раз открой `https://<worker>/da/login?secret=<TG_WEBHOOK_SECRET>` и авторизуйся.

Тарифы и цены — константа `PLANS` в начале `worker.js` (₽ / ⭐ / USDT правятся в одном месте).
