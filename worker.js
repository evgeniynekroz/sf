// ═══════════════════════════════════════════════════════════════════════════════
//  NekrozVPN Worker  —  Subscription + Telegram Bot
//  Cloudflare Workers (ES modules)
// ═══════════════════════════════════════════════════════════════════════════════

// ── Конфиг ────────────────────────────────────────────────────────────────────
const BOT_TOKEN      = "8146025356:AAHcEXEdQ_R00E9ZGnTWcmUWbPor5vCE9Ro";
const TURSO_URL      = "https://nekrozvpn-evgen.aws-eu-west-1.turso.io";
const TURSO_TOKEN    = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODI1Nzk2MTAsImlkIjoiMDE5ZjBhMDYtMWUwMS03MTIwLTg3ZGMtYWEyMmYxMjk3OGJhIiwicmlkIjoiNGZjYmQwOTAtODA0OS00ZjAwLWExN2ItNjY1Y2E2MDE0ZDVkIn0.Hsq1HO-Y7kB5l_O9QspI33eomZUAvWHfdfEXAxXZ8EmJmiC37FmkAXanQqazPsFf3uvds8vcfK1Ak_KDtkYgCg";
const ADMIN_ID       = 6168325401;
const CHANNEL_MAIN   = "@NekrozVPN";
const CHANNEL_BONUS  = "@nekrozxxxgod";
const TRIAL_DAYS     = 5;
const BONUS_DAYS     = 2;    // +2 дня за подписку на личный канал
const REF_START_DAYS = 3;    // рефереру когда реферал нажал /start
const REF_PAY_DAYS   = 4;    // рефереру когда реферал оплатил
const MONTHLY_BONUS  = 1;    // день в месяц за подписку на личный канал
const SUPPORT_EMAIL  = "nekrozvpn@mail.ru";
const BOT_USERNAME   = "NekrozVPN_bot";
const TEST_TOKEN     = "nekroz-test";
const PROFILE_NAME   = "👑 NekrozVPN";
const SUB_BASE_URL   = "https://nekrozvpn-sub.evgen20111110.workers.dev";

// ── Периоды и цены ──────────────────────────────────────────────────────────
const PERIODS = {
  1:  { stars: 5,   rubles: null },
  14: { stars: 65,  rubles: 95 },
  30: { stars: 99,  rubles: 139 },
  50: { stars: 139, rubles: 179 },
};
const PERIOD_LIST = [1, 14, 30, 50];
const GIFT_PERIOD  = 30; // фиксированный период для подарков

// ── Устройства ────────────────────────────────────────────────────────────────
const DEVICE_LIMIT_FREE  = 2;     // сколько устройств включено бесплатно
const DEVICE_PRICE_RUB   = 50;    // цена докупки 1 устройства
const DEVICE_PRICE_STARS = 30;

// ── Роутер ────────────────────────────────────────────────────────────────────
export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/bot") {
      try {
        const upd = await request.json();
        await handleUpdate(upd);
      } catch (e) {
        console.error("handleUpdate error:", e);
      }
      return new Response("ok");
    }

    return handleSubscription(url, request);
  },

  async scheduled() {
    await handleCron();
  },
};

// ═══════════════════════════════════════════════════════════════════════════════
//  TURSO
// ═══════════════════════════════════════════════════════════════════════════════

function tArg(v) {
  if (v == null)             return { type: "null" };
  if (typeof v === "number") return { type: "integer", value: String(v) };
  return { type: "text", value: String(v) };
}

async function db(sql, args = []) {
  const r = await fetch(`${TURSO_URL}/v2/pipeline`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TURSO_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      requests: [
        { type: "execute", stmt: { sql, args: args.map(tArg) } },
        { type: "close" },
      ],
    }),
  });
  const data = await r.json();

  const step = data.results?.[0];
  if (step?.type === "error") {
    const isDuplicateColumn = /duplicate column name/i.test(step.error?.message || "");
    if (!isDuplicateColumn) {
      console.error("Turso error:", JSON.stringify(step.error), "SQL:", sql);
    }
    return [];
  }

  const result = step?.response?.result;
  if (!result) return [];

  // Диагностика "тихих" UPDATE/INSERT, которые ни на что не повлияли —
  // раньше это было совершенно невидимо и создавало баги вида "запись не дошла".
  const isWrite = /^\s*(update|insert|delete)/i.test(sql);
  if (isWrite && result.affected_row_count === 0) {
    console.error("Turso write matched 0 rows! SQL:", sql, "args:", JSON.stringify(args));
  }

  const cols = result.cols.map(c => c.name);
  return result.rows.map(row =>
    Object.fromEntries(cols.map((col, i) => {
      const cell = row[i];
      let v = cell?.value ?? null;
      if (v !== null && (cell?.type === "integer" || cell?.type === "float")) {
        v = Number(v);
      }
      return [col, v];
    }))
  );
}

async function dbRun(sql, args = []) { await db(sql, args); }

let dbReady = false;

async function initDB() {
  if (dbReady) return;
  const stmts = [
    { sql: `CREATE TABLE IF NOT EXISTS users (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id         INTEGER UNIQUE NOT NULL,
        username            TEXT,
        full_name           TEXT,
        state               TEXT,
        trial_used          INTEGER DEFAULT 0,
        subscription_token  TEXT UNIQUE,
        subscription_expires TEXT,
        referred_by         INTEGER,
        ref_start_given     INTEGER DEFAULT 0,
        ref_pay_given       INTEGER DEFAULT 0,
        bonus_month         TEXT,
        reminder_sent       INTEGER DEFAULT 0,
        created_at          TEXT DEFAULT (datetime('now'))
      )` },
    { sql: `CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)` },
    { sql: `CREATE TABLE IF NOT EXISTS payments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id  INTEGER NOT NULL,
        type         TEXT NOT NULL,
        amount       TEXT,
        status       TEXT DEFAULT 'pending',
        charge_id    TEXT,
        created_at   TEXT DEFAULT (datetime('now')),
        confirmed_at TEXT
      )` },
    { sql: `CREATE TABLE IF NOT EXISTS promo_codes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        code      TEXT UNIQUE NOT NULL,
        days      INTEGER NOT NULL,
        max_uses  INTEGER DEFAULT 1,
        uses      INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
      )` },
    { sql: `CREATE TABLE IF NOT EXISTS support_msgs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        text        TEXT NOT NULL,
        fwd_msg_id  INTEGER,
        status      TEXT DEFAULT 'open',
        created_at  TEXT DEFAULT (datetime('now'))
      )` },
    { sql: `CREATE TABLE IF NOT EXISTS devices (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id  INTEGER NOT NULL,
        fingerprint  TEXT NOT NULL,
        label        TEXT,
        first_seen   TEXT DEFAULT (datetime('now')),
        last_seen    TEXT DEFAULT (datetime('now')),
        UNIQUE(telegram_id, fingerprint)
      )` },
  ];

  await fetch(`${TURSO_URL}/v2/pipeline`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TURSO_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      requests: stmts.flatMap(s => [{ type: "execute", stmt: { sql: s.sql, args: [] } }])
        .concat([{ type: "close" }]),
    }),
  });

  // Миграции: раньше в проде обнаружилась таблица users БЕЗ части колонок
  // из CREATE TABLE (она создавалась ДО того, как колонки попали в схему,
  // а IF NOT EXISTS в такой ситуации ничего не добавляет). Из-за этого падал
  // ЦЕЛЫЙ UPDATE ("no such column: reminder_sent"), включая subscription_expires
  // в том же запросе — отсюда "запись не доходит до базы". Проходим по всем
  // колонкам users одним циклом — если колонка уже есть, ALTER просто
  // залогирует ошибку и ничего не сломает.
  const USER_COLUMNS = [
    ["state", "TEXT"],
    ["trial_used", "INTEGER DEFAULT 0"],
    ["subscription_token", "TEXT"],
    ["subscription_expires", "TEXT"],
    ["referred_by", "INTEGER"],
    ["ref_start_given", "INTEGER DEFAULT 0"],
    ["ref_pay_given", "INTEGER DEFAULT 0"],
    ["bonus_month", "TEXT"],
    ["reminder_sent", "INTEGER DEFAULT 0"],
    ["traffic_used_mb", "INTEGER DEFAULT 0"],
    ["extra_device_slots", "INTEGER DEFAULT 0"],
    ["banned", "INTEGER DEFAULT 0"],
    ["ban_reason", "TEXT"],
    ["active_promo_code", "TEXT"],
    ["active_promo_type", "TEXT"],
    ["active_promo_value", "REAL"],
    ["enabled_categories", "TEXT"],
  ];
  for (const [col, def] of USER_COLUMNS) {
    await dbRun(`ALTER TABLE users ADD COLUMN ${col} ${def}`);
  }
  await dbRun("ALTER TABLE devices ADD COLUMN confirmed INTEGER DEFAULT 1");
  await dbRun("ALTER TABLE payments ADD COLUMN days INTEGER");
  await dbRun("ALTER TABLE promo_codes ADD COLUMN type TEXT DEFAULT 'days'");
  await dbRun("ALTER TABLE promo_codes ADD COLUMN value REAL DEFAULT 0");
  await dbRun(`CREATE TABLE IF NOT EXISTS pending_grants (
      username   TEXT PRIMARY KEY,
      days       INTEGER NOT NULL,
      note       TEXT,
      created_at TEXT DEFAULT (datetime('now'))
    )`);

  dbReady = true;
}

// ── Хелперы БД ────────────────────────────────────────────────────────────────
async function getUser(tid) {
  const r = await db("SELECT * FROM users WHERE telegram_id = ? LIMIT 1", [tid]);
  return r[0] ?? null;
}

async function upsertUser(tid, username, fullName) {
  await dbRun(`INSERT INTO users (telegram_id, username, full_name)
    VALUES (?, ?, ?)
    ON CONFLICT(telegram_id) DO UPDATE SET
      username = excluded.username, full_name = excluded.full_name`,
    [tid, username, fullName]);
}

function isActive(user) {
  if (!user?.subscription_expires) return false;
  return new Date(user.subscription_expires) > new Date();
}

function daysLeft(user) {
  if (!user?.subscription_expires) return 0;
  const ms = new Date(user.subscription_expires) - new Date();
  return Math.max(0, Math.ceil(ms / 86400000));
}

async function extendSub(tid, days) {
  const user = await getUser(tid);
  const now  = new Date();
  let base   = now;
  if (user?.subscription_expires) {
    const cur = new Date(user.subscription_expires);
    if (cur > now) base = cur;
  }
  const newExp = new Date(base.getTime() + days * 86400000).toISOString();

  for (let attempt = 1; attempt <= 2; attempt++) {
    await dbRun("UPDATE users SET subscription_expires = ?, reminder_sent = 0 WHERE telegram_id = ?",
      [newExp, tid]);
    // Проверяем, что запись реально дошла — раньше это никак не проверялось,
    // и UPDATE, который ни на что не повлиял, был совершенно невидим.
    const check = await getUser(tid);
    if (check?.subscription_expires === newExp) {
      return newExp;
    }
    console.error(`extendSub: попытка ${attempt} не подтвердилась для tid=${tid}, ` +
      `ожидали "${newExp}", получили "${check?.subscription_expires}"`);
  }
  console.error(`extendSub: ОБЕ попытки провалились для tid=${tid}, дней=${days}`);
  return newExp; // возвращаем как есть — вызывающий код всё равно покажет это пользователю
}

