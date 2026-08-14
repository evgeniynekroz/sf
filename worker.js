/**
 * ViraVPN — Cloudflare Worker: Telegram-бот + отдача подписок + оплаты.
 *
 * Один воркер делает всё:
 *   - Telegram-бот (webhook): меню, покупка, триал, админка, рассылка;
 *   - оплаты: Telegram Stars, CryptoBot (Crypto Pay API), DonationAlerts (код в донате);
 *   - отдача подписок: /sub/:token (plain | base64 | xray | singbox | manifest);
 *   - cron: сверка донатов DA, очередь рассылки, напоминания об окончании подписки.
 *
 * ── Переменные окружения (Settings → Variables and Secrets в дашборде CF) ──
 *   BOT_TOKEN          — токен бота от @BotFather                       (secret, обязателен)
 *   TG_WEBHOOK_SECRET  — случайная строка, защита вебхука и /init       (secret, обязателен)
 *   TURSO_URL          — libsql://... (тот же, что у билдера)           (secret, обязателен)
 *   TURSO_TOKEN        — токен Turso                                    (secret, обязателен)
 *   ADMIN_IDS          — telegram id админов через запятую, напр. "123456789"
 *   CRYPTOBOT_TOKEN    — токен приложения из @CryptoBot → Crypto Pay    (secret, опционален)
 *   DA_CLIENT_ID       — DonationAlerts OAuth app id                    (опционален)
 *   DA_CLIENT_SECRET   — DonationAlerts OAuth app secret                (secret, опционален)
 *   PUBLIC_BASE_URL    — переопределить базовый URL подписок (по умолчанию — origin воркера)
 *
 * ── Первичная настройка ──
 *   1. Задеплой воркер, пропиши переменные.
 *   2. Открой  https://<worker>/init?secret=<TG_WEBHOOK_SECRET>
 *      — создаст таблицы и поставит Telegram webhook.
 *   3. Cron Trigger в дашборде: "* * * * *" (каждую минуту) — нужен для DA и рассылок.
 *   4. CryptoBot: в @CryptoBot → Crypto Pay → My Apps → Webhooks укажи
 *      https://<worker>/cryptobot/webhook
 *   5. DonationAlerts: создай приложение на www.donationalerts.com/application/clients,
 *      redirect URI: https://<worker>/da/callback, затем один раз открой
 *      https://<worker>/da/login?secret=<TG_WEBHOOK_SECRET> и авторизуйся.
 */

// ── Конфиг тарифов ────────────────────────────────────────────────────────────

const BRAND = "ViraVPN";

const PLANS = [
  { id: "m1",  title: "1 месяц",    days: 30,  rub: 100, stars: 100, usdt: "1.3"  },
  { id: "m3",  title: "3 месяца",   days: 90,  rub: 270, stars: 270, usdt: "3.5"  },
  { id: "m12", title: "12 месяцев", days: 365, rub: 900, stars: 900, usdt: "11.5" },
];

const TRIAL_DAYS = 7;

const BROADCAST_BATCH = 30;   // сообщений за один запуск cron (лимит subrequests на free-плане)
const REMIND_HOURS    = 24;   // за сколько часов напоминать об окончании подписки

const planById = (id) => PLANS.find((p) => p.id === id) || null;

// ── Утилиты ───────────────────────────────────────────────────────────────────

const esc = (s) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function sqliteNow(offsetSec = 0) {
  return new Date(Date.now() + offsetSec * 1000).toISOString().slice(0, 19).replace("T", " ");
}

function parseSqliteDate(s) {
  if (!s) return null;
  const d = new Date(String(s).replace(" ", "T") + "Z");
  return isNaN(d.getTime()) ? null : d;
}

function fmtDateHuman(s) {
  const d = parseSqliteDate(s);
  if (!d) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)}.${d.getUTCFullYear()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

function randomToken(bytes = 16) {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomCode() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // без похожих символов
  const a = new Uint8Array(5);
  crypto.getRandomValues(a);
  return "VIRA-" + [...a].map((b) => alphabet[b % alphabet.length]).join("");
}

const jsonResp = (obj, status = 200) =>
  new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

const textResp = (s, status = 200, type = "text/plain; charset=utf-8") =>
  new Response(s, { status, headers: { "content-type": type } });

function adminIds(env) {
  return String(env.ADMIN_IDS || "")
    .split(",")
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => Number.isFinite(n));
}

const isAdmin = (env, tgId) => adminIds(env).includes(Number(tgId));

function baseUrl(env, req) {
  if (env.PUBLIC_BASE_URL) return String(env.PUBLIC_BASE_URL).replace(/\/+$/, "");
  const u = new URL(req.url);
  return `${u.protocol}//${u.host}`;
}

// ── Turso (libSQL HTTP API) ───────────────────────────────────────────────────

function tursoArg(v) {
  if (v === null || v === undefined) return { type: "null" };
  return { type: "text", value: String(v) };
}

