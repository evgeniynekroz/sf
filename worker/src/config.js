// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Configuration & Settings
// ═══════════════════════════════════════════════════════════════════════════════

export const BRAND_NAME = "HQRay VPN";
export const DEFAULT_BOT_TOKEN = "8933693835:AAF2hyPi6EPHTXljAQ5QylcU4UPcZPiRkuc";
export const DEFAULT_TURSO_URL = "https://nekrozvpn-evgen.aws-eu-west-1.turso.io";
export const DEFAULT_TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODI1Nzk2MTAsImlkIjoiMDE5ZjBhMDYtMWUwMS03MTIwLTg3ZGMtYWEyMmYxMjk3OGJhIiwicmlkIjoiNGZjYmQwOTAtODA0OS00ZjAwLWExN2ItNjY1Y2E2MDE0ZDVkIn0.Hsq1HO-Y7kB5l_O9QspI33eomZUAvWHfdfEXAxXZ8EmJmiC37FmkAXanQqazPsFf3uvds8vcfK1Ak_KDtkYgCg";
export const ADMIN_ID = 6168325401;
export const CHANNEL_USERNAME = "@hqray";
export const BOT_USERNAME = "hqraybot";

export const TRIAL_DAYS = 3;
export const REF_START_DAYS = 3;
export const REF_PAY_DAYS = 5;

export const PERIODS = {
  14: {
    days: 14,
    title: "14 дней",
    label: "14 дней — 59 ₽ (35 ⭐️)",
    priceRub: 59,
    stars: 35,
    badge: "Проба",
  },
  30: {
    days: 30,
    title: "30 дней",
    label: "30 дней — 99 ₽ (60 ⭐️)",
    priceRub: 99,
    stars: 60,
    badge: "Хит 🔥",
    popular: true,
  },
  90: {
    days: 90,
    title: "90 дней",
    label: "90 дней — 249 ₽ (149 ⭐️)",
    priceRub: 249,
    stars: 149,
    badge: "Выгода -20%",
  },
};

export const PERIOD_LIST = [14, 30, 90];

export function getConfig(env) {
  return {
    botToken: env?.BOT_TOKEN || DEFAULT_BOT_TOKEN,
    tursoUrl: env?.TURSO_URL || DEFAULT_TURSO_URL,
    tursoToken: env?.TURSO_TOKEN || DEFAULT_TURSO_TOKEN,
    adminId: Number(env?.ADMIN_ID || ADMIN_ID),
    channel: env?.CHANNEL_USERNAME || CHANNEL_USERNAME,
    botUsername: env?.BOT_USERNAME || BOT_USERNAME,
    cryptoPayToken: env?.CRYPTO_PAY_TOKEN || "",
    cryptoApiUrl: "https://pay.crypt.bot/api",
  };
}