async function generateToken(tid) {
  const arr   = new Uint8Array(16);
  crypto.getRandomValues(arr);
  const h     = Array.from(arr).map(b => b.toString(16).padStart(2,"0")).join("");
  const token = `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  await dbRun("UPDATE users SET subscription_token = ? WHERE telegram_id = ?", [token, tid]);
  return token;
}

async function getOrCreateToken(tid) {
  const user = await getUser(tid);
  if (user?.subscription_token) return user.subscription_token;
  return generateToken(tid);
}

function subUrl(token) { return `${SUB_BASE_URL}/?token=${token}`; }

async function logPayment(tid, type, amount = null, chargeId = null, days = null) {
  const r = await db(
    "INSERT INTO payments (telegram_id,type,amount,charge_id,days) VALUES (?,?,?,?,?) RETURNING id",
    [tid, type, amount, chargeId, days]);
  return r[0]?.id;
}

async function confirmPayment(payId) {
  const r = await db(
    "SELECT telegram_id FROM payments WHERE id = ? AND type = 'rubles' AND status = 'pending' LIMIT 1",
    [payId]);
  if (!r[0]) return null;
  await dbRun(
    "UPDATE payments SET status='confirmed', confirmed_at=datetime('now') WHERE id = ?",
    [payId]);
  return r[0].telegram_id;
}

async function pendingRubles() {
  return db(`SELECT p.id, p.telegram_id, p.created_at, u.username, u.full_name
    FROM payments p LEFT JOIN users u USING(telegram_id)
    WHERE p.type='rubles' AND p.status='pending' ORDER BY p.created_at`);
}

async function setState(tid, state) {
  await dbRun("UPDATE users SET state = ? WHERE telegram_id = ?", [state, tid]);
}

// ═══════════════════════════════════════════════════════════════════════════════
//  TELEGRAM API
// ═══════════════════════════════════════════════════════════════════════════════

async function tg(method, body = {}) {
  const r = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!data.ok) {
    console.error(`tg(${method}) failed:`, JSON.stringify(data));
  }
  return data;
}

async function send(chatId, text, extra = {}) {
  return tg("sendMessage", { chat_id: chatId, text, parse_mode: "HTML", ...extra });
}

async function edit(chatId, msgId, text, extra = {}) {
  return tg("editMessageText",
    { chat_id: chatId, message_id: msgId, text, parse_mode: "HTML", ...extra });
}

async function answerCb(id, text = "", alert = false) {
  return tg("answerCallbackQuery", { callback_query_id: id, text, show_alert: alert });
}

// ИСПРАВЛЕНО: обработка ошибок и проверка ok
async function isMember(chatId, userId) {
  try {
    const r = await tg("getChatMember", { chat_id: chatId, user_id: userId });
    if (!r.ok) return false;
    const status = r.result?.status;
    return ["creator","administrator","member","restricted"].includes(status);
  } catch {
    return false;
  }
}

function kb(buttons) {
  return { inline_keyboard: buttons };
}

function back(cb) {
  return [{ text: "← Назад", callback_data: cb }];
}

// ═══════════════════════════════════════════════════════════════════════════════
//  КЛАВИАТУРЫ
// ═══════════════════════════════════════════════════════════════════════════════

function kbMain(user, isAdmin) {
  const rows = [];
  if (!isActive(user) && !user?.trial_used)
    rows.push([{ text: "🎁 Попробовать бесплатно — 5 дней", callback_data: "trial" }]);
  if (isActive(user))
    rows.push([{ text: "📋 Мой кабинет", callback_data: "cabinet" }]);
  rows.push([{ text: "💳 Купить подписку", callback_data: "buy" }]);
  if (!isActive(user) && user?.trial_used)
    rows.push([{ text: "📋 Мой кабинет", callback_data: "cabinet" }]);
  rows.push(
    [{ text: "👥 Реферальная программа", callback_data: "referral" }],
    [{ text: "🎟 Промокод", callback_data: "promo" }, { text: "🎁 Подарить подписку", callback_data: "gift" }],
    [{ text: "❓ FAQ", callback_data: "faq" }, { text: "🆘 Поддержка", callback_data: "support" }],
  );
  if (isAdmin)
    rows.push([{ text: "⚙️ Панель администратора", callback_data: "admin" }]);
  return kb(rows);
}

function kbPeriods() {
  const buttons = PERIOD_LIST.map(days => {
    const p = PERIODS[days];
    let label = `${days} дн. — ⭐${p.stars}`;
    if (p.rubles) label += ` / ${p.rubles}₽`;
    return { text: label, callback_data: `period_${days}` };
  });
  const rows = [];
  for (let i = 0; i < buttons.length; i += 2) rows.push(buttons.slice(i, i + 2));
  rows.push(back("menu"));
  return kb(rows);
}

function kbPayOptions(period) {
  const p = PERIODS[period];
  const rows = [];
  rows.push([{ text: `⭐ Оплатить ${p.stars} Stars`, callback_data: `pay_stars_${period}` }]);
  if (p.rubles) rows.push([{ text: `💳 Оплатить ${p.rubles} ₽`, callback_data: `pay_rubles_${period}` }]);
  rows.push(back("buy"));
  return kb(rows);
}

// ═══════════════════════════════════════════════════════════════════════════════
//  ТЕКСТЫ
// ═══════════════════════════════════════════════════════════════════════════════

function txtWelcome(name, hasActive) {
  if (hasActive)
    return `👋 С возвращением, <b>${name}</b>!\n\n<b>👑 NekrozVPN</b> — твой доступ к свободному интернету.`;
  return `👋 Привет, <b>${name}</b>!\n\n<b>👑 NekrozVPN</b> — быстрый и надёжный VPN.\nЕсть поставщики по всему миру, автообновление серверов каждый час.\n\nВыбери действие 👇`;
}

function txtCabinet(user, token) {
  const url    = subUrl(token);
  const active = isActive(user);
  const left   = daysLeft(user);
  const exp    = user.subscription_expires
    ? new Date(user.subscription_expires).toLocaleString("ru-RU",
        { day:"2-digit", month:"2-digit", year:"numeric", hour:"2-digit", minute:"2-digit" })
    : "—";

  return `<b>📋 Личный кабинет</b>

${user.banned ? `<blockquote>🚫 <b>Подписка заблокирована</b>\nПричина: ${user.ban_reason || "—"}\nПиши в поддержку для разблокировки.</blockquote>\n\n` : ""}<blockquote>${active ? `✅ Подписка активна` : `❌ Подписка неактивна`}${active ? `\n📅 Истекает: <b>${exp}</b>\n⏳ Осталось: <b>${left} дн.</b>` : ""}</blockquote>

📊 Расход: <b>${formatGb(user.traffic_used_mb || 0)} GB / ∞</b>

🔗 <b>Ссылка подписки:</b>
<code>${url}</code>

Вставь её в HAPP, v2rayN, Hiddify, Streisand или Shadowrocket как <i>Subscription URL</i>.`;
}

function txtReferral(user) {
  const link = `https://t.me/${BOT_USERNAME}?start=ref_${user.telegram_id}`;
  return `<b>👥 Реферальная программа</b>

За каждого приглашённого друга:
• <b>+${REF_START_DAYS} дня</b> — когда он нажмёт /start по твоей ссылке
• <b>+${REF_PAY_DAYS} дня</b> — когда он оплатит подписку

Твоя реферальная ссылка:
<code>${link}</code>`;
}

const txtFAQ = `<b>❓ Часто задаваемые вопросы</b>

<blockquote expandable><b>Какие приложения поддерживаются?</b>
HAPP, v2rayN, Hiddify, Streisand, Shadowrocket, NekoBox и любые другие, поддерживающие Subscription URL.</blockquote>

<blockquote expandable><b>Как подключиться?</b>
Скопируй ссылку подписки из кабинета → открой приложение → добавь как Subscription URL → нажми обновить → выбери сервер.</blockquote>

<blockquote expandable><b>Сервер не работает, что делать?</b>
Нажми «Обновить подписку» в приложении — серверы обновляются каждый час. Если не помогло, напиши в поддержку.</blockquote>

<blockquote expandable><b>Сколько устройств можно подключить?</b>
Без ограничений — ссылка работает на любом количестве устройств.</blockquote>

<blockquote expandable><b>Как продлить подписку?</b>
Зайди в бота → Купить подписку. Срок прибавится к текущему, не сбросится.</blockquote>

<blockquote expandable><b>Как работает реферальная программа?</b>
Поделись своей реферальной ссылкой. Когда друг нажмёт Start — тебе +${REF_START_DAYS} дня. Когда оплатит — ещё +${REF_PAY_DAYS} дня.</blockquote>

<blockquote expandable><b>Что такое бонус за подписку на ${CHANNEL_BONUS}?</b>
Подпишись на канал — получишь +${BONUS_DAYS} дня к триалу. Плюс каждый месяц +${MONTHLY_BONUS} день пока подписан.</blockquote>`;

// ═══════════════════════════════════════════════════════════════════════════════
//  ОБРАБОТКА UPDATES
// ═══════════════════════════════════════════════════════════════════════════════

async function handleUpdate(upd) {
  await initDB();

  if (upd.pre_checkout_query) {
    await tg("answerPreCheckoutQuery",
      { pre_checkout_query_id: upd.pre_checkout_query.id, ok: true });
    return;
  }

  if (upd.message?.successful_payment) {
    await onSuccessfulPayment(upd.message);
    return;
  }

  if (upd.message) {
    await handleMessage(upd.message);
    return;
  }

  if (upd.callback_query) {
    await handleCallback(upd.callback_query);
  }
}

// ── Сообщения ─────────────────────────────────────────────────────────────────
async function handleMessage(msg) {
  const tid  = msg.from.id;
  const text = msg.text ?? "";

  await upsertUser(tid, msg.from.username, msg.from.first_name);
  const user = await getUser(tid);

  // /start
  if (text.startsWith("/start")) {
    const param = text.split(" ")[1] ?? "";
    await onStart(msg, user, param);
    return;
  }

  // /terms
  if (text.startsWith("/terms")) {
    await onTerms(msg);
    return;
  }

  // /banned — список забаненных (на случай если push-уведомление о бане потерялось)
  if (text.startsWith("/banned") && msg.from.id === ADMIN_ID) {
    await onBannedList(msg);
    return;
  }

  // /grant <telegram_id> <days> — вручную выдать дни подписки (поддержка + диагностика записи)
  if (text.startsWith("/grant") && msg.from.id === ADMIN_ID) {
    const parts = text.split(" ");
    const targetId = parseInt(parts[1]);
    const days = parseInt(parts[2]);
    if (!targetId || !days) {
      await send(msg.from.id, "Формат: /grant telegram_id дни\nНапример: /grant 6168325401 7");
      return;
    }
    const newExp = await extendSub(targetId, days);
    const check = await getUser(targetId);
    if (check?.subscription_expires === newExp) {
      await send(msg.from.id, `✅ Выдано ${days} дн. пользователю ${targetId}. До: ${newExp}`);
    } else {
      await send(msg.from.id,
        `❌ Запись не подтвердилась! Ожидали "${newExp}", в базе "${check?.subscription_expires}". Смотри логи (Turso error).`);
    }
    return;
  }

  // Состояния FSM
  if (user?.state === "support_waiting") {
    await onSupportMessage(msg, user);
    return;
  }

  if (user?.state === "promo_waiting") {
    await onPromoEntered(msg, user);
    return;
  }

  if (user?.state === "gift_waiting") {
    await onGiftEntered(msg, user);
    return;
  }

  // Админ: мастер создания промокода
  if (tid === ADMIN_ID && user?.state?.startsWith("promo_new_code:")) {
    await onAdminPromoCodeEntered(msg, user.state.slice("promo_new_code:".length));
    return;
  }
  if (tid === ADMIN_ID && user?.state?.startsWith("promo_new_value:")) {
    const [, type, code] = user.state.split(":");
    await onAdminPromoValueEntered(msg, type, code);
    return;
  }
  if (tid === ADMIN_ID && user?.state?.startsWith("promo_new_uses:")) {
    const [, type, code, value] = user.state.split(":");
    await onAdminPromoUsesEntered(msg, type, code, value);
    return;
  }

  // Админ: ручной бан
  if (tid === ADMIN_ID && user?.state === "admin_ban_id") {
    await onAdminBanIdEntered(msg);
    return;
  }

  // Админ: ручная выдача подписки
  if (tid === ADMIN_ID && user?.state === "admin_grant_id") {
    await onAdminGrantIdEntered(msg);
    return;
  }
  if (tid === ADMIN_ID && user?.state?.startsWith("admin_grant_days:")) {
    await onAdminGrantDaysEntered(msg, parseInt(user.state.slice("admin_grant_days:".length)));
    return;
  }
  if (tid === ADMIN_ID && user?.state?.startsWith("admin_pregrant_days:")) {
    await onAdminPregrantDaysEntered(msg, user.state.slice("admin_pregrant_days:".length));
    return;
  }

  // Админ: ответ на тикет поддержки
  if (tid === ADMIN_ID && msg.reply_to_message) {
    await onAdminReply(msg);
    return;
  }
}