async function tursoPipeline(env, statements) {
  const url = String(env.TURSO_URL || "").replace("libsql://", "https://");
  if (!url || !env.TURSO_TOKEN) throw new Error("TURSO_URL/TURSO_TOKEN не заданы");
  const resp = await fetch(`${url}/v2/pipeline`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.TURSO_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ requests: [...statements, { type: "close" }] }),
  });
  if (!resp.ok) throw new Error(`Turso HTTP ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  for (const r of data.results || []) {
    if (r.type === "error") throw new Error(`Turso: ${r.error?.message || "unknown error"}`);
  }
  return data.results || [];
}

async function ex(env, sql, args = []) {
  await tursoPipeline(env, [{ type: "execute", stmt: { sql, args: args.map(tursoArg) } }]);
}

async function q(env, sql, args = []) {
  const results = await tursoPipeline(env, [
    { type: "execute", stmt: { sql, args: args.map(tursoArg) } },
  ]);
  const result = results[0]?.response?.result || {};
  const cols = (result.cols || []).map((c) => c.name);
  return (result.rows || []).map((row) =>
    Object.fromEntries(cols.map((c, i) => [c, row[i]?.value ?? null]))
  );
}

const one = async (env, sql, args = []) => (await q(env, sql, args))[0] || null;

async function getSetting(env, key) {
  const row = await one(env, "SELECT value FROM settings WHERE key = ?", [key]);
  return row ? row.value : null;
}

async function setSetting(env, key, value) {
  await ex(env, "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [key, value]);
}

async function initDb(env) {
  await tursoPipeline(env, [
    { type: "execute", stmt: { sql: `
      CREATE TABLE IF NOT EXISTS users (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id          INTEGER UNIQUE NOT NULL,
        username             TEXT,
        full_name            TEXT,
        trial_used           INTEGER NOT NULL DEFAULT 0,
        subscription_token   TEXT UNIQUE,
        subscription_expires TEXT,
        created_at           TEXT NOT NULL DEFAULT (datetime('now'))
      )`, args: [] } },
    { type: "execute", stmt: { sql: `
      CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
      )`, args: [] } },
    { type: "execute", stmt: { sql: `
      CREATE TABLE IF NOT EXISTS payments (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER,
        provider            TEXT NOT NULL,
        provider_payment_id TEXT UNIQUE,
        amount              TEXT,
        currency            TEXT,
        status              TEXT NOT NULL,
        period_days         INTEGER,
        raw_payload         TEXT,
        created_at          TEXT NOT NULL DEFAULT (datetime('now')),
        paid_at             TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
      )`, args: [] } },
    { type: "execute", stmt: { sql: `
      CREATE TABLE IF NOT EXISTS broadcasts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        text       TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'draft',
        cursor     INTEGER NOT NULL DEFAULT 0,
        sent       INTEGER NOT NULL DEFAULT 0,
        failed     INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      )`, args: [] } },
    { type: "execute", stmt: { sql:
      "CREATE INDEX IF NOT EXISTS idx_users_subscription_token ON users(subscription_token)", args: [] } },
    { type: "execute", stmt: { sql:
      "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)", args: [] } },
  ]);
}

// ── Пользователи и подписка ───────────────────────────────────────────────────

async function ensureUser(env, from) {
  const fullName = [from.first_name, from.last_name].filter(Boolean).join(" ");
  let user = await one(env, "SELECT * FROM users WHERE telegram_id = ?", [from.id]);
  if (!user) {
    await ex(
      env,
      "INSERT OR IGNORE INTO users (telegram_id, username, full_name) VALUES (?, ?, ?)",
      [from.id, from.username || null, fullName || null]
    );
    user = await one(env, "SELECT * FROM users WHERE telegram_id = ?", [from.id]);
  } else if (user.username !== (from.username || null) || user.full_name !== (fullName || null)) {
    await ex(env, "UPDATE users SET username = ?, full_name = ? WHERE telegram_id = ?", [
      from.username || null, fullName || null, from.id,
    ]);
  }
  return user;
}

function subIsActive(user) {
  const exp = parseSqliteDate(user?.subscription_expires);
  return !!exp && exp.getTime() > Date.now();
}

/** Продлевает подписку на days от max(now, текущий срок). Возвращает свежую строку user. */
async function extendSubscription(env, user, days) {
  let token = user.subscription_token;
  if (!token) token = randomToken();
  const current = parseSqliteDate(user.subscription_expires);
  const from = current && current.getTime() > Date.now() ? current : new Date();
  const newExp = new Date(from.getTime() + days * 86400 * 1000)
    .toISOString().slice(0, 19).replace("T", " ");
  await ex(
    env,
    "UPDATE users SET subscription_token = ?, subscription_expires = ? WHERE id = ?",
    [token, newExp, user.id]
  );
  return await one(env, "SELECT * FROM users WHERE id = ?", [user.id]);
}

// ── Telegram API ──────────────────────────────────────────────────────────────

async function tg(env, method, payload) {
  const resp = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!data.ok) console.log(`tg ${method} error: ${JSON.stringify(data)}`);
  return data;
}

const send = (env, chatId, text, extra = {}) =>
  tg(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    ...extra,
  });

/** Ответ на callback: правим сообщение, при неудаче шлём новое. */
async function editOrSend(env, cq, text, extra = {}) {
  const res = await tg(env, "editMessageText", {
    chat_id: cq.message.chat.id,
    message_id: cq.message.message_id,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    ...extra,
  });
  if (!res.ok) await send(env, cq.message.chat.id, text, extra);
}

// ── Клавиатуры и тексты ───────────────────────────────────────────────────────

const kbMain = (admin) => ({
  inline_keyboard: [
    [{ text: "📊 Моя подписка", callback_data: "sub" }],
    [{ text: "💳 Купить подписку", callback_data: "buy" }],
    [{ text: `🎁 Пробный период (${TRIAL_DAYS} дн.)`, callback_data: "trial" }],
    [{ text: "📱 Как подключиться", callback_data: "howto" }],
    ...(admin ? [[{ text: "🛠 Админка", callback_data: "admin" }]] : []),
  ],
});

const kbBack = { inline_keyboard: [[{ text: "◀️ В меню", callback_data: "menu" }]] };

const kbPlans = {
  inline_keyboard: [
    ...PLANS.map((p) => [{ text: `${p.title} — ${p.rub} ₽`, callback_data: `plan:${p.id}` }]),
    [{ text: "◀️ В меню", callback_data: "menu" }],
  ],
};

function kbPayMethods(env, planId) {
  const rows = [[{ text: "⭐ Telegram Stars", callback_data: `pay:${planId}:stars` }]];
  if (env.CRYPTOBOT_TOKEN)
    rows.push([{ text: "💎 CryptoBot (USDT)", callback_data: `pay:${planId}:crypto` }]);
  if (env.DA_CLIENT_ID && env.DA_CLIENT_SECRET)
    rows.push([{ text: "🎁 DonationAlerts (₽)", callback_data: `pay:${planId}:da` }]);
  rows.push([{ text: "◀️ К тарифам", callback_data: "buy" }]);
  return { inline_keyboard: rows };
}

function welcomeText(from) {
  return (
    `🛡 <b>${BRAND}</b>\n\n` +
    `Привет, ${esc(from.first_name || "друг")}!\n\n` +
    `Это твой личный VPN: быстрые серверы, автообновляемая подписка для ` +
    `V2Ray/Xray-клиентов (Happ, v2rayTun, Hiddify, Streisand, v2rayNG и др.).\n\n` +
    `• 🎁 Пробный период — ${TRIAL_DAYS} дней бесплатно\n` +
    `• 💳 Оплата: Telegram Stars, крипта, DonationAlerts\n` +
    `• 🔄 Серверы обновляются автоматически\n\n` +
    `Выбирай действие 👇`
  );
}

function subLinksText(env, req, user) {
  const base = baseUrl(env, req);
  const u = `${base}/sub/${user.subscription_token}`;
  return (
    `✅ Подписка активна до <b>${fmtDateHuman(user.subscription_expires)}</b>\n\n` +
    `🔗 Твоя ссылка-подписка (вставь в приложение):\n` +
    `<code>${u}</code>\n\n` +
    `Форматы, если клиент не определился автоматически:\n` +
    `• plain: <code>${u}/plain</code>\n` +
    `• base64: <code>${u}/base64</code>\n` +
    `• Xray (Happ): <code>${u}/xray</code>\n` +
    `• sing-box (Hiddify/NekoBox): <code>${u}/singbox</code>\n\n` +
    `📱 «Как подключиться» — пошаговая инструкция.`
  );
}

const HOWTO_TEXT =
  `📱 <b>Как подключиться</b>\n\n` +
  `1️⃣ Установи приложение:\n` +
  `• iOS: Happ, Streisand, v2rayTun\n` +
  `• Android: Happ, v2rayNG, Hiddify, NekoBox\n` +
  `• Windows: Hiddify, v2rayN\n` +
  `• macOS: Happ, Streisand, Hiddify\n\n` +
  `2️⃣ Скопируй свою ссылку-подписку («📊 Моя подписка»).\n\n` +
  `3️⃣ В приложении: «Добавить подписку» → вставь ссылку → обнови список серверов.\n\n` +
  `4️⃣ Выбери сервер и подключайся. Список серверов обновляется автоматически — ` +
  `просто периодически жми «обновить подписку» в приложении.`;

// ── Оплата: Telegram Stars ────────────────────────────────────────────────────

async function sendStarsInvoice(env, chatId, plan) {
  await tg(env, "sendInvoice", {
    chat_id: chatId,
    title: `${BRAND} — ${plan.title}`,
    description: `Подписка ${BRAND} на ${plan.days} дн. Доступ ко всем серверам.`,
    payload: JSON.stringify({ p: plan.id }),
    currency: "XTR",
    prices: [{ label: `${BRAND} ${plan.title}`, amount: plan.stars }],
  });
}

// ── Оплата: CryptoBot (Crypto Pay API) ────────────────────────────────────────

async function cryptoPay(env, method, payload) {
  const resp = await fetch(`https://pay.crypt.bot/api/${method}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Crypto-Pay-API-Token": env.CRYPTOBOT_TOKEN,
    },
    body: JSON.stringify(payload),
  });
  return await resp.json();
}

