// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Telegram Stars Payment Integration
// ═══════════════════════════════════════════════════════════════════════════════

import { getConfig, PERIODS, REF_PAY_DAYS } from "../config.js";
import { extendSub, logPayment, getUser, dbRun } from "../db/turso.js";
import { sendMessage } from "../bot/telegram.js";

export async function sendStarsInvoice(env, chatId, days) {
  const cfg = getConfig(env);
  const period = PERIODS[days];
  if (!period) return;
  const payload = `sub_${days}d`;
  const body = {
    chat_id: chatId,
    title: `HQRay VIP — ${period.title}`,
    description: `🚀 Безлимитный VIP доступ (60+ Мбит/с, 15+ локаций, обход блокировок, Happ/Hiddify/Xray)`,
    payload,
    currency: "XTR",
    prices: [{ label: period.title, amount: period.stars }],
  };
  const res = await fetch(`https://api.telegram.org/bot${cfg.botToken}/sendInvoice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export async function handlePreCheckoutQuery(env, preCheckoutQuery) {
  const cfg = getConfig(env);
  await fetch(`https://api.telegram.org/bot${cfg.botToken}/answerPreCheckoutQuery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pre_checkout_query_id: preCheckoutQuery.id,
      ok: true,
    }),
  });
}

export async function handleSuccessfulPayment(env, msg) {
  const cfg = getConfig(env);
  const tid = msg.from.id;
  const payment = msg.successful_payment;
  const payload = payment.invoice_payload;
  const chargeId = payment.telegram_payment_charge_id;
  const paidStars = payment.total_amount;
  if (payload.startsWith("sub_")) {
    const days = parseInt(payload.replace("sub_", "").replace("d", ""), 10);
    const newExp = await extendSub(env, tid, days);
    await logPayment(env, {
      telegramId: tid,
      type: "stars",
      amount: `${paidStars} XTR`,
      chargeId,
      days,
      status: "confirmed",
    });

    const user = await getUser(env, tid);
    if (user?.referred_by && !user.ref_pay_given) {
      await dbRun(
        env,
        `UPDATE users 
         SET subscription_expires = datetime(COALESCE(subscription_expires, datetime('now')), '+${REF_PAY_DAYS} days'),
             ref_pay_given = 1
         WHERE telegram_id = ?`,
        [user.referred_by]
      );
      await sendMessage(
        cfg.botToken,
        user.referred_by,
        `🎉 <b>Ваш друг оплатил подписку!</b>\nВам начислено <b>+${REF_PAY_DAYS} дней</b> VIP доступа в подарок!`
      );
    }

    const expDateRu = new Date(newExp).toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "long",
      year: "numeric",
    });

    await sendMessage(
      cfg.botToken,
      tid,
      `🎉 <b>Оплата через Stars успешно принята!</b>\n\n` +
      `⭐️ Оплачено: <b>${paidStars} ⭐️</b>\n` +
      `⚡ Тариф: <b>HQRay VIP (${days} дней)</b>\n` +
      `📅 Подписка активна до: <b>${expDateRu}</b>\n\n` +
      `Конфигурация в вашем приложении обновится автоматически! Приятного пользования 🚀`,
      {
        reply_markup: {
          inline_keyboard: [
            [{ text: "⚡ Моя подписка (ссылки)", callback_data: "get_sub_links" }],
            [{ text: "👤 Личный кабинет", callback_data: "cabinet" }],
          ],
        },
      }
    );

    if (cfg.adminId && cfg.adminId !== tid) {
      await sendMessage(
        cfg.botToken,
        cfg.adminId,
        `💰 <b>Новая оплата Stars!</b>\n` +
        `👤 Клиент: ${msg.from.first_name || ""} (@${msg.from.username || "нет"})\n` +
        `ID: <code>${tid}</code>\n` +
        `Тариф: <b>${days} дней</b> (${paidStars} ⭐️)\n` +
        `Транзакция: <code>${chargeId}</code>`
      );
    }
  }
}