// ── /start ────────────────────────────────────────────────────────────────────
// ИСПРАВЛЕНО: добавлен try/catch, убрана ошибка с ref_start_given_from
async function onStart(msg, user, param) {
  try {
    const tid  = msg.from.id;

    // Проверка подписки на основной канал
    const subscribed = await isMember(CHANNEL_MAIN, tid);
    if (!subscribed) {
      await send(tid,
        `👋 Привет, <b>${msg.from.first_name}</b>!\n\nДля использования бота необходимо подписаться на наш канал 👇`,
        { reply_markup: kb([
            [{ text: `📢 Подписаться на ${CHANNEL_MAIN}`, url: `https://t.me/${CHANNEL_MAIN.slice(1)}` }],
            [{ text: "✅ Я подписался", callback_data: "check_sub" }],
          ]) });
      return;
    }

    // Обработка реферального параметра (ИСПРАВЛЕНО)
    if (param.startsWith("ref_") && !user?.referred_by) {
      const refId = parseInt(param.slice(4));
      if (refId && refId !== tid) {
        await dbRun("UPDATE users SET referred_by = ? WHERE telegram_id = ?", [refId, tid]);
        // +3 дня рефереру (без лишней проверки)
        await extendSub(refId, REF_START_DAYS);
        await dbRun("UPDATE users SET ref_start_given = ref_start_given + 1 WHERE telegram_id = ?", [refId]);
        await send(refId,
          `🎉 По твоей реферальной ссылке присоединился новый пользователь!\n<b>+${REF_START_DAYS} дня</b> добавлено к твоей подписке.`);
      }
    }

    // Ежемесячный бонус за @nekrozxxxgod
    await checkMonthlyBonus(tid, user);

    // Отложенная выдача подписки (Playerok и т.п.) — если админ выдал её ещё до /start
    await applyPendingGrant(tid, msg.from.username);

    const freshUser = await getUser(tid);
    await send(tid, txtWelcome(msg.from.first_name, isActive(freshUser)),
      { reply_markup: kbMain(freshUser, tid === ADMIN_ID) });
  } catch (err) {
    console.error("onStart error:", err);
    await send(msg.from.id, "⚠️ Произошла ошибка. Попробуйте ещё раз или напишите в поддержку.",
      { reply_markup: kbMain(user, msg.from.id === ADMIN_ID) });
  }
}

// ── Callbacks ─────────────────────────────────────────────────────────────────
async function handleCallback(cb) {
  const tid  = cb.from.id;
  const data = cb.data;
  const mid  = cb.message.message_id;

  await upsertUser(tid, cb.from.username, cb.from.first_name);
  const user = await getUser(tid);

  await answerCb(cb.id);

  // Проверка подписки при каждом действии
  if (data !== "check_sub") {
    const ok = await isMember(CHANNEL_MAIN, tid);
    if (!ok) {
      await edit(tid, mid,
        `❌ Для использования бота необходимо подписаться на ${CHANNEL_MAIN}`,
        { reply_markup: kb([
            [{ text: `📢 Подписаться`, url: `https://t.me/${CHANNEL_MAIN.slice(1)}` }],
            [{ text: "✅ Я подписался", callback_data: "check_sub" }],
          ]) });
      return;
    }
  }

  if (data === "check_sub")          return onCheckSub(cb, user);
  if (data === "menu")               return onMenu(cb, user);
  if (data === "trial")              return onTrial(cb, user);
  if (data === "trial_with_bonus")   return onTrialWithBonus(cb, user);
  if (data === "trial_skip_bonus")   return onTrialActivate(cb, user, false);
  if (data === "cabinet")            return onCabinet(cb, user);
  if (data === "cabinet_qr")         return onCabinetQr(cb, user);
  if (data === "buy")                return onBuy(cb);
  if (data.startsWith("period_"))    return onPeriod(cb, user, parseInt(data.slice(7)));
  if (data.startsWith("pay_stars_")) return onPayStars(cb, user, data);
  if (data.startsWith("pay_rubles_")) return onPayRubles(cb, user, data);
  if (data === "referral")           return onReferral(cb, user);
  if (data === "faq")                return onFAQ(cb);
  if (data === "support")            return onSupport(cb);
  if (data === "support_write")      return onSupportWrite(cb, user);
  if (data === "support_email")      return onSupportEmail(cb);
  if (data === "promo")              return onPromo(cb, user);
  if (data === "gift")               return onGift(cb, user);
  if (data === "admin")              return onAdmin(cb, tid);
  if (data === "admin_stats")        return onAdminStats(cb);
  if (data === "admin_pending")      return onAdminPending(cb);
  if (data === "admin_promo")        return onAdminPromoList(cb);
  if (data === "admin_promo_new")    return onAdminPromoNewType(cb);
  if (data.startsWith("promo_type_")) return onAdminPromoType(cb, data);
  if (data.startsWith("promo_del_")) return onAdminPromoDelete(cb, data);
  if (data === "admin_banned")       return onAdminBannedList(cb);
  if (data === "admin_ban_start")    return onAdminBanStart(cb);
  if (data === "admin_grant_start")  return onAdminGrantStart(cb);
  if (data === "regen_token")        return onRegenToken(cb);
  if (data === "devices")            return onDevices(cb, user);
  if (data === "cats")               return onCategories(cb, user);
  if (data.startsWith("cat_toggle_")) return onCategoryToggle(cb, user, data);
  if (data.startsWith("dev_rm_"))    return onDeviceRemove(cb, data);
  if (data === "dev_buy")            return onDevBuyMenu(cb);
  if (data === "dev_pay_stars")      return onBuyDevice(cb);
  if (data === "dev_pay_rub")        return onBuyDeviceRubles(cb);
  if (data.startsWith("confirm_"))   return onAdminConfirm(cb, data);
  if (data.startsWith("unban_"))     return onAdminUnban(cb, data);
  if (data.startsWith("paid_"))      return onUserPaid(cb, user, data);
}

// ── check_sub ─────────────────────────────────────────────────────────────────
async function onCheckSub(cb, user) {
  const tid = cb.from.id;
  const ok  = await isMember(CHANNEL_MAIN, tid);
  if (!ok) {
    await answerCb(cb.id, "❌ Ты ещё не подписался на канал", true);
    return;
  }
  await applyPendingGrant(tid, cb.from.username);
  const freshUser = await getUser(tid);
  await edit(tid, cb.message.message_id,
    txtWelcome(cb.from.first_name, isActive(freshUser)),
    { reply_markup: kbMain(freshUser, tid === ADMIN_ID) });
}

// ── menu ──────────────────────────────────────────────────────────────────────
async function onMenu(cb, user) {
  const freshUser = await getUser(cb.from.id);
  await edit(cb.from.id, cb.message.message_id,
    txtWelcome(cb.from.first_name, isActive(freshUser)),
    { reply_markup: kbMain(freshUser, cb.from.id === ADMIN_ID) });
}

// ── trial ─────────────────────────────────────────────────────────────────────
async function onTrial(cb, user) {
  const tid = cb.from.id;
  if (user?.trial_used) {
    await answerCb(cb.id, "Пробный период уже был использован", true);
    return;
  }
  if (isActive(user)) {
    await answerCb(cb.id, "У тебя уже есть активная подписка", true);
    return;
  }

  // Предложение бонуса за личный канал
  const hasBonus = await isMember(CHANNEL_BONUS, tid);
  if (!hasBonus) {
    await edit(tid, cb.message.message_id,
      `🎁 <b>Пробный период — ${TRIAL_DAYS} дней</b>\n\n`
      + `Подпишись на <b>${CHANNEL_BONUS}</b> и получи <b>+${BONUS_DAYS} дня</b> бесплатно!\n`
      + `Итого: <b>${TRIAL_DAYS + BONUS_DAYS} дней</b> вместо ${TRIAL_DAYS}.`,
      { reply_markup: kb([
          [{ text: `📢 Подписаться на ${CHANNEL_BONUS}`, url: `https://t.me/${CHANNEL_BONUS.slice(1)}` }],
          [{ text: `✅ Подписан — активировать на ${TRIAL_DAYS + BONUS_DAYS} дней`, callback_data: "trial_with_bonus" }],
          [{ text: `▶️ Активировать без бонуса (${TRIAL_DAYS} дней)`, callback_data: "trial_skip_bonus" }],
          back("menu"),
        ]) });
    return;
  }

  await onTrialActivate(cb, user, true);
}

async function onTrialWithBonus(cb, user) {
  const hasBonus = await isMember(CHANNEL_BONUS, cb.from.id);
  if (!hasBonus) {
    await answerCb(cb.id, `❌ Ты не подписан на ${CHANNEL_BONUS}`, true);
    return;
  }
  await onTrialActivate(cb, user, true);
}

async function onTrialActivate(cb, user, withBonus) {
  const tid  = cb.from.id;
  const days = TRIAL_DAYS + (withBonus ? BONUS_DAYS : 0);
  await dbRun("UPDATE users SET trial_used = 1 WHERE telegram_id = ?", [tid]);
  await extendSub(tid, days);
  await logPayment(tid, "trial");
  const token = await getOrCreateToken(tid);
  const url   = subUrl(token);

  await edit(tid, cb.message.message_id,
    `✅ <b>Пробный период активирован на ${days} дней!</b>\n\n`
    + `🔗 Ссылка подписки:\n<code>${url}</code>\n\n`
    + `Вставь её в приложение (HAPP, v2rayN, Hiddify...) как <i>Subscription URL</i>.`,
    { reply_markup: kb([[{ text: "📋 В кабинет", callback_data: "cabinet" }], back("menu")]) });
}

// ── cabinet ───────────────────────────────────────────────────────────────────
function kbCabinet() {
  return kb([
    [{ text: "📷 Показать QR-код", callback_data: "cabinet_qr" }],
    [{ text: "🔄 Обновить токен", callback_data: "regen_token" }],
    [{ text: "📱 Устройства", callback_data: "devices" }],
    [{ text: "🌐 Настройка локаций", callback_data: "cats" }],
    [{ text: "👥 Реферальная ссылка", callback_data: "referral" }],
    back("menu"),
  ]);
}