async function createCryptoInvoice(env, user, plan) {
  const res = await cryptoPay(env, "createInvoice", {
    asset: "USDT",
    amount: plan.usdt,
    description: `${BRAND} — ${plan.title} (${plan.days} дн.)`,
    payload: JSON.stringify({ tg: user.telegram_id, p: plan.id }),
    expires_in: 3600,
  });
  if (!res.ok) throw new Error(`CryptoBot: ${JSON.stringify(res)}`);
  return res.result; // { invoice_id, bot_invoice_url, mini_app_invoice_url, ... }
}

async function verifyCryptoSignature(env, rawBody, signature) {
  const tokenHash = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(env.CRYPTOBOT_TOKEN)
  );
  const key = await crypto.subtle.importKey(
    "raw", tokenHash, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
  const hex = [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return hex === String(signature || "").toLowerCase();
}

// ── Оплата: DonationAlerts ────────────────────────────────────────────────────

async function daTokens(env) {
  const raw = await getSetting(env, "da_tokens");
  return raw ? JSON.parse(raw) : null;
}

async function daRefreshIfNeeded(env) {
  let tokens = await daTokens(env);
  if (!tokens) return null;
  if (tokens.expires_at - Date.now() / 1000 > 3600) return tokens;
  const resp = await fetch("https://www.donationalerts.com/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: tokens.refresh_token,
      client_id: env.DA_CLIENT_ID,
      client_secret: env.DA_CLIENT_SECRET,
      scope: "oauth-user-show oauth-donation-index",
    }),
  });
  if (!resp.ok) {
    console.log(`DA refresh failed: ${resp.status} ${await resp.text()}`);
    return tokens; // попробуем со старым
  }
  const data = await resp.json();
  tokens = {
    access_token: data.access_token,
    refresh_token: data.refresh_token || tokens.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600),
  };
  await setSetting(env, "da_tokens", JSON.stringify(tokens));
  return tokens;
}

