// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — CryptoBot Payment Integration
// ═══════════════════════════════════════════════════════════════════════════════

import { getConfig, PERIODS } from "../config.js";
import { getSetting, logPayment, getPaymentByChargeId, confirmPayment, extendSub } from "../db/turso.js";

export async function getCryptoToken(env) {
  const cfg = getConfig(env);
  if (cfg.cryptoPayToken) return cfg.cryptoPayToken;
  const fromDb = await getSetting(env, "crypto_pay_token");
  return fromDb || "";
}

export async function createCryptoInvoice(env, { telegramId, days }) {
  const cfg = getConfig(env);
  const token = await getCryptoToken(env);
  if (!token) {
    return {
      ok: false,
      error: "CRYPTO_NOT_CONFIGURED",
      message:
        "Оплата через CryptoBot временно настраивается. Пожалуйста, воспользуйтесь Telegram Stars ⭐️",
    };
  }
  const period = PERIODS[days];
  if (!period) {
    return { ok: false, error: "INVALID_PERIOD" };
  }
  const payload = `tid:${telegramId}:days:${days}`;
  const reqBody = {
    currency_type: "fiat",
    fiat: "RUB",
    amount: String(period.priceRub),
    description: `HQRay VIP — ${period.title} (Народный ВПН)`,
    payload,
    paid_btn_name: "openBot",
    paid_btn_url: `https://t.me/${cfg.botUsername}`,
  };

  try {
    const res = await fetch(`${cfg.cryptoApiUrl}/createInvoice`, {
      method: "POST",
      headers: {
        "Crypto-Pay-API-Token": token,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(reqBody),
    });
    const data = await res.json();
    if (!data.ok) {
      console.error("CryptoBot createInvoice error:", data);
      return { ok: false, error: data.error?.name || "API_ERROR" };
    }
    const inv = data.result;
    const chargeId = `crypto_${inv.invoice_id}`;
    await logPayment(env, {
      telegramId,
      type: "cryptobot",
      amount: `${period.priceRub} RUB`,
      chargeId,
      days,
      status: "pending",
    });
    return {
      ok: true,
      invoiceId: inv.invoice_id,
      botInvoiceUrl: inv.bot_invoice_url,
      miniAppInvoiceUrl: inv.mini_app_invoice_url,
      payUrl: inv.bot_invoice_url || inv.mini_app_invoice_url || inv.web_app_invoice_url,
      priceRub: period.priceRub,
      days,
    };
  } catch (err) {
    console.error("CryptoBot request failed:", err);
    return { ok: false, error: "NETWORK_ERROR" };
  }
}

export async function checkCryptoInvoice(env, invoiceId) {
  const cfg = getConfig(env);
  const token = await getCryptoToken(env);
  if (!token) return { ok: false, error: "NO_TOKEN" };

  try {
    const res = await fetch(`${cfg.cryptoApiUrl}/getInvoices?invoice_ids=${invoiceId}`, {
      headers: { "Crypto-Pay-API-Token": token },
    });
    const data = await res.json();
    if (!data.ok || !data.result?.items?.length) {
      return { ok: false, status: "not_found" };
    }
    const inv = data.result.items[0];
    const chargeId = `crypto_${invoiceId}`;
    const payment = await getPaymentByChargeId(env, chargeId);

    if (inv.status === "paid") {
      if (payment && payment.status !== "confirmed") {
        await confirmPayment(env, chargeId);
        const days = payment.days || 30;
        const newExp = await extendSub(env, payment.telegram_id, days);
        return {
          ok: true,
          paid: true,
          firstConfirmation: true,
          newExp,
          days,
          telegramId: payment.telegram_id,
        };
      }
      return { ok: true, paid: true, firstConfirmation: false };
    }
    return { ok: true, paid: false, status: inv.status };
  } catch (err) {
    console.error("checkCryptoInvoice error:", err);
    return { ok: false, error: "NETWORK_ERROR" };
  }
}