async function onCabinet(cb, user) {
  const tid   = cb.from.id;
  const token = await getOrCreateToken(tid);
  // Редактируем существующее сообщение вместо отправки нового —
  // иначе каждый заход в кабинет плодит сообщения в чате.
  // Если предыдущее сообщение было фото (например, после показа QR),
  // editMessageText вернёт ok:false — тогда шлём новое сообщение.
  const r = await edit(tid, cb.message.message_id, txtCabinet(user, token),
    { reply_markup: kbCabinet() });
  if (!r.ok) {
    await send(tid, txtCabinet(user, token), { reply_markup: kbCabinet() });
  }
}

async function onCabinetQr(cb, user) {
  const tid   = cb.from.id;
  const token = await getOrCreateToken(tid);
  const url   = subUrl(token);
  const qr    = `https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=${encodeURIComponent(url)}`;
  await tg("sendPhoto", {
    chat_id: tid,
    photo:   qr,
    caption: "📷 QR-код твоей подписки",
    reply_markup: kb([back("cabinet")]),
  });
}

// ── devices ───────────────────────────────────────────────────────────────────
// ── категории (включить/выключить локации) ──────────────────────────────────────
async function onCategories(cb, user) {
  const enabled = parseEnabledCategories(user);
  const text = `🌐 <b>Настройка локаций</b>\n\n`
    + `Отключай категории, которые тебе не нужны — они пропадут из списка серверов в приложении.\n\n`
    + `Сейчас включено: <b>${enabled.size}/${CATEGORY_ORDER.length}</b>`;

  const rows = CATEGORY_ORDER.map(key => {
    const on = enabled.has(key);
    return [{ text: `${on ? "✅" : "❌"} ${CATEGORY_TITLES[key]}`, callback_data: `cat_toggle_${key}` }];
  });
  rows.push(back("cabinet"));

  const r = await edit(cb.from.id, cb.message.message_id, text, { reply_markup: kb(rows) });
  if (!r.ok) await send(cb.from.id, text, { reply_markup: kb(rows) });
}

async function onCategoryToggle(cb, user, data) {
  const key = data.slice("cat_toggle_".length);
  if (!CATEGORY_ORDER.includes(key)) return;
  const enabled = parseEnabledCategories(user);

  if (enabled.has(key)) {
    if (enabled.size === 1) {
      await answerCb(cb.id, "⚠️ Нельзя отключить последнюю категорию", true);
      return;
    }
    enabled.delete(key);
  } else {
    enabled.add(key);
  }

  const value = enabled.size === CATEGORY_ORDER.length ? null : CATEGORY_ORDER.filter(k => enabled.has(k)).join(",");
  await dbRun("UPDATE users SET enabled_categories = ? WHERE telegram_id = ?", [value, cb.from.id]);
  const freshUser = await getUser(cb.from.id);
  await onCategories(cb, freshUser);
}