/** Создаёт «ожидающий» платёж с кодом, который юзер укажет в сообщении доната. */
async function createDaPending(env, user, plan) {
  const code = randomCode();
  await ex(
    env,
    `INSERT INTO payments (user_id, provider, provider_payment_id, amount, currency, status, period_days)
     VALUES (?, 'donationalerts', ?, ?, 'RUB', 'pending', ?)`,
    [user.id, `code:${code}`, String(plan.rub), plan.days]
  );
  return code;
}

/** Cron: тянем последние донаты и сверяем коды. */
async function pollDonationAlerts(env) {
  if (!env.DA_CLIENT_ID || !env.DA_CLIENT_SECRET) return;
  const tokens = await daRefreshIfNeeded(env);
  if (!tokens) return;

  const pending = await q(
    env,
    `SELECT p.*, u.telegram_id FROM payments p JOIN users u ON u.id = p.user_id
     WHERE p.provider = 'donationalerts' AND p.status = 'pending'
       AND p.created_at > datetime('now', '-7 days')`
  );
  if (!pending.length) return;

  const resp = await fetch("https://www.donationalerts.com/api/v1/alerts/donations", {
    headers: { Authorization: `Bearer ${tokens.access_token}` },
  });
  if (!resp.ok) {
    console.log(`DA donations failed: ${resp.status}`);
    return;
  }
  const data = await resp.json();
  const donations = data.data || [];

  for (const d of donations) {
    const msg = String(d.message || "").toUpperCase();
    if (!msg.includes("VIRA-")) continue;
    for (const p of pending) {
      if (p.status !== "pending") continue;
      const code = String(p.provider_payment_id || "").replace("code:", "");
      if (!code || !msg.includes(code)) continue;
      if (String(d.currency).toUpperCase() === "RUB" && Number(d.amount) < Number(p.amount) * 0.99) {
        continue; // сумма меньше нужной — пусть решает админ
      }
      // уже обработан этот донат?
      const dup = await one(env, "SELECT id FROM payments WHERE provider_payment_id = ?", [
        `da:${d.id}`,
      ]);
      if (dup) continue;

      const user = await one(env, "SELECT * FROM users WHERE id = ?", [p.user_id]);
      if (!user) continue;
      await extendSubscription(env, user, Number(p.period_days));
      await ex(
        env,
        `UPDATE payments SET status = 'paid', paid_at = datetime('now'),
                provider_payment_id = ?, raw_payload = ? WHERE id = ?`,
        [`da:${d.id}`, JSON.stringify(d), p.id]
      );
      p.status = "paid";
      await send(
        env,
        p.telegram_id,
        `🎉 Донат получен! Подписка продлена на <b>${p.period_days} дн.</b>\n` +
          `Открой «📊 Моя подписка», чтобы получить ссылку.`,
        { reply_markup: kbBack }
      );
    }
  }
}

// ── Начисление после оплаты ───────────────────────────────────────────────────

