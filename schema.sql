-- Minimal sf schema. The builder also creates users/settings automatically.

CREATE TABLE IF NOT EXISTS users (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id          INTEGER UNIQUE NOT NULL,
  username             TEXT,
  full_name            TEXT,
  trial_used           INTEGER NOT NULL DEFAULT 0,
  subscription_token   TEXT UNIQUE,
  subscription_expires TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Suggested private-bot table. Not used by the public Worker directly.
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
);

CREATE INDEX IF NOT EXISTS idx_users_subscription_token ON users(subscription_token);
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
