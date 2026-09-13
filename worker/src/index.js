// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Cloudflare Worker Entry Point
// ═══════════════════════════════════════════════════════════════════════════════

import { BRAND_NAME, BOT_USERNAME, getConfig } from "./config.js";
import { db, dbRun } from "./db/turso.js";
import { sendMessage } from "./bot/telegram.js";
import { handleBotUpdate } from "./bot/handlers.js";
import { checkCryptoInvoice } from "./payment/cryptobot.js";
import { handleSubscription } from "./subscription/handler.js";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Telegram Bot Webhook
    if (request.method === "POST" && url.pathname === "/bot") {
      try {
        const update = await request.json();
        ctx.waitUntil(handleBotUpdate(env, update, request.url));
      } catch (err) {
        console.error("Error processing bot update:", err);
      }
      return new Response("ok");
    }

    // CryptoBot Webhook
    if (request.method === "POST" && url.pathname === "/cryptobot-webhook") {
      try {
        const body = await request.json();
        if (body.update_type === "invoice_paid" && body.payload?.invoice_id) {
          ctx.waitUntil(checkCryptoInvoice(env, body.payload.invoice_id));
        }
      } catch (err) {
        console.error("CryptoBot webhook error:", err);
      }
      return new Response("ok");
    }

    // Subscription feed
    if (url.pathname === "/sub" || url.pathname.startsWith("/sub/")) {
      request.env = env;
      return handleSubscription(request);
    }

    // Health & Infrastructure Status
    if (url.pathname === "/status" || url.pathname === "/health") {
      return new Response(
        JSON.stringify({
          status: "ok",
          service: BRAND_NAME,
          bot: `@${BOT_USERNAME}`,
          timestamp: new Date().toISOString(),
        }),
        {
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    // Default redirect to Telegram bot
    return Response.redirect(`https://t.me/${BOT_USERNAME}`, 302);
  },

  // Daily Cron Schedule: уведомления об истекающей подписке
  async scheduled(event, env, ctx) {
    const cfg = getConfig(env);
    try {
      const expiringUsers = await db(
        env,
        `SELECT telegram_id, full_name, subscription_expires 
         FROM users 
         WHERE subscription_expires IS NOT NULL 
           AND subscription_expires > datetime('now') 
           AND subscription_expires <= datetime('now', '+1 day') 
           AND reminder_sent = 0`
      );

      for (const u of expiringUsers) {
        await sendMessage(
          cfg.botToken,
          u.telegram_id,
          `⏳ <b>Внимание: ваша VIP-подписка ${BRAND_NAME} заканчивается менее чем через 24 часа!</b>\n\n` +
          `Чтобы не потерять доступ к максимальной скорости 60+ Мбит/с и YouTube 4K, продлите подписку заранее по народным ценам:`,
          {
            reply_markup: {
              inline_keyboard: [
                [{ text: "💳 Продлить VIP (от 59 ₽)", callback_data: "buy_menu" }],
                [{ text: "« В кабинет", callback_data: "cabinet" }],
              ],
            },
          }
        );
        await dbRun(env, "UPDATE users SET reminder_sent = 1 WHERE telegram_id = ?", [u.telegram_id]);
      }
    } catch (err) {
      console.error("Cron scheduled error:", err);
    }
  },
};