async function creditPayment(env, req, tgId, plan, provider, providerPaymentId, amount, currency, raw) {
  const dup = await one(env, "SELECT id FROM payments WHERE provider_payment_id = ?", [
    providerPaymentId,
  ]);
  if (dup) return; // идемпотентность

  let user = await one(env, "SELECT * FROM users WHERE telegram_id = ?", [tgId]);
  if (!user) {
    await ex(env, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", [tgId]);
    user = await one(env, "SELECT * FROM users WHERE telegram_id = ?", [tgId]);
  }
  user = await extendSubscription(env, user, plan.days);
  await ex(
    env,
    `INSERT INTO payments (user_id, provider, provider_payment_id, amount, currency, status, period_days, raw_payload, paid_at)
     VALUES (?, ?, ?, ?, ?, 'paid', ?, ?, datetime('now'))`,
    [user.id, provider, providerPaymentId, String(amount), currency, plan.days, JSON.stringify(raw ?? null)]
  );
  await send(
    env,
    tgId,
    `🎉 Оплата получена! Подписка активна до <b>${fmtDateHuman(user.subscription_expires)}</b>.\n\n` +
      subLinksText(env, req, user),
    { reply_markup: kbBack }
  );
}

// ── Обработка Telegram-обновлений ─────────────────────────────────────────────

async function handleUpdate(env, req, update, ctx) {
  if (update.pre_checkout_query) {
    await tg(env, "answerPreCheckoutQuery", {
      pre_checkout_query_id: update.pre_checkout_query.id,
      ok: true,
    });
    return;
  }

  if (update.message?.successful_payment) {
    const sp = update.message.successful_payment;
    let planId = null;
    try { planId = JSON.parse(sp.invoice_payload).p; } catch {}
    const plan = planById(planId);
    if (plan) {
      await creditPayment(
        env, req,
        update.message.from.id,
        plan,
        "stars",
        `stars:${sp.telegram_payment_charge_id}`,
        sp.total_amount,
        "XTR",
        sp
      );
    }
    return;
  }

  if (update.callback_query) {
    await handleCallback(env, req, update.callback_query);
    return;
  }

  if (update.message?.text) {
    await handleMessage(env, req, update.message);
  }
}

async function handleMessage(env, req, msg) {
  const from = msg.from;
  const chatId = msg.chat.id;
  const text = msg.text.trim();
  const user = await ensureUser(env, from);
  const admin = isAdmin(env, from.id);

  if (text.startsWith("/start")) {
    await send(env, chatId, welcomeText(from), { reply_markup: kbMain(admin) });
    return;
  }
  if (text === "/help") {
    await send(env, chatId, HOWTO_TEXT, { reply_markup: kbBack });
    return;
  }
  if (text === "/menu") {
    await send(env, chatId, `🛡 <b>${BRAND}</b> — меню`, { reply_markup: kbMain(admin) });
    return;
  }

  // ── админ-команды ──
  if (admin && text.startsWith("/grant")) {
    const m = text.match(/^\/grant\s+(\d+)\s+(-?\d+)/);
    if (!m) {
      await send(env, chatId, "Формат: <code>/grant &lt;telegram_id&gt; &lt;дней&gt;</code>");
      return;
    }
    const [, tgId, days] = m;
    let target = await one(env, "SELECT * FROM users WHERE telegram_id = ?", [tgId]);
    if (!target) {
      await ex(env, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", [tgId]);
      target = await one(env, "SELECT * FROM users WHERE telegram_id = ?", [tgId]);
    }
    target = await extendSubscription(env, target, Number(days));
    await send(
      env, chatId,
      `✅ Пользователю <code>${tgId}</code> выдано ${days} дн. ` +
        `Подписка до ${fmtDateHuman(target.subscription_expires)}.`
    );
    await send(
      env, tgId,
      `🎁 Тебе выдана подписка ${BRAND} на <b>${days} дн.</b> Открой «📊 Моя подписка».`,
      { reply_markup: kbBack }
    ).catch(() => {});
    return;
  }

  if (admin && text.startsWith("/find")) {
    const m = text.match(/^\/find\s+(\S+)/);
    if (!m) { await send(env, chatId, "Формат: <code>/find &lt;telegram_id или @username&gt;</code>"); return; }
    const key = m[1].replace(/^@/, "");
    const target = /^\d+$/.test(key)
      ? await one(env, "SELECT * FROM users WHERE telegram_id = ?", [key])
      : await one(env, "SELECT * FROM users WHERE username = ?", [key]);
    if (!target) { await send(env, chatId, "Не найден."); return; }
    await send(
      env, chatId,
      `👤 <b>${esc(target.full_name || "—")}</b> @${esc(target.username || "—")}\n` +
        `id: <code>${target.telegram_id}</code>\n` +
        `триал: ${Number(target.trial_used) ? "использован" : "нет"}\n` +
        `подписка до: ${fmtDateHuman(target.subscription_expires)}\n` +
        `токен: <code>${esc(target.subscription_token || "—")}</code>`
    );
    return;
  }

  if (admin && text.startsWith("/broadcast")) {
    const body = text.replace(/^\/broadcast\s*/, "");
    if (!body) {
      await send(env, chatId, "Формат: <code>/broadcast &lt;текст сообщения&gt;</code>\nПоддерживается HTML-разметка.");
      return;
    }
    await ex(env, "INSERT INTO broadcasts (text, status, created_by) VALUES (?, 'draft', ?)", [
      body, from.id,
    ]);
    const row = await one(env, "SELECT id FROM broadcasts ORDER BY id DESC LIMIT 1");
    const total = await one(env, "SELECT COUNT(*) AS c FROM users");
    await send(
      env, chatId,
      `📢 <b>Предпросмотр рассылки</b> (получателей: ${total.c}):\n\n${body}`,
      {
        reply_markup: {
          inline_keyboard: [[
            { text: "🚀 Отправить", callback_data: `bc:send:${row.id}` },
            { text: "❌ Отмена", callback_data: `bc:cancel:${row.id}` },
          ]],
        },
      }
    );
    return;
  }

  if (admin && text === "/admin") {
    await sendAdminPanel(env, chatId);
    return;
  }

  await send(env, chatId, `Не понял 🤔 Вот меню:`, { reply_markup: kbMain(admin) });
}

async function sendAdminPanel(env, chatId, cq = null) {
  const kb = {
    inline_keyboard: [
      [{ text: "👥 Статистика", callback_data: "admin:stats" }],
      [{ text: "📢 Рассылка", callback_data: "admin:bc" }],
      [{ text: "◀️ В меню", callback_data: "menu" }],
    ],
  };
  const text =
    `🛠 <b>Админка ${BRAND}</b>\n\n` +
    `Команды:\n` +
    `• <code>/grant &lt;tg_id&gt; &lt;дней&gt;</code> — выдать подписку\n` +
    `• <code>/find &lt;tg_id|@username&gt;</code> — найти пользователя\n` +
    `• <code>/broadcast &lt;текст&gt;</code> — рассылка всем`;
  if (cq) await editOrSend(env, cq, text, { reply_markup: kb });
  else await send(env, chatId, text, { reply_markup: kb });
}

async function handleCallback(env, req, cq) {
  const from = cq.from;
  const data = cq.data || "";
  const user = await ensureUser(env, from);
  const admin = isAdmin(env, from.id);
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });

  if (data === "menu") {
    await editOrSend(env, cq, welcomeText(from), { reply_markup: kbMain(admin) });
    return;
  }

  if (data === "sub") {
    if (subIsActive(user)) {
      await editOrSend(env, cq, subLinksText(env, req, user), { reply_markup: kbBack });
    } else {
      const expired = user.subscription_expires
        ? `Подписка закончилась ${fmtDateHuman(user.subscription_expires)}.`
        : `У тебя ещё нет подписки.`;
      await editOrSend(
        env, cq,
        `😔 ${expired}\n\nОформи подписку или попробуй бесплатный период 👇`,
        {
          reply_markup: {
            inline_keyboard: [
              [{ text: "💳 Купить подписку", callback_data: "buy" }],
              ...(Number(user.trial_used) ? [] : [[{ text: `🎁 Триал ${TRIAL_DAYS} дн.`, callback_data: "trial" }]]),
              [{ text: "◀️ В меню", callback_data: "menu" }],
            ],
          },
        }
      );
    }
    return;
  }

  if (data === "buy") {
    await editOrSend(
      env, cq,
      `💳 <b>Тарифы ${BRAND}</b>\n\n` +
        PLANS.map((p) => `• <b>${p.title}</b> — ${p.rub} ₽ / ${p.stars} ⭐ / ${p.usdt} USDT`).join("\n") +
        `\n\nВыбери тариф:`,
      { reply_markup: kbPlans }
    );
    return;
  }

  if (data === "trial") {
    if (Number(user.trial_used)) {
      await editOrSend(
        env, cq,
        `🙈 Пробный период уже был использован.\n\nОформи подписку 👇`,
        { reply_markup: { inline_keyboard: [
          [{ text: "💳 Купить подписку", callback_data: "buy" }],
          [{ text: "◀️ В меню", callback_data: "menu" }],
        ] } }
      );
      return;
    }
    await ex(env, "UPDATE users SET trial_used = 1 WHERE id = ?", [user.id]);
    const updated = await extendSubscription(env, { ...user, trial_used: 1 }, TRIAL_DAYS);
    await editOrSend(
      env, cq,
      `🎁 Пробный период на <b>${TRIAL_DAYS} дн.</b> активирован!\n\n` +
        subLinksText(env, req, updated),
      { reply_markup: kbBack }
    );
    return;
  }

  if (data === "howto") {
    await editOrSend(env, cq, HOWTO_TEXT, { reply_markup: kbBack });
    return;
  }

  if (data.startsWith("plan:")) {
    const plan = planById(data.slice(5));
    if (!plan) return;
    await editOrSend(
      env, cq,
      `<b>${plan.title}</b> — ${plan.days} дн.\n\n` +
        `• ⭐ Telegram Stars: <b>${plan.stars} ⭐</b>\n` +
        (env.CRYPTOBOT_TOKEN ? `• 💎 CryptoBot: <b>${plan.usdt} USDT</b>\n` : "") +
        (env.DA_CLIENT_ID ? `• 🎁 DonationAlerts: <b>${plan.rub} ₽</b>\n` : "") +
        `\nВыбери способ оплаты:`,
      { reply_markup: kbPayMethods(env, plan.id) }
    );
    return;
  }

  if (data.startsWith("pay:")) {
    const [, planId, method] = data.split(":");
    const plan = planById(planId);
    if (!plan) return;

    if (method === "stars") {
      await sendStarsInvoice(env, cq.message.chat.id, plan);
      return;
    }

    if (method === "crypto" && env.CRYPTOBOT_TOKEN) {
      try {
        const inv = await createCryptoInvoice(env, user, plan);
        await editOrSend(
          env, cq,
          `💎 Счёт на <b>${plan.usdt} USDT</b> (${plan.title}) создан.\n` +
            `Оплати в течение часа — подписка активируется автоматически.`,
          { reply_markup: { inline_keyboard: [
            [{ text: "💳 Оплатить в CryptoBot", url: inv.bot_invoice_url }],
            [{ text: "◀️ В меню", callback_data: "menu" }],
          ] } }
        );
      } catch (e) {
        console.log(String(e));
        await editOrSend(env, cq, "😔 Не удалось создать счёт CryptoBot. Попробуй позже.", { reply_markup: kbBack });
      }
      return;
    }

    if (method === "da" && env.DA_CLIENT_ID) {
      const code = await createDaPending(env, user, plan);
      const daName = env.DA_USERNAME ? `https://www.donationalerts.com/r/${env.DA_USERNAME}` : null;
      await editOrSend(
        env, cq,
        `🎁 <b>Оплата через DonationAlerts</b>\n\n` +
          `1️⃣ Отправь донат на сумму <b>не менее ${plan.rub} ₽</b>\n` +
          `2️⃣ В сообщении доната укажи код:\n\n<code>${code}</code>\n\n` +
          `Проверка проходит автоматически раз в минуту — как только донат дойдёт, ` +
          `я продлю подписку и напишу тебе. Код действует 7 дней.`,
        { reply_markup: { inline_keyboard: [
          ...(daName ? [[{ text: "🎁 Открыть страницу доната", url: daName }]] : []),
          [{ text: "◀️ В меню", callback_data: "menu" }],
        ] } }
      );
      return;
    }
    return;
  }

  // ── админ-колбэки ──
  if (!admin) return;

  if (data === "admin") {
    await sendAdminPanel(env, cq.message.chat.id, cq);
    return;
  }

  if (data === "admin:stats") {
    const [users, active, trials, payStats] = await Promise.all([
      one(env, "SELECT COUNT(*) AS c FROM users"),
      one(env, "SELECT COUNT(*) AS c FROM users WHERE subscription_expires > datetime('now')"),
      one(env, "SELECT COUNT(*) AS c FROM users WHERE trial_used = 1"),
      q(env, `SELECT provider, currency, COUNT(*) AS n, SUM(CAST(amount AS REAL)) AS total
              FROM payments WHERE status = 'paid' GROUP BY provider, currency`),
    ]);
    const payLines = payStats.length
      ? payStats.map((r) => `• ${r.provider}: ${r.n} шт., ${Number(r.total).toFixed(2)} ${r.currency}`).join("\n")
      : "— пока нет";
    await editOrSend(
      env, cq,
      `👥 <b>Статистика</b>\n\n` +
        `Пользователей: <b>${users.c}</b>\n` +
        `Активных подписок: <b>${active.c}</b>\n` +
        `Использовали триал: <b>${trials.c}</b>\n\n` +
        `💰 Оплаты:\n${payLines}`,
      { reply_markup: { inline_keyboard: [
        [{ text: "🔄 Обновить", callback_data: "admin:stats" }],
        [{ text: "◀️ Админка", callback_data: "admin" }],
      ] } }
    );
    return;
  }

  if (data === "admin:bc") {
    await editOrSend(
      env, cq,
      `📢 <b>Рассылка</b>\n\nОтправь команду:\n<code>/broadcast &lt;текст&gt;</code>\n\n` +
        `Я покажу предпросмотр и попрошу подтвердить. Отправка идёт пачками по ` +
        `${BROADCAST_BATCH} сообщений в минуту (через cron).`,
      { reply_markup: { inline_keyboard: [[{ text: "◀️ Админка", callback_data: "admin" }]] } }
    );
    return;
  }

  if (data.startsWith("bc:send:")) {
    const id = data.split(":")[2];
    await ex(env, "UPDATE broadcasts SET status = 'queued' WHERE id = ? AND status = 'draft'", [id]);
    await editOrSend(env, cq, `🚀 Рассылка #${id} поставлена в очередь. Пришлю отчёт по завершении.`);
    return;
  }

  if (data.startsWith("bc:cancel:")) {
    const id = data.split(":")[2];
    await ex(env, "UPDATE broadcasts SET status = 'cancelled' WHERE id = ? AND status = 'draft'", [id]);
    await editOrSend(env, cq, `❌ Рассылка #${id} отменена.`);
    return;
  }
}