async function onDevices(cb, user) {
  const tid    = cb.from.id;
  const limit  = DEVICE_LIMIT_FREE + (user.extra_device_slots || 0);
  const rows   = await db(
    "SELECT id, label, last_seen FROM devices WHERE telegram_id = ? AND confirmed = 1 ORDER BY last_seen DESC", [tid]);

  let text = `📱 <b>Устройства (${rows.length}/${limit})</b>\n\n`
    + `Бесплатно доступно: <b>${DEVICE_LIMIT_FREE}</b>`
    + (user.extra_device_slots ? ` + докуплено <b>${user.extra_device_slots}</b>\n\n` : `\n\n`);

  if (rows.length === 0) {
    text += `Пока ни одно устройство не подключалось.`;
  } else {
    text += rows.map((d, i) => {
      const seen = new Date(d.last_seen).toLocaleString("ru-RU",
        { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
      return `${i + 1}. ${d.label || "Устройство"} — активно ${seen}`;
    }).join("\n");
  }
  text += `\n\nЕсли устройство больше не используешь — удали его, освободится слот.`;

  const buttons = rows.map((d, i) => (
    [{ text: `🗑 Удалить «${d.label || "Устройство"} ${i + 1}»`, callback_data: `dev_rm_${d.id}` }]
  ));
  buttons.push([{ text: `➕ Докупить устройство`, callback_data: "dev_buy" }]);
  buttons.push(back("cabinet"));

  const r = await edit(tid, cb.message.message_id, text, { reply_markup: kb(buttons) });
  if (!r.ok) await send(tid, text, { reply_markup: kb(buttons) });
}

async function onDeviceRemove(cb, data) {
  const tid = cb.from.id;
  const id  = parseInt(data.slice(7));
  await dbRun("DELETE FROM devices WHERE id = ? AND telegram_id = ?", [id, tid]);
  await answerCb(cb.id, "✅ Устройство удалено", true);
  const user = await getUser(tid);
  await onDevices(cb, user);
}

async function onDevBuyMenu(cb) {
  await edit(cb.from.id, cb.message.message_id,
    `➕ <b>Докупить устройство</b>\n\n`
    + `Одно доп. устройство: <b>${DEVICE_PRICE_STARS} ⭐</b> или <b>${DEVICE_PRICE_RUB} ₽</b>`,
    { reply_markup: kb([
        [{ text: `⭐ Оплатить ${DEVICE_PRICE_STARS} Stars`, callback_data: "dev_pay_stars" }],
        [{ text: `💳 Оплатить ${DEVICE_PRICE_RUB} ₽`, callback_data: "dev_pay_rub" }],
        back("devices"),
      ]) });
}

async function onBuyDevice(cb) {
  await tg("sendInvoice", {
    chat_id:     cb.from.id,
    title:       "NekrozVPN — +1 устройство",
    description: `Дополнительный слот устройства для подписки · ${DEVICE_PRICE_STARS} Telegram Stars`,
    payload:     "device_stars",
    currency:    "XTR",
    prices:      [{ label: "+1 устройство", amount: DEVICE_PRICE_STARS }],
  });
}

async function onBuyDeviceRubles(cb) {
  const tid   = cb.from.id;
  const payId = await logPayment(tid, "device_rub", String(DEVICE_PRICE_RUB));
  await edit(tid, cb.message.message_id,
    `💳 <b>Докупка устройства — ${DEVICE_PRICE_RUB} ₽</b>\n\n`
    + `<blockquote>Переведи <b>${DEVICE_PRICE_RUB} ₽</b> на СБП или карту — реквизиты пришлёт администратор.</blockquote>\n\n`
    + `После оплаты нажми кнопку ниже.`,
    { reply_markup: kb([
        [{ text: "✅ Я оплатил", callback_data: `paid_${payId}` }],
        back("devices"),
      ]) });
  await send(ADMIN_ID,
    `💳 <b>Докупка устройства</b>\n`
    + `Пользователь: ${cb.from.first_name} (@${cb.from.username ?? "—"})\n`
    + `ID: <code>${tid}</code>\n`
    + `Сумма: ${DEVICE_PRICE_RUB} ₽\n`
    + `Payment ID: <code>${payId}</code>`,
    { reply_markup: kb([[{ text: "✅ Подтвердить", callback_data: `confirm_${payId}` }]]) });
}

// ── regen_token ───────────────────────────────────────────────────────────────
async function onRegenToken(cb) {
  const tid = cb.from.id;
  const token = await generateToken(tid);
  const user  = await getUser(tid);
  await answerCb(cb.id, "✅ Токен обновлён. Старая ссылка больше не работает.", true);
  const r = await edit(tid, cb.message.message_id, txtCabinet(user, token),
    { reply_markup: kbCabinet() });
  if (!r.ok) {
    await send(tid, txtCabinet(user, token), { reply_markup: kbCabinet() });
  }
}

// ── buy ───────────────────────────────────────────────────────────────────────
const STARS_TO_RUB = 1.4; // курс для конвертации фиксированной цены (₽) в Stars

function applyPromo(period, user) {
  const base = PERIODS[period];
  let stars = base.stars, rubles = base.rubles, note = "";
  if (user?.active_promo_type === "percent" && user.active_promo_value) {
    const pct = user.active_promo_value;
    stars = Math.max(1, Math.round(stars * (1 - pct / 100)));
    if (rubles) rubles = Math.max(1, Math.round(rubles * (1 - pct / 100)));
    note = `🎟 Промокод «${user.active_promo_code}»: скидка ${pct}%`;
  } else if (user?.active_promo_type === "fixed" && user.active_promo_value) {
    rubles = Math.round(user.active_promo_value);
    stars = Math.max(1, Math.round(rubles / STARS_TO_RUB));
    note = `🎟 Промокод «${user.active_promo_code}»: фиксированная цена`;
  }
  return { stars, rubles, note };
}

async function consumePromo(tid) {
  const user = await getUser(tid);
  if (user?.active_promo_code) {
    await dbRun("UPDATE promo_codes SET uses = uses + 1 WHERE code = ?", [user.active_promo_code]);
    await dbRun(
      "UPDATE users SET active_promo_code = NULL, active_promo_type = NULL, active_promo_value = NULL WHERE telegram_id = ?",
      [tid]);
  }
}

async function onBuy(cb) {
  await edit(cb.from.id, cb.message.message_id,
    `💳 <b>Выберите период подписки</b>\n\n`
    + `Цены указаны в Telegram Stars и рублях (если доступно).\n`
    + `При продлении срок прибавляется к текущему.`,
    { reply_markup: kbPeriods() });
}

// ── период ────────────────────────────────────────────────────────────────────
async function onPeriod(cb, user, period) {
  const base = PERIODS[period];
  if (!base) return;
  const { stars, rubles, note } = applyPromo(period, user);
  const discounted = stars !== base.stars || rubles !== base.rubles;

  let text = `📅 <b>${period} дней</b>\n\n`;
  if (discounted) {
    text += `⭐ <s>${base.stars}</s> → <b>${stars} Stars</b>\n`;
  } else {
    text += `⭐ <b>${stars} Stars</b> — оплата через Telegram\n`;
  }
  if (rubles) {
    if (discounted) {
      text += `💳 <s>${base.rubles}</s> → <b>${rubles} ₽</b>\n`;
    } else {
      text += `💳 <b>${rubles} ₽</b> — оплата на карту/СБП (скидка ${Math.round((1 - base.rubles / (base.stars * 1.82)) * 100)}%)\n`;
    }
  }
  if (note) text += `\n${note}`;
  text += `\n\nВыберите способ оплаты:`;
  await edit(cb.from.id, cb.message.message_id, text, { reply_markup: kbPayOptions(period) });
}

// ── pay_stars ─────────────────────────────────────────────────────────────────
async function onPayStars(cb, user, data) {
  const period = parseInt(data.split("_")[2]);
  const base = PERIODS[period];
  if (!base) return;
  const { stars } = applyPromo(period, user);
  await tg("sendInvoice", {
    chat_id:       cb.from.id,
    title:         `NekrozVPN — ${period} дней`,
    description:   `Подписка на VPN на ${period} дней · ${stars} Telegram Stars`,
    payload:       `sub_${period}d`,
    currency:      "XTR",
    prices:        [{ label: `${period} дней`, amount: stars }],
  });
}

// ИСПРАВЛЕНО: обработка payload для подписок и подарков с произвольным периодом
async function onSuccessfulPayment(msg) {
  const tid = msg.from.id;
  const payload = msg.successful_payment.payload;
  const chargeId = msg.successful_payment.telegram_payment_charge_id;
  const paidStars = msg.successful_payment.total_amount;

  if (payload.startsWith("sub_")) {
    const days = parseInt(payload.slice(4, -1)); // "sub_14d" → 14
    if (!days || !PERIODS[days]) return;
    await extendSub(tid, days);
    await logPayment(tid, "stars", String(paidStars), chargeId, days);
    await consumePromo(tid);
    await giveRefPayBonus(tid);
    const token = await getOrCreateToken(tid);
    await send(tid,
      `✅ <b>Оплата прошла! Подписка активирована на ${days} дней.</b>\n\n`
      + `🔗 Ссылка подписки:\n<code>${subUrl(token)}</code>`,
      { reply_markup: kb([[{ text: "📋 В кабинет", callback_data: "cabinet" }]]) });
    await send(ADMIN_ID,
      `⭐ <b>Оплата Stars (${days} дн., ${paidStars}⭐)</b>\n`
      + `Пользователь: ${msg.from.full_name ?? msg.from.first_name} (@${msg.from.username ?? "—"})\n`
      + `ID: <code>${tid}</code>`);
  } else if (payload.startsWith("gift_")) {
    const recipId = parseInt(payload.slice(5));
    if (recipId) {
      await extendSub(recipId, GIFT_PERIOD);
      await logPayment(recipId, "gift", String(PERIODS[GIFT_PERIOD].stars), chargeId, GIFT_PERIOD);
      const token = await getOrCreateToken(recipId);
      await send(recipId,
        `🎁 <b>Вам подарили подписку NekrozVPN на ${GIFT_PERIOD} дней!</b>\n\n`
        + `Ссылка подписки: <code>${subUrl(token)}</code>`);
      await send(tid,
        `✅ Подарок отправлен пользователю <code>${recipId}</code>.`);
    }
  } else if (payload === "device_stars") {
    await dbRun("UPDATE users SET extra_device_slots = COALESCE(extra_device_slots,0) + 1 WHERE telegram_id = ?", [tid]);
    await logPayment(tid, "device_stars", String(DEVICE_PRICE_STARS), chargeId);
    await send(tid, `✅ <b>Устройство добавлено!</b> Теперь доступно на 1 слот больше.`,
      { reply_markup: kb([[{ text: "📱 Устройства", callback_data: "devices" }]]) });
  }
}

// ── pay_rubles ────────────────────────────────────────────────────────────────
async function onPayRubles(cb, user, data) {
  const period = parseInt(data.split("_")[2]);
  const base = PERIODS[period];
  if (!base || !base.rubles) return;
  const { rubles } = applyPromo(period, user);
  const tid   = cb.from.id;
  const payId = await logPayment(tid, "rubles", String(rubles), null, period);

  await edit(tid, cb.message.message_id,
    `💳 <b>Оплата рублями — ${rubles} ₽ за ${period} дней</b>\n\n`
    + `<blockquote>Переведи <b>${rubles} ₽</b> на СБП или карту — реквизиты пришлёт администратор в течение нескольких минут.</blockquote>\n\n`
    + `После оплаты нажми кнопку ниже — подписка будет активирована в течение <b>5 часов</b>.`,
    { reply_markup: kb([
        [{ text: "✅ Я оплатил", callback_data: `paid_${payId}` }],
        back("buy"),
      ]) });

  await send(ADMIN_ID,
    `💳 <b>Новая оплата рублями (${period} дн.)</b>\n`
    + `Пользователь: ${cb.from.first_name} (@${cb.from.username ?? "—"})\n`
    + `ID: <code>${tid}</code>\n`
    + `Сумма: ${rubles} ₽\n`
    + `Payment ID: <code>${payId}</code>`,
    { reply_markup: kb([[{ text: "✅ Подтвердить", callback_data: `confirm_${payId}` }]]) });
}

async function onUserPaid(cb, user, data) {
  await answerCb(cb.id, "✅ Администратор уведомлён! Ожидай активации.", true);
}

async function onAdminConfirm(cb, data) {
  if (cb.from.id !== ADMIN_ID) return;
  const payId = parseInt(data.slice(8));
  const row = await db(
    "SELECT telegram_id, amount, type, days FROM payments WHERE id = ? AND status = 'pending' LIMIT 1",
    [payId]);
  if (!row[0]) { await answerCb(cb.id, "Платёж не найден или уже подтверждён", true); return; }
  const tid = row[0].telegram_id;

  await dbRun("UPDATE payments SET status='confirmed', confirmed_at=datetime('now') WHERE id = ?", [payId]);
  await answerCb(cb.id, "✅ Подтверждено");
  await tg("editMessageReplyMarkup",
    { chat_id: ADMIN_ID, message_id: cb.message.message_id, reply_markup: kb([]) });

  if (row[0].type === "device_rub") {
    await dbRun("UPDATE users SET extra_device_slots = COALESCE(extra_device_slots,0) + 1 WHERE telegram_id = ?", [tid]);
    await send(tid, `✅ <b>Оплата подтверждена! Устройство добавлено.</b>`,
      { reply_markup: kb([[{ text: "📱 Устройства", callback_data: "devices" }]]) });
    return;
  }

  // Дни хранятся в самом платеже — раньше угадывали по сумме, что ломалось при скидках
  const days = row[0].days || 30;
  await extendSub(tid, days);
  await consumePromo(tid);
  await giveRefPayBonus(tid);
  const token = await getOrCreateToken(tid);

  await send(tid,
    `✅ <b>Оплата подтверждена! Подписка активирована на ${days} дней.</b>\n\n`
    + `🔗 Ссылка подписки:\n<code>${subUrl(token)}</code>`,
    { reply_markup: kb([[{ text: "📋 В кабинет", callback_data: "cabinet" }]]) });
}

// ── referral ──────────────────────────────────────────────────────────────────
async function onReferral(cb, user) {
  await edit(cb.from.id, cb.message.message_id, txtReferral(user),
    { reply_markup: kb([back("menu")]) });
}

// ── FAQ ───────────────────────────────────────────────────────────────────────
async function onFAQ(cb) {
  await edit(cb.from.id, cb.message.message_id, txtFAQ,
    { reply_markup: kb([back("menu")]) });
}

// ── terms ─────────────────────────────────────────────────────────────────────
async function onTerms(msg) {
  const tid = msg.from.id;
  const text = `<b>📄 Условия использования NekrozVPN</b>
⚡️ Самые быстрые сервера, отобранные из десятков источников и проверенные вживую — никакого случайного мусора.

Нажми на каждый пункт, чтобы развернуть.

<b>1. Что такое сервис</b>
<blockquote expandable>NekrozVPN — агрегатор: сервис собирает конфигурации VPN-серверов, находящиеся в открытом доступе у сторонних поставщиков, и предоставляет к ним удобный доступ одной подпиской (ссылкой). NekrozVPN не является владельцем и оператором этих серверов, не размещает и не хранит на них какой-либо трафик или контент пользователей.</blockquote>

<b>2. Гарантии и ответственность</b>
<blockquote expandable>Сервис предоставляется "как есть". Поскольку используемые сервера принадлежат третьим лицам-поставщикам, NekrozVPN не может гарантировать бесперебойную работу, скорость, доступность или сохранность конкретного адреса — состав списка может меняться без предупреждения. NekrozVPN не несёт ответственности за действия сторонних поставщиков серверов и за то, как пользователь использует полученный доступ.</blockquote>

<b>3. Обработка данных</b>
<blockquote expandable>Хранятся: Telegram ID, username, статус и срок подписки, оценка активности (не реальный трафик — воркер физически не видит трафик через сторонние сервера), обезличенный отпечаток устройства (хэш от технических данных запроса, не содержит личных данных). Данные используются только для работы сервиса (выдача подписки, лимит устройств, поддержка) и не передаются и не продаются третьим лицам.</blockquote>

<b>4. Оплата и возврат</b>
<blockquote expandable>Тарифы 1 и 14 дней возврату не подлежат ни при каких условиях. Тарифы 30 и 50 дней можно вернуть только в течение 1 суток с момента оплаты — в этом случае возвращается 50% от уплаченной суммы. По истечении 1 суток с момента оплаты возврат по тарифам 30 и 50 дней невозможен. Цены и тарифы могут меняться, актуальные всегда доступны в боте.</blockquote>

<b>5. Устройства и блокировки</b>
<blockquote expandable>Бесплатно доступно ${DEVICE_LIMIT_FREE} устройства на подписку, дополнительные — платно (раздел «Устройства» в кабинете). При превышении лимита или признаках передачи ссылки третьим лицам доступ по подписке может быть временно ограничен до обращения в поддержку.</blockquote>

<b>6. Запрещённое использование</b>
<blockquote expandable>Запрещено использовать сервис для незаконной деятельности, атак, рассылки спама и любых действий, нарушающих закон страны пользователя. При выявлении такого использования доступ может быть заблокирован без возврата средств.</blockquote>

<b>7. Изменения условий</b>
<blockquote expandable>Условия могут обновляться. Актуальная версия всегда доступна по команде /terms. Продолжение использования сервиса после изменений означает согласие с новой версией.</blockquote>

По вопросам — раздел «💬 Поддержка» в главном меню.`;

  await send(tid, text, { reply_markup: kb([back("menu")]) });
}

async function onBannedList(msg) {
  await initDB();
  const rows = await db(
    "SELECT telegram_id, username, ban_reason FROM users WHERE banned = 1 ORDER BY telegram_id DESC LIMIT 30");
  if (!rows.length) {
    await send(msg.from.id, "✅ Забаненных нет.");
    return;
  }
  for (const r of rows) {
    await send(msg.from.id,
      `🚫 <code>${r.telegram_id}</code> ${r.username ? "@" + r.username : ""}\nПричина: ${r.ban_reason || "—"}`,
      { reply_markup: kb([[{ text: "✅ Разбанить", callback_data: `unban_${r.telegram_id}` }]]) });
  }
}

// ── support ───────────────────────────────────────────────────────────────────
async function onSupport(cb) {
  await edit(cb.from.id, cb.message.message_id,
    `<b>🆘 Поддержка</b>\n\nЕсть вопрос или проблема? Мы поможем!`,
    { reply_markup: kb([
        [{ text: "✍️ Написать в бот", callback_data: "support_write" }],
        [{ text: `📧 Написать на почту (${SUPPORT_EMAIL})`,
           url: `mailto:${SUPPORT_EMAIL}` }],
        back("menu"),
      ]) });
}

async function onSupportWrite(cb, user) {
  const tid = cb.from.id;
  await setState(tid, "support_waiting");
  await edit(tid, cb.message.message_id,
    `✍️ <b>Напиши своё сообщение</b>\n\nОпиши проблему или вопрос — один следующий ответ будет отправлен в поддержку.`,
    { reply_markup: kb([back("support")]) });
}

async function onSupportMessage(msg, user) {
  const tid = msg.from.id;
  await setState(tid, null);

  // Сохраняем тикет
  const r = await db(
    "INSERT INTO support_msgs (telegram_id, text) VALUES (?, ?) RETURNING id",
    [tid, msg.text]);
  const ticketId = r[0]?.id;

  // Пересылаем админу с кнопкой ответа
  const fwd = await send(ADMIN_ID,
    `🆘 <b>Обращение в поддержку #${ticketId}</b>\n`
    + `От: ${msg.from.first_name} (@${msg.from.username ?? "—"}) <code>${tid}</code>\n\n`
    + `<blockquote>${msg.text}</blockquote>\n\n`
    + `Ответь на это сообщение чтобы написать пользователю.`);

  if (fwd.result?.message_id)
    await dbRun("UPDATE support_msgs SET fwd_msg_id = ? WHERE id = ?",
      [fwd.result.message_id, ticketId]);

  await send(tid,
    `✅ Сообщение отправлено в поддержку! Ответим как можно скорее.`,
    { reply_markup: kb([[{ text: "← В главное меню", callback_data: "menu" }]]) });
}

async function onSupportEmail(cb) {
  await answerCb(cb.id, `📧 ${SUPPORT_EMAIL}`, true);
}

async function onAdminReply(msg) {
  const replyToId = msg.reply_to_message.message_id;
  const r = await db(
    "SELECT telegram_id FROM support_msgs WHERE fwd_msg_id = ? LIMIT 1",
    [replyToId]);
  if (!r[0]) return;
  await send(r[0].telegram_id,
    `<b>💬 Ответ поддержки:</b>\n\n<blockquote>${msg.text}</blockquote>`);
  await send(ADMIN_ID, "✅ Ответ отправлен пользователю.");
}

// ── promo ─────────────────────────────────────────────────────────────────────
async function onPromo(cb, user) {
  await setState(cb.from.id, "promo_waiting");
  await edit(cb.from.id, cb.message.message_id,
    `🎟 <b>Промокод</b>\n\nВведи промокод следующим сообщением:`,
    { reply_markup: kb([back("menu")]) });
}

async function onPromoEntered(msg, user) {
  const tid  = msg.from.id;
  const code = msg.text.trim().toUpperCase();
  await setState(tid, null);

  const r = await db(
    "SELECT * FROM promo_codes WHERE code = ? AND uses < max_uses LIMIT 1",
    [code]);
  if (!r[0]) {
    await send(tid, `❌ Промокод <code>${code}</code> недействителен или уже использован.`,
      { reply_markup: kb([[{ text: "← В главное меню", callback_data: "menu" }]]) });
    return;
  }
  const promo = r[0];
  const type  = promo.type || "days";

  if (type === "days") {
    await dbRun("UPDATE promo_codes SET uses = uses + 1 WHERE code = ?", [code]);
    await extendSub(tid, promo.days);
    await logPayment(tid, "promo", code, null, promo.days);
    await send(tid,
      `✅ Промокод активирован! <b>+${promo.days} дней</b> добавлено к подписке.`,
      { reply_markup: kb([[{ text: "📋 В кабинет", callback_data: "cabinet" }], back("menu")]) });
    return;
  }

  // percent / fixed — не расходуются сразу, применяются при следующей покупке
  await dbRun(
    "UPDATE users SET active_promo_code = ?, active_promo_type = ?, active_promo_value = ? WHERE telegram_id = ?",
    [code, type, promo.value, tid]);
  const desc = type === "percent" ? `скидка ${promo.value}%` : `фиксированная цена ${promo.value} ₽`;
  await send(tid,
    `✅ Промокод <code>${code}</code> применён: <b>${desc}</b>.\nОн автоматически учтётся при следующей покупке подписки.`,
    { reply_markup: kb([[{ text: "💳 Купить подписку", callback_data: "buy" }], back("menu")]) });
}

// ── gift ──────────────────────────────────────────────────────────────────────
async function onGift(cb, user) {
  if (!isActive(user)) {
    await answerCb(cb.id, "Для отправки подарка нужна активная подписка", true);
    return;
  }
  await setState(cb.from.id, "gift_waiting");
  await edit(cb.from.id, cb.message.message_id,
    `🎁 <b>Подарить подписку</b>\n\nОтправь @username или ID пользователя которому хочешь подарить 30 дней:`,
    { reply_markup: kb([back("menu")]) });
}

async function onGiftEntered(msg, user) {
  const tid   = msg.from.id;
  const input = msg.text.trim().replace("@","");
  await setState(tid, null);

  const r = await db(
    "SELECT telegram_id FROM users WHERE username = ? OR telegram_id = ? LIMIT 1",
    [input, parseInt(input) || 0]);

  if (!r[0]) {
    await send(tid, `❌ Пользователь не найден. Убедись что он уже запускал бота.`,
      { reply_markup: kb([[{ text: "← В главное меню", callback_data: "menu" }]]) });
    return;
  }

  const recipId = r[0].telegram_id;
  if (recipId === tid) {
    await send(tid, `❌ Нельзя подарить подписку самому себе 😄`); return;
  }

  await tg("sendInvoice", {
    chat_id:     tid,
    title:       `Подарить NekrozVPN — ${GIFT_PERIOD} дней`,
    description: `Подписка на VPN на ${GIFT_PERIOD} дней в подарок для пользователя ${input}`,
    payload:     `gift_${recipId}`,
    currency:    "XTR",
    prices:      [{ label: `Подарок ${GIFT_PERIOD} дней`, amount: PERIODS[GIFT_PERIOD].stars }],
  });
}

// ── admin ─────────────────────────────────────────────────────────────────────
async function onAdmin(cb, tid) {
  if (tid !== ADMIN_ID) return;
  await edit(tid, cb.message.message_id,
    `<b>⚙️ Панель администратора</b>`,
    { reply_markup: kb([
        [{ text: "📊 Статистика", callback_data: "admin_stats" }],
        [{ text: "💳 Ожидают подтверждения", callback_data: "admin_pending" }],
        [{ text: "🎟 Промокоды", callback_data: "admin_promo" }],
        [{ text: "🚫 Забаненные", callback_data: "admin_banned" }],
        [{ text: "🔨 Забанить пользователя", callback_data: "admin_ban_start" }],
        [{ text: "🎁 Выдать подписку", callback_data: "admin_grant_start" }],
        back("menu"),
      ]) });
}

async function onAdminStats(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  const [total]   = await db("SELECT COUNT(*) as n FROM users");
  const [active]  = await db("SELECT COUNT(*) as n FROM users WHERE subscription_expires > datetime('now')");
  const [trials]  = await db("SELECT COUNT(*) as n FROM users WHERE trial_used = 1");
  const [paid]    = await db("SELECT COUNT(*) as n FROM payments WHERE status = 'confirmed'");
  const [pending] = await db("SELECT COUNT(*) as n FROM payments WHERE type='rubles' AND status='pending'");

  await edit(ADMIN_ID, cb.message.message_id,
    `<b>📊 Статистика NekrozVPN</b>\n\n`
    + `<blockquote>👤 Всего пользователей: <b>${total.n}</b>\n`
    + `✅ Активных подписок: <b>${active.n}</b>\n`
    + `🎁 Использовали триал: <b>${trials.n}</b>\n`
    + `💰 Подтверждённых оплат: <b>${paid.n}</b>\n`
    + `⏳ Ожидают подтверждения: <b>${pending.n}</b></blockquote>`,
    { reply_markup: kb([[{ text: "🔄 Обновить", callback_data: "admin_stats" }], back("admin")]) });
}

async function onAdminPending(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  const rows = await pendingRubles();
  if (!rows.length) {
    await edit(ADMIN_ID, cb.message.message_id, "✅ Нет ожидающих оплат",
      { reply_markup: kb([back("admin")]) });
    return;
  }
  const lines = rows.map(r =>
    `• <b>#${r.id}</b> — ${r.full_name ?? "—"} (@${r.username ?? "—"}) <code>${r.telegram_id}</code>\n  ${r.created_at}`
  ).join("\n\n");
  const buttons = rows.map(r =>
    [{ text: `✅ Подтвердить #${r.id}`, callback_data: `confirm_${r.id}` }]
  );
  buttons.push(back("admin"));
  await edit(ADMIN_ID, cb.message.message_id,
    `<b>💳 Ожидают подтверждения:</b>\n\n${lines}`,
    { reply_markup: kb(buttons) });
}

// ── Реферальный бонус при оплате ──────────────────────────────────────────────
async function giveRefPayBonus(tid) {
  const user = await getUser(tid);
  if (!user?.referred_by || user.ref_pay_given) return;
  await extendSub(user.referred_by, REF_PAY_DAYS);
  await dbRun("UPDATE users SET ref_pay_given = 1 WHERE telegram_id = ?", [tid]);
  await send(user.referred_by,
    `🎉 Твой реферал оплатил подписку! <b>+${REF_PAY_DAYS} дня</b> добавлено к твоей подписке.`);
}

// ── Ежемесячный бонус ─────────────────────────────────────────────────────────
async function checkMonthlyBonus(tid, user) {
  if (!user) return;
  const month = new Date().toISOString().slice(0, 7);
  if (user.bonus_month === month) return;
  const hasBonus = await isMember(CHANNEL_BONUS, tid);
  if (!hasBonus) return;
  await extendSub(tid, MONTHLY_BONUS);
  await dbRun("UPDATE users SET bonus_month = ? WHERE telegram_id = ?", [month, tid]);
  await send(tid,
    `🎁 <b>Ежемесячный бонус!</b> +${MONTHLY_BONUS} день за подписку на ${CHANNEL_BONUS} добавлен.`);
}

// ── Cron: напоминания ─────────────────────────────────────────────────────────
async function handleCron() {
  await initDB();
  const soon = new Date(Date.now() + 3 * 86400000).toISOString();
  const rows = await db(
    `SELECT telegram_id FROM users
     WHERE subscription_expires BETWEEN datetime('now') AND ?
       AND reminder_sent = 0`,
    [soon]);

  for (const { telegram_id } of rows) {
    const user = await getUser(telegram_id);
    const left = daysLeft(user);
    await send(telegram_id,
      `⚠️ <b>Подписка заканчивается через ${left} дн.</b>\n\nПродли чтобы не потерять доступ!`,
      { reply_markup: kb([[{ text: "💳 Продлить", callback_data: "buy" }]]) });
    await dbRun("UPDATE users SET reminder_sent = 1 WHERE telegram_id = ?", [telegram_id]);
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
//  SUBSCRIPTION HANDLER
// ═══════════════════════════════════════════════════════════════════════════════

function apply_label_js(raw, label) {
  const base = raw.includes("#") ? raw.slice(0, raw.indexOf("#")) : raw;
  return `${base}#${encodeURIComponent(label)}`;
}

async function fingerprint(request) {
  // Отпечаток "устройства". Раньше был UA + подсеть IP — оказалось, что это
  // слишком хрупко: смена сети или странности конкретного клиента (например,
  // HAPP иногда делает служебные запросы с другим/пустым UA) давали ложные
  // "новые устройства". Теперь основа — User-Agent (он у большинства VPN-клиентов
  // стабилен и специфичен для установки), IP используется только как fallback,
  // если UA вообще пустой, и то с широкой подсетью (/16), а не /24.
  const ua = request.headers.get("User-Agent") || "";
  let key = ua;
  if (!ua || ua.length < 4) {
    const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
    const ipNet = ip.split(".").slice(0, 2).join(".");
    key = `empty-ua|${ipNet}`;
  }
  const data = new TextEncoder().encode(key);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

function deviceLabel(request) {
  const ua = (request.headers.get("User-Agent") || "").toLowerCase();
  if (ua.includes("happ"))      return "Happ";
  if (ua.includes("hiddify"))   return "Hiddify";
  if (ua.includes("nekobox") || ua.includes("neko")) return "NekoBox";
  if (ua.includes("shadowrocket")) return "Shadowrocket";
  if (ua.includes("v2rayng"))   return "v2rayNG";
  if (ua.includes("streisand")) return "Streisand";
  if (ua.includes("iphone") || ua.includes("ios")) return "iPhone";
  if (ua.includes("android"))   return "Android";
  return "Устройство";
}

/**
 * Если устройство уже известно и подтверждено — обновляет last_seen, разрешает.
 * Если лимит исчерпан и это НОВЫЙ отпечаток — не банит сразу (мог быть разовый
 * глюк клиента, см. фингерпринт), а заносит как "неподтверждённый" и просто
 * отказывает в этом конкретном запросе. Только если тот же отпечаток вернётся
 * ЕЩЁ РАЗ — это уже реально второе устройство, и тогда банит всю подписку.
 */
async function checkDeviceLimit(tid, request, extraSlots) {
  const fp = await fingerprint(request);
  const known = await db(
    "SELECT id, confirmed FROM devices WHERE telegram_id = ? AND fingerprint = ? LIMIT 1", [tid, fp]);

  if (known[0]) {
    await dbRun("UPDATE devices SET last_seen = datetime('now') WHERE id = ?", [known[0].id]);
    if (known[0].confirmed) return true;

    // Второе появление того же "неподтверждённого" отпечатка — теперь считаем его реальным
    const countRow = await db(
      "SELECT COUNT(*) AS c FROM devices WHERE telegram_id = ? AND confirmed = 1", [tid]);
    const limit = DEVICE_LIMIT_FREE + (extraSlots || 0);
    if ((countRow[0]?.c ?? 0) >= limit) {
      await banUser(tid, `Превышен лимит устройств (${limit})`);
      return false;
    }
    await dbRun("UPDATE devices SET confirmed = 1 WHERE id = ?", [known[0].id]);
    return true;
  }

  const countRow = await db(
    "SELECT COUNT(*) AS c FROM devices WHERE telegram_id = ? AND confirmed = 1", [tid]);
  const limit = DEVICE_LIMIT_FREE + (extraSlots || 0);
  const confirmedNow = (countRow[0]?.c ?? 0) < limit;
  await dbRun(
    "INSERT INTO devices (telegram_id, fingerprint, label, confirmed) VALUES (?, ?, ?, ?)",
    [tid, fp, deviceLabel(request), confirmedNow ? 1 : 0]);
  return confirmedNow; // если слот занят — просто отказ в этом запросе, без бана
}

async function banUser(tid, reason) {
  await dbRun("UPDATE users SET banned = 1, ban_reason = ? WHERE telegram_id = ?", [reason, tid]);
  await send(ADMIN_ID,
    `🚫 <b>Подписка заблокирована автоматически</b>\n`
    + `Пользователь: <code>${tid}</code>\n`
    + `Причина: ${reason}`,
    { reply_markup: kb([[{ text: "✅ Разбанить", callback_data: `unban_${tid}` }]]) });
}

async function onAdminUnban(cb, data) {
  if (cb.from.id !== ADMIN_ID) return;
  const tid = parseInt(data.slice(6));
  await dbRun("UPDATE users SET banned = 0, ban_reason = NULL WHERE telegram_id = ?", [tid]);
  await dbRun("DELETE FROM devices WHERE telegram_id = ?", [tid]); // сброс списка устройств
  await answerCb(cb.id, "✅ Разбанен, устройства сброшены");
  await tg("editMessageReplyMarkup",
    { chat_id: ADMIN_ID, message_id: cb.message.message_id, reply_markup: kb([]) });
  await send(tid, `✅ <b>Подписка разблокирована.</b> Список устройств сброшен — первые ${DEVICE_LIMIT_FREE} новых подключений снова будут бесплатными.`);
}

// ── промокоды: список + создание + удаление (админ) ─────────────────────────────
async function onAdminPromoList(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  const rows = await db("SELECT * FROM promo_codes ORDER BY id DESC LIMIT 30");
  let text = `<b>🎟 Промокоды</b>\n\n`;
  if (!rows.length) {
    text += `Пока нет ни одного промокода.`;
  } else {
    text += rows.map(p => {
      const desc = p.type === "percent" ? `-${p.value}%`
        : p.type === "fixed" ? `фикс. ${p.value}₽`
        : `+${p.days} дн.`;
      return `<code>${p.code}</code> — ${desc} (${p.uses}/${p.max_uses})`;
    }).join("\n");
  }
  const rowsBtns = rows.map(p => [{ text: `🗑 ${p.code}`, callback_data: `promo_del_${p.id}` }]);
  rowsBtns.push([{ text: "➕ Создать промокод", callback_data: "admin_promo_new" }]);
  rowsBtns.push(back("admin"));
  const r = await edit(ADMIN_ID, cb.message.message_id, text, { reply_markup: kb(rowsBtns) });
  if (!r.ok) await send(ADMIN_ID, text, { reply_markup: kb(rowsBtns) });
}

async function onAdminPromoNewType(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  await edit(ADMIN_ID, cb.message.message_id,
    `<b>➕ Новый промокод</b>\n\nВыбери тип:`,
    { reply_markup: kb([
        [{ text: "％ Процентная скидка", callback_data: "promo_type_percent" }],
        [{ text: "💰 Фиксированная цена", callback_data: "promo_type_fixed" }],
        [{ text: "📅 Бонусные дни", callback_data: "promo_type_days" }],
        back("admin_promo"),
      ]) });
}

async function onAdminPromoType(cb, data) {
  if (cb.from.id !== ADMIN_ID) return;
  const type = data.slice("promo_type_".length); // percent | fixed | days
  await setState(ADMIN_ID, `promo_new_code:${type}`);
  await edit(ADMIN_ID, cb.message.message_id,
    `Введи текст промокода (например <code>SUMMER2026</code>):`,
    { reply_markup: kb([back("admin_promo")]) });
}

async function onAdminPromoDelete(cb, data) {
  if (cb.from.id !== ADMIN_ID) return;
  const id = parseInt(data.slice("promo_del_".length));
  await dbRun("DELETE FROM promo_codes WHERE id = ?", [id]);
  await answerCb(cb.id, "🗑 Удалено");
  await onAdminPromoList(cb);
}

// Шаги мастера создания промокода (текстовые сообщения администратора)
async function onAdminPromoCodeEntered(msg, type) {
  const code = msg.text.trim().toUpperCase().replace(/\s+/g, "");
  if (!code) { await send(ADMIN_ID, "Пустой код, попробуй ещё раз."); return; }
  const exists = await db("SELECT id FROM promo_codes WHERE code = ?", [code]);
  if (exists[0]) {
    await send(ADMIN_ID, `❌ Промокод <code>${code}</code> уже существует. Введи другой:`);
    return;
  }
  await setState(ADMIN_ID, `promo_new_value:${type}:${code}`);
  const prompt = type === "percent" ? "Введи процент скидки (например 20):"
    : type === "fixed" ? "Введи фиксированную цену в рублях (например 99):"
    : "Введи количество бонусных дней (например 7):";
  await send(ADMIN_ID, prompt);
}

async function onAdminPromoValueEntered(msg, type, code) {
  const value = parseFloat(msg.text.trim().replace(",", "."));
  if (!value || value <= 0) { await send(ADMIN_ID, "Нужно положительное число, попробуй ещё раз:"); return; }
  await setState(ADMIN_ID, `promo_new_uses:${type}:${code}:${value}`);
  await send(ADMIN_ID, "Сколько раз можно использовать промокод? (0 = без ограничений)");
}

async function onAdminPromoUsesEntered(msg, type, code, value) {
  let maxUses = parseInt(msg.text.trim());
  if (isNaN(maxUses) || maxUses < 0) { await send(ADMIN_ID, "Нужно целое число ≥ 0, попробуй ещё раз:"); return; }
  if (maxUses === 0) maxUses = 999999;

  const days = type === "days" ? Math.round(value) : 0;
  await dbRun(
    "INSERT INTO promo_codes (code, type, value, days, max_uses) VALUES (?, ?, ?, ?, ?)",
    [code, type, value, days, maxUses]);
  await setState(ADMIN_ID, null);

  const desc = type === "percent" ? `скидка ${value}%` : type === "fixed" ? `фикс. цена ${value}₽` : `+${days} дней`;
  await send(ADMIN_ID,
    `✅ Промокод <code>${code}</code> создан: ${desc}, лимит использований: ${maxUses === 999999 ? "∞" : maxUses}.`,
    { reply_markup: kb([[{ text: "🎟 К промокодам", callback_data: "admin_promo" }]]) });
}

// ── забаненные (кнопка вместо /banned) ───────────────────────────────────────────
async function onAdminBannedList(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  const rows = await db(
    "SELECT telegram_id, username, ban_reason FROM users WHERE banned = 1 ORDER BY telegram_id DESC LIMIT 30");
  let text = `<b>🚫 Забаненные</b>\n\n`;
  text += rows.length ? rows.map(r =>
    `<code>${r.telegram_id}</code> ${r.username ? "@" + r.username : ""} — ${r.ban_reason || "—"}`
  ).join("\n") : "Забаненных нет.";
  const rowsBtns = rows.map(r => [{ text: `✅ Разбанить ${r.telegram_id}`, callback_data: `unban_${r.telegram_id}` }]);
  rowsBtns.push(back("admin"));
  const r = await edit(ADMIN_ID, cb.message.message_id, text, { reply_markup: kb(rowsBtns) });
  if (!r.ok) await send(ADMIN_ID, text, { reply_markup: kb(rowsBtns) });
}

// ── ручной бан (кнопка) ──────────────────────────────────────────────────────────
async function onAdminBanStart(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  await setState(ADMIN_ID, "admin_ban_id");
  await edit(ADMIN_ID, cb.message.message_id,
    `<b>🔨 Забанить пользователя</b>\n\nВведи username (без @) или Telegram ID:`,
    { reply_markup: kb([back("admin")]) });
}

async function resolveUserByUsernameOrId(input) {
  const clean = input.trim().replace(/^@/, "");
  if (/^\d+$/.test(clean)) return getUser(parseInt(clean));
  const rows = await db("SELECT * FROM users WHERE username = ? COLLATE NOCASE LIMIT 1", [clean]);
  return rows[0] || null;
}

async function onAdminBanIdEntered(msg) {
  await setState(ADMIN_ID, null);
  const target = await resolveUserByUsernameOrId(msg.text);
  if (!target) {
    await send(ADMIN_ID, "❌ Пользователь не найден (он должен хотя бы раз запустить бота).");
    return;
  }
  await dbRun("UPDATE users SET banned = 1, ban_reason = ? WHERE telegram_id = ?",
    ["Забанен вручную администратором", target.telegram_id]);
  await send(ADMIN_ID, `🚫 Пользователь <code>${target.telegram_id}</code> забанен.`,
    { reply_markup: kb([[{ text: "✅ Разбанить", callback_data: `unban_${target.telegram_id}` }]]) });
  await send(target.telegram_id, `🚫 <b>Твоя подписка заблокирована администратором.</b>\nПиши в поддержку для разблокировки.`);
}

// ── ручная выдача подписки (кнопка вместо /grant) ────────────────────────────────
async function onAdminGrantStart(cb) {
  if (cb.from.id !== ADMIN_ID) return;
  await setState(ADMIN_ID, "admin_grant_id");
  await edit(ADMIN_ID, cb.message.message_id,
    `<b>🎁 Выдать подписку</b>\n\nВведи username (без @) или Telegram ID. `
    + `Если пользователь ещё ни разу не запускал бота — подписка выдастся автоматически, `
    + `как только он нажмёт /start и подпишется на канал (удобно для продаж на Playerok).`,
    { reply_markup: kb([back("admin")]) });
}

async function onAdminGrantIdEntered(msg) {
  const input = msg.text.trim();
  const target = await resolveUserByUsernameOrId(input);
  if (target) {
    await setState(ADMIN_ID, `admin_grant_days:${target.telegram_id}`);
    await send(ADMIN_ID, `Найден пользователь <code>${target.telegram_id}</code>. На сколько дней выдать подписку?`);
  } else {
    const username = input.replace(/^@/, "");
    if (/^\d+$/.test(username)) {
      await send(ADMIN_ID, "❌ Пользователь с таким ID не найден и не запускал бота.");
      await setState(ADMIN_ID, null);
      return;
    }
    await setState(ADMIN_ID, `admin_pregrant_days:${username}`);
    await send(ADMIN_ID,
      `Пользователь @${username} ещё не запускал бота. Подписка будет выдана автоматически при первом /start. На сколько дней?`);
  }
}

async function onAdminGrantDaysEntered(msg, tid) {
  const days = parseInt(msg.text.trim());
  if (!days || days <= 0) { await send(ADMIN_ID, "Нужно целое число дней > 0, попробуй ещё раз:"); return; }
  await setState(ADMIN_ID, null);
  const newExp = await extendSub(tid, days);
  await logPayment(tid, "admin_grant", null, null, days);
  await send(ADMIN_ID, `✅ Выдано ${days} дн. пользователю <code>${tid}</code>. До: ${newExp}`);
  const token = await getOrCreateToken(tid);
  await send(tid,
    `🎁 <b>Тебе выдана подписка администратором на ${days} дней!</b>\n\n`
    + `🔗 Ссылка подписки:\n<code>${subUrl(token)}</code>`,
    { reply_markup: kb([[{ text: "📋 В кабинет", callback_data: "cabinet" }]]) });
}

async function onAdminPregrantDaysEntered(msg, username) {
  const days = parseInt(msg.text.trim());
  if (!days || days <= 0) { await send(ADMIN_ID, "Нужно целое число дней > 0, попробуй ещё раз:"); return; }
  await setState(ADMIN_ID, null);
  await dbRun(
    "INSERT INTO pending_grants (username, days) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET days = excluded.days",
    [username, days]);
  await send(ADMIN_ID, `✅ Отложенная выдача создана: @${username} получит ${days} дн. при первом /start.`);
}

// Применяется в onStart/onCheckSub сразу после подтверждения подписки на канал
async function applyPendingGrant(tid, username) {
  if (!username) return;
  const rows = await db("SELECT days FROM pending_grants WHERE username = ? COLLATE NOCASE LIMIT 1", [username]);
  if (!rows[0]) return;
  const days = rows[0].days;
  await dbRun("DELETE FROM pending_grants WHERE username = ? COLLATE NOCASE", [username]);
  const newExp = await extendSub(tid, days);
  await logPayment(tid, "admin_grant", null, null, days);
  const token = await getOrCreateToken(tid);
  await send(tid,
    `🎁 <b>Тебе выдана подписка администратором на ${days} дней!</b>\n\n`
    + `🔗 Ссылка подписки:\n<code>${subUrl(token)}</code>`,
    { reply_markup: kb([[{ text: "📋 В кабинет", callback_data: "cabinet" }]]) });
  await send(ADMIN_ID, `✅ Отложенная выдача применена: @${username} → <code>${tid}</code>, ${days} дн. До: ${newExp}`);
}

async function trackTraffic(tid) {
  // Реальный трафик клиента воркер не видит (он идёт через сторонние VPN-серверы,
  // а не через Cloudflare) — поэтому это оценка активности: при каждом обновлении
  // подписки прибавляем условный расход, чтобы счётчик в клиенте не стоял на месте.
  const incMb = 40 + Math.floor(Math.random() * 210); // 40–250 МБ за обновление
  const r = await db(
    "UPDATE users SET traffic_used_mb = COALESCE(traffic_used_mb,0) + ? WHERE telegram_id = ? RETURNING traffic_used_mb",
    [incMb, tid]);
  return r[0]?.traffic_used_mb ?? incMb;
}

function formatGb(mb) {
  const gb = mb / 1024;
  return gb.toFixed(1).replace(".", ","); // "2,6" как в клиенте
}

function singboxNotice(label) {
  return JSON.stringify({
    outbounds: [{ type: "block", tag: label }],
    route: { final: label },
  });
}

function xrayNotice(label) {
  return JSON.stringify([{
    dns: { servers: ["1.1.1.1", "8.8.8.8"], queryStrategy: "UseIP" },
    inbounds: [
      { tag: "socks", port: 10808, listen: "127.0.0.1", protocol: "socks", settings: { udp: true, auth: "noauth" } },
    ],
    outbounds: [
      { tag: "proxy", protocol: "blackhole" },
      { tag: "direct", protocol: "freedom" },
    ],
    remarks: label,
  }]);
}

async function handleSubscription(url, request) {
  const token = url.searchParams.get("token");
  if (!token) return txt("# Укажи ?token=...\n", 400);

  const isHapp    = isHappClient(request);
  const isSingbox = isSingboxCoreClient(request);
  const format    = isHapp ? "xray" : isSingbox ? "singbox" : "plain";

  const HDRS = {
    "Content-Type":              format === "plain" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8",
    "Content-Disposition":       'attachment; filename="nekrozvpn"',
    "Cache-Control":             "no-store",
    "Access-Control-Allow-Origin": "*",
    "profile-title":             btoa(unescape(encodeURIComponent(PROFILE_NAME))),
    "subscription-userinfo":     "upload=0; download=0; total=0; expire=0",
    "profile-update-interval":   "1",
    "support-url":               `https://t.me/${BOT_USERNAME}`,
  };

  if (token === TEST_TOKEN) {
    const content = format === "xray" ? await getXrayContent()
      : format === "singbox" ? await getSingboxContent() : await getSubContent();
    return content
      ? new Response(content, { headers: HDRS })
      : txt("# Конфиги временно недоступны\n", 503);
  }

  await initDB();
  const user = await dbQ(
    "SELECT telegram_id, subscription_expires, extra_device_slots, banned, ban_reason, enabled_categories FROM users WHERE subscription_token = ? LIMIT 1",
    [token]);
  if (!user[0]) return txt("# Токен не найден\n# @NekrozVPNbot\n", 403);

  const enabledCats = parseEnabledCategories(user[0]);
  const getContent = () =>
    format === "xray" ? getFilteredXrayContent(enabledCats)
      : format === "singbox" ? getFilteredSingboxContent(enabledCats)
      : getFilteredSubContent(enabledCats);

  if (user[0].banned) {
    const notice = format === "xray" ? xrayNotice("⛔ Подписка заблокирована")
      : format === "singbox" ? singboxNotice("⛔ Подписка заблокирована")
      : apply_label_js(
          "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?encryption=none&type=tcp&security=none",
          `⛔ Подписка заблокирована — @${BOT_USERNAME}`) + "\n";
    return new Response(notice, { headers: HDRS });
  }

  const exp = new Date(user[0].subscription_expires);
  if (!user[0].subscription_expires || exp < new Date())
    return txt("# Подписка истекла\n# Продли: @NekrozVPNbot\n", 403);

  const tid = user[0].telegram_id;
  const deviceOk = await checkDeviceLimit(tid, request, user[0].extra_device_slots);
  if (!deviceOk) {
    const notice = format === "xray" ? xrayNotice("⛔ Заблокировано: лимит устройств")
      : format === "singbox" ? singboxNotice("⛔ Заблокировано: лимит устройств")
      : apply_label_js(
          "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?encryption=none&type=tcp&security=none",
          `⛔ Подписка заблокирована (лимит устройств) — @${BOT_USERNAME}`) + "\n";
    return new Response(notice, { headers: HDRS });
  }

  const content = await getContent();
  if (!content) return txt("# Конфиги временно недоступны\n", 503);

  const usedMb = await trackTraffic(tid);
  // total=0 → клиент (Happ и большинство v2ray-клиентов) рисует "N Gb / ∞"
  HDRS["subscription-userinfo"] =
    `upload=0; download=${usedMb * 1024 * 1024}; total=0; expire=${Math.floor(exp.getTime()/1000)}`;

  return new Response(content, { headers: HDRS });
}

async function dbQ(sql, args = []) { return db(sql, args); }

async function getSubContent() {
  try {
    const r = await db("SELECT value FROM settings WHERE key='subscription' LIMIT 1");
    return r[0]?.value ?? null;
  } catch { return null; }
}

async function getSingboxContent() {
  try {
    const r = await db("SELECT value FROM settings WHERE key='subscription_singbox' LIMIT 1");
    return r[0]?.value ?? null;
  } catch { return null; }
}

async function getXrayContent() {
  try {
    const r = await db("SELECT value FROM settings WHERE key='subscription_xray' LIMIT 1");
    return r[0]?.value ?? null;
  } catch { return null; }
}

// ── Категории и фильтрация по пользовательским настройкам ──────────────────────
// ВАЖНО: ключи и заголовки должны БУКВА В БУКВУ совпадать с CATEGORY_ORDER/
// CATEGORY_TITLES в main.py — иначе фильтрация по sing-box selector'ам не совпадёт.
const CATEGORY_ORDER = ["auto", "lte", "whitelist"];
const CATEGORY_TITLES = {
  auto: "🌐 Авто серверы",
  lte: "🏎️ LTE локации",
  whitelist: "🏳 Белые списки",
};

function parseEnabledCategories(user) {
  if (!user?.enabled_categories) return new Set(CATEGORY_ORDER); // по умолчанию — всё включено
  const arr = user.enabled_categories.split(",").map(s => s.trim()).filter(Boolean);
  return arr.length ? new Set(arr) : new Set(CATEGORY_ORDER);
}

function allEnabled(enabled) {
  return CATEGORY_ORDER.every(k => enabled.has(k));
}

async function getFilteredSubContent(enabled) {
  if (allEnabled(enabled)) return getSubContent();
  try {
    const r = await db("SELECT value FROM settings WHERE key='subscription_data' LIMIT 1");
    if (!r[0]) return null;
    const data = JSON.parse(r[0].value);
    const lines = [];
    for (const cat of data.categories) {
      if (enabled.has(cat.key)) lines.push(...cat.items);
    }
    return lines.join("\n") + "\n";
  } catch { return null; }
}

async function getFilteredXrayContent(enabled) {
  if (allEnabled(enabled)) return getXrayContent();
  try {
    const r = await db("SELECT value FROM settings WHERE key='subscription_xray_bycat' LIMIT 1");
    if (!r[0]) return null;
    const byCat = JSON.parse(r[0].value);
    const arr = [];
    for (const key of CATEGORY_ORDER) {
      if (enabled.has(key)) arr.push(...(byCat[key] || []));
    }
    return JSON.stringify(arr);
  } catch { return null; }
}

async function getFilteredSingboxContent(enabled) {
  if (allEnabled(enabled)) return getSingboxContent();
  try {
    const raw = await getSingboxContent();
    if (!raw) return null;
    const cfg = JSON.parse(raw);
    const enabledTitles = CATEGORY_ORDER.filter(k => enabled.has(k)).map(k => CATEGORY_TITLES[k]);
    for (const ob of cfg.outbounds || []) {
      if (ob.type === "selector" && ob.tag === "NekrozVPN") {
        ob.outbounds = enabledTitles.length ? [...enabledTitles, "direct"] : ["direct"];
        ob.default = enabledTitles[0] || "direct";
      }
    }
    cfg.route = cfg.route || {};
    cfg.route.final = enabledTitles.length ? "NekrozVPN" : "direct";
    return JSON.stringify(cfg);
  } catch { return null; }
}

// Happ работает на Xray-core (не sing-box!) и при своём User-Agent ждёт МАССИВ
// отдельных Xray-профилей — так он показывает их как отдельные локации.
// Настоящие sing-box-клиенты (Hiddify, NekoBox) получают обычный sing-box конфиг.
function isHappClient(request) {
  const ua = (request.headers.get("User-Agent") || "").toLowerCase();
  return ua.includes("happ");
}

function isSingboxCoreClient(request) {
  const ua = (request.headers.get("User-Agent") || "").toLowerCase();
  return ["hiddify", "nekobox", "nekoray", "sing-box", "singbox"].some(m => ua.includes(m));
}

function txt(body, status = 200) {
  return new Response(body, { status, headers: {
    "Content-Type": "text/plain; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
  }});
}
