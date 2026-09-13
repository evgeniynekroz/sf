// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Turso Database Client & Repository
// ═══════════════════════════════════════════════════════════════════════════════

import { getConfig, TRIAL_DAYS, REF_START_DAYS } from "../config.js";

function tArg(v) {
  if (v == null) return { type: "null" };
  if (typeof v === "number") return { type: "integer", value: String(v) };
  return { type: "text", value: String(v) };
}

export async function db(env, sql, args = []) {
  const cfg = getConfig(env);
  const r = await fetch(`${cfg.tursoUrl}/v2/pipeline`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cfg.tursoToken}`,
      "Content-Type": "application/json",
    },
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
    const msg = step.error?.message || "";
    if (!/duplicate column/i.test(msg)) {
      console.error("Turso error:", msg, "SQL:", sql);
    }
    return [];
  }
  const result = step?.response?.result;
  if (!result) return [];
  const cols = result.cols.map((c) => c.name);
  return result.rows.map((row) =>
    Object.fromEntries(
      cols.map((col, i) => {
        const cell = row[i];
        let v = cell?.value ?? null;
        if (v !== null && (cell?.type === "integer" || cell?.type === "float")) {
          v = Number(v);
        }
        return [col, v];
      })
    )
  );
}

export async function dbRun(env, sql, args = []) {
  return db(env, sql, args);
}

let dbInitialized = false;
export async function initDb(env) {
  if (dbInitialized) return;
  const stmts = [
    `CREATE TABLE IF NOT EXISTS users (
      id                   INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_id          INTEGER UNIQUE NOT NULL,
      username             TEXT,
      full_name            TEXT,
      state                TEXT,
      trial_used           INTEGER NOT NULL DEFAULT 0,
      subscription_token   TEXT UNIQUE,
      subscription_expires TEXT,
      referred_by          INTEGER,
      ref_start_given      INTEGER DEFAULT 0,
      ref_pay_given        INTEGER DEFAULT 0,
      bonus_month          TEXT,
      reminder_sent        INTEGER DEFAULT 0,
      traffic_used_mb      INTEGER DEFAULT 0,
      extra_device_slots   INTEGER DEFAULT 0,
      banned               INTEGER DEFAULT 0,
      ban_reason           TEXT,
      active_promo_code    TEXT,
      active_promo_type    TEXT,
      active_promo_value   REAL,
      enabled_categories   TEXT,
      created_at           TEXT NOT NULL DEFAULT (datetime('now'))
    )`,
    `CREATE TABLE IF NOT EXISTS settings (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )`,
    `CREATE TABLE IF NOT EXISTS payments (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_id  INTEGER NOT NULL,
      type         TEXT NOT NULL,
      amount       TEXT,
      days         INTEGER,
      status       TEXT DEFAULT 'pending',
      charge_id    TEXT UNIQUE,
      created_at   TEXT DEFAULT (datetime('now')),
      confirmed_at TEXT
    )`,
    `CREATE TABLE IF NOT EXISTS promo_codes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      code       TEXT UNIQUE NOT NULL,
      days       INTEGER NOT NULL,
      max_uses   INTEGER DEFAULT 1,
      uses       INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now'))
    )`,
    `CREATE TABLE IF NOT EXISTS devices (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_id INTEGER NOT NULL,
      fingerprint TEXT NOT NULL,
      label       TEXT,
      confirmed   INTEGER DEFAULT 1,
      first_seen  TEXT DEFAULT (datetime('now')),
      last_seen   TEXT DEFAULT (datetime('now')),
      UNIQUE(telegram_id, fingerprint)
    )`,
  ];
  for (const sql of stmts) {
    await dbRun(env, sql);
  }
  dbInitialized = true;
}

export async function getUser(env, telegramId) {
  await initDb(env);
  const rows = await db(env, "SELECT * FROM users WHERE telegram_id = ? LIMIT 1", [telegramId]);
  return rows[0] || null;
}

export async function getUserByToken(env, token) {
  await initDb(env);
  const rows = await db(env, "SELECT * FROM users WHERE subscription_token = ? LIMIT 1", [token]);
  return rows[0] || null;
}

export async function getOrCreateUser(env, from, referredBy = null) {
  await initDb(env);
  const tid = from.id;
  let user = await getUser(env, tid);
  let isNew = false;
  if (!user) {
    isNew = true;
    const token = generateToken();
    const expires = new Date(Date.now() + TRIAL_DAYS * 86400 * 1e3).toISOString();
    const fullName = [from.first_name, from.last_name].filter(Boolean).join(" ") || "Пользователь";
    await dbRun(
      env,
      `INSERT INTO users (telegram_id, username, full_name, trial_used, subscription_token, subscription_expires, referred_by)
       VALUES (?, ?, ?, 1, ?, ?, ?)`,
      [tid, from.username || null, fullName, token, expires, referredBy ? Number(referredBy) : null]
    );
    user = await getUser(env, tid);
    if (referredBy && Number(referredBy) !== tid) {
      await dbRun(
        env,
        `UPDATE users 
         SET subscription_expires = datetime(COALESCE(subscription_expires, datetime('now')), '+${REF_START_DAYS} days'),
             ref_start_given = COALESCE(ref_start_given, 0) + 1
         WHERE telegram_id = ?`,
        [Number(referredBy)]
      );
    }
  } else {
    if (from.username && user.username !== from.username) {
      await dbRun(env, "UPDATE users SET username = ? WHERE telegram_id = ?", [from.username, tid]);
      user.username = from.username;
    }
  }
  return { user, isNew };
}

export async function extendSub(env, telegramId, days) {
  await initDb(env);
  const user = await getUser(env, telegramId);
  const now = new Date();
  let baseDate = now;
  if (user?.subscription_expires) {
    const currentExp = new Date(user.subscription_expires);
    if (currentExp > now) {
      baseDate = currentExp;
    }
  }
  const newExp = new Date(baseDate.getTime() + days * 86400 * 1e3).toISOString();
  await dbRun(
    env,
    "UPDATE users SET subscription_expires = ?, reminder_sent = 0 WHERE telegram_id = ?",
    [newExp, telegramId]
  );
  return newExp;
}

export function generateToken() {
  const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
  let token = "hq_";
  for (let i = 0; i < 20; i++) {
    token += chars[Math.floor(Math.random() * chars.length)];
  }
  return token;
}

export async function logPayment(env, { telegramId, type, amount, chargeId, days, status = "pending" }) {
  await initDb(env);
  await dbRun(
    env,
    `INSERT INTO payments (telegram_id, type, amount, charge_id, days, status, confirmed_at)
     VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? = 'confirmed' THEN datetime('now') ELSE NULL END)`,
    [telegramId, type, String(amount), chargeId, days, status, status]
  );
}

export async function getPaymentByChargeId(env, chargeId) {
  await initDb(env);
  const rows = await db(env, "SELECT * FROM payments WHERE charge_id = ? LIMIT 1", [chargeId]);
  return rows[0] || null;
}

export async function confirmPayment(env, chargeId) {
  await initDb(env);
  await dbRun(
    env,
    "UPDATE payments SET status = 'confirmed', confirmed_at = datetime('now') WHERE charge_id = ?",
    [chargeId]
  );
}

export async function getSetting(env, key) {
  await initDb(env);
  const rows = await db(env, "SELECT value FROM settings WHERE key = ? LIMIT 1", [key]);
  return rows[0]?.value ?? null;
}

export async function setSetting(env, key, value) {
  await initDb(env);
  await dbRun(env, "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [key, value]);
}