// ── Cron: рассылка и напоминания ──────────────────────────────────────────────

async function processBroadcasts(env) {
  const bc = await one(
    env, "SELECT * FROM broadcasts WHERE status = 'queued' ORDER BY id LIMIT 1"
  );
  if (!bc) return;
  const users = await q(
    env,
    "SELECT id, telegram_id FROM users WHERE id > ? ORDER BY id LIMIT ?",
    [bc.cursor, BROADCAST_BATCH]
  );
  let sent = 0, failed = 0, cursor = Number(bc.cursor);
  for (const u of users) {
    const res = await send(env, u.telegram_id, bc.text).catch(() => ({ ok: false }));
    if (res.ok) sent++; else failed++;
    cursor = Number(u.id);
  }
  const done = users.length < BROADCAST_BATCH;
  await ex(
    env,
    "UPDATE broadcasts SET cursor = ?, sent = sent + ?, failed = failed + ?, status = ? WHERE id = ?",
    [cursor, sent, failed, done ? "done" : "queued", bc.id]
  );
  if (done && bc.created_by) {
    const fin = await one(env, "SELECT * FROM broadcasts WHERE id = ?", [bc.id]);
    await send(
      env, bc.created_by,
      `📢 Рассылка #${bc.id} завершена.\nДоставлено: ${fin.sent}, ошибок: ${fin.failed}.`
    ).catch(() => {});
  }
}

async function remindExpiring(env) {
  const soon = await q(
    env,
    `SELECT * FROM users
     WHERE subscription_expires > datetime('now')
       AND subscription_expires < datetime('now', '+${REMIND_HOURS} hours')
     LIMIT 20`
  );
  for (const u of soon) {
    const key = `rm:${u.id}:${u.subscription_expires}`;
    const already = await getSetting(env, key);
    if (already) continue;
    await setSetting(env, key, "1");
    await send(
      env, u.telegram_id,
      `⏰ Подписка ${BRAND} закончится <b>${fmtDateHuman(u.subscription_expires)}</b>.\n` +
        `Продли заранее, чтобы не остаться без VPN 👇`,
      { reply_markup: { inline_keyboard: [
        [{ text: "💳 Продлить", callback_data: "buy" }],
      ] } }
    ).catch(() => {});
  }
}

// ── Отдача подписок ───────────────────────────────────────────────────────────

function detectFormat(ua) {
  const s = (ua || "").toLowerCase();
  if (s.includes("happ")) return "xray";
  if (s.includes("sing-box") || s.includes("singbox") || s.includes("sfa") || s.includes("hiddify") || s.includes("nekobox"))
    return "singbox";
  return "base64";
}

async function serveSubscription(env, req, token, fmt) {
  const user = await one(env, "SELECT * FROM users WHERE subscription_token = ?", [token]);
  if (!user) return textResp("not found", 404);
  if (!subIsActive(user)) return textResp("subscription expired", 403);

  if (!fmt) fmt = detectFormat(req.headers.get("user-agent"));

  const headers = {
    "profile-title": `base64:${b64encode(`🛡 ${BRAND}`)}`,
    "profile-update-interval": "6",
    "subscription-userinfo": `upload=0; download=0; total=0; expire=${Math.floor(
      (parseSqliteDate(user.subscription_expires)?.getTime() || 0) / 1000
    )}`,
  };

  if (fmt === "plain" || fmt === "base64") {
    const flat = await getSetting(env, "subscription");
    if (!flat) return textResp("subscription is being rebuilt, try later", 503);
    const body = fmt === "base64" ? b64encode(flat) : flat;
    return new Response(body, {
      headers: { "content-type": "text/plain; charset=utf-8", ...headers },
    });
  }
  if (fmt === "xray") {
    const raw = await getSetting(env, "subscription_xray");
    if (!raw) return textResp("not ready", 503);
    return new Response(raw, {
      headers: { "content-type": "application/json; charset=utf-8", ...headers },
    });
  }
  if (fmt === "singbox") {
    const raw = await getSetting(env, "subscription_singbox");
    if (!raw) return textResp("not ready", 503);
    return new Response(raw, {
      headers: { "content-type": "application/json; charset=utf-8", ...headers },
    });
  }
  if (fmt === "manifest") {
    const raw = await getSetting(env, "subscription_data");
    if (!raw) return textResp("not ready", 503);
    return new Response(raw, {
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  }
  return textResp("unknown format", 400);
}

// ── HTTP-роутинг ──────────────────────────────────────────────────────────────

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    try {
      // Telegram webhook
      if (path === "/tg/webhook" && req.method === "POST") {
        const secret = req.headers.get("x-telegram-bot-api-secret-token");
        if (secret !== env.TG_WEBHOOK_SECRET) return textResp("forbidden", 403);
        const update = await req.json();
        ctx.waitUntil(
          handleUpdate(env, req, update, ctx).catch((e) => console.log("update error:", String(e)))
        );
        return textResp("ok");
      }

      // CryptoBot webhook
      if (path === "/cryptobot/webhook" && req.method === "POST") {
        if (!env.CRYPTOBOT_TOKEN) return textResp("disabled", 404);
        const raw = await req.text();
        const sig = req.headers.get("crypto-pay-api-signature");
        if (!(await verifyCryptoSignature(env, raw, sig))) return textResp("bad signature", 403);
        const update = JSON.parse(raw);
        if (update.update_type === "invoice_paid") {
          const inv = update.payload;
          let meta = {};
          try { meta = JSON.parse(inv.payload || "{}"); } catch {}
          const plan = planById(meta.p);
          if (plan && meta.tg) {
            ctx.waitUntil(
              creditPayment(
                env, req, meta.tg, plan, "cryptobot",
                `cryptobot:${inv.invoice_id}`, inv.amount, inv.asset, inv
              ).catch((e) => console.log("cryptobot credit error:", String(e)))
            );
          }
        }
        return textResp("ok");
      }

      // Первичная настройка
      if (path === "/init") {
        if (url.searchParams.get("secret") !== env.TG_WEBHOOK_SECRET)
          return textResp("forbidden", 403);
        await initDb(env);
        const hook = await tg(env, "setWebhook", {
          url: `${url.protocol}//${url.host}/tg/webhook`,
          secret_token: env.TG_WEBHOOK_SECRET,
          allowed_updates: ["message", "callback_query", "pre_checkout_query"],
          drop_pending_updates: false,
        });
        return jsonResp({ ok: true, db: "ready", webhook: hook });
      }

      // DonationAlerts OAuth
      if (path === "/da/login") {
        if (url.searchParams.get("secret") !== env.TG_WEBHOOK_SECRET)
          return textResp("forbidden", 403);
        if (!env.DA_CLIENT_ID) return textResp("DA_CLIENT_ID не задан", 500);
        const redirect = `${url.protocol}//${url.host}/da/callback`;
        const authUrl =
          `https://www.donationalerts.com/oauth/authorize?client_id=${encodeURIComponent(env.DA_CLIENT_ID)}` +
          `&redirect_uri=${encodeURIComponent(redirect)}&response_type=code` +
          `&scope=${encodeURIComponent("oauth-user-show oauth-donation-index")}`;
        return Response.redirect(authUrl, 302);
      }

      if (path === "/da/callback") {
        const code = url.searchParams.get("code");
        if (!code) return textResp("no code", 400);
        const redirect = `${url.protocol}//${url.host}/da/callback`;
        const resp = await fetch("https://www.donationalerts.com/oauth/token", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "authorization_code",
            client_id: env.DA_CLIENT_ID,
            client_secret: env.DA_CLIENT_SECRET,
            redirect_uri: redirect,
            code,
          }),
        });
        if (!resp.ok) return textResp(`DA token error: ${await resp.text()}`, 502);
        const data = await resp.json();
        await setSetting(
          env, "da_tokens",
          JSON.stringify({
            access_token: data.access_token,
            refresh_token: data.refresh_token,
            expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600),
          })
        );
        return textResp("DonationAlerts подключён ✅ Можно закрыть страницу.");
      }

      // Подписки: /sub/:token[/:format]
      const subMatch = path.match(/^\/sub\/([A-Za-z0-9_-]+)(?:\/(plain|base64|xray|singbox|manifest))?$/);
      if (subMatch) {
        return await serveSubscription(env, req, subMatch[1], subMatch[2] || null);
      }

      if (path === "/status") {
        const generated = await getSetting(env, "subscription_data");
        let generatedAt = null;
        try { generatedAt = JSON.parse(generated || "{}").generated_at || null; } catch {}
        return jsonResp({ ok: true, service: BRAND, subscription_generated_at: generatedAt });
      }

      return textResp("ViraVPN worker. See /status", 404);
    } catch (e) {
      console.log("fetch error:", String(e && e.stack || e));
      return textResp("internal error", 500);
    }
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        try { await pollDonationAlerts(env); } catch (e) { console.log("DA poll error:", String(e)); }
        try { await processBroadcasts(env); } catch (e) { console.log("broadcast error:", String(e)); }
        try { await remindExpiring(env); } catch (e) { console.log("remind error:", String(e)); }
      })()
    );
  },
};
