// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Telegram Bot Handlers
// ═══════════════════════════════════════════════════════════════════════════════

import { getConfig, REF_START_DAYS } from "../config.js";
import { getOrCreateUser, setSetting, getSetting } from "../db/turso.js";
import { sendMessage, editMessageText, answerCallbackQuery, setMyCommands } from "./telegram.js";
import {
  welcomeText,
  cabinetText,
  buyMenuText,
  paymentMethodText,
  appsGuideText,
  subLinksText,
  statusText,
} from "./texts.js";
import {
  mainMenuKeyboard,
  cabinetKeyboard,
  buyTariffsKeyboard,
  paymentMethodsKeyboard,
  cryptoInvoiceKeyboard,
  subLinksKeyboard,
  backToMenuKeyboard,
} from "./keyboards.js";
import { sendStarsInvoice, handlePreCheckoutQuery, handleSuccessfulPayment } from "../payment/stars.js";
import { createCryptoInvoice, checkCryptoInvoice } from "../payment/cryptobot.js";

export function getSubUrl(requestUrl, token) {
  const u = new URL(requestUrl);
  return `${u.origin}/sub?token=${token}`;
}

export async function handleBotUpdate(env, update, requestUrl) {
  const cfg = getConfig(env);

  if (update.pre_checkout_query) {
    return handlePreCheckoutQuery(env, update.pre_checkout_query);
  }

  if (update.message) {
    const msg = update.message;
    const tid = msg.from.id;

    if (msg.successful_payment) {
      return handleSuccessfulPayment(env, msg);
    }

    const text = msg.text || "";
    let refId = null;
    if (text.startsWith("/start")) {
      const parts = text.split(" ");
      if (parts[1]?.startsWith("ref_")) {
        refId = parts[1].replace("ref_", "");
      }
    }

    const { user, isNew } = await getOrCreateUser(env, msg.from, refId);
    const subUrl = getSubUrl(requestUrl, user.subscription_token);

    // Уведомление реферера при первом переходе друга
    if (isNew && refId && Number(refId) !== tid) {
      try {
        await sendMessage(
          cfg.botToken,
          Number(refId),
          `🎉 <b>Ваш друг запустил бота по вашей ссылке!</b>\nВам начислено <b>+${REF_START_DAYS} дня</b> VIP доступа в подарок!`
        );
      } catch (err) {
        console.error("Referral notify error:", err);
      }
    }

    if (text.startsWith("/set_crypto") && tid === cfg.adminId) {
      const cryptoToken = text.replace("/set_crypto", "").trim();
      if (cryptoToken) {
        await setSetting(env, "crypto_pay_token", cryptoToken);
        return sendMessage(cfg.botToken, tid, "✅ <b>Токен CryptoBot успешно сохранен в базе данных!</b>");
      }
      return sendMessage(cfg.botToken, tid, "Использование: <code>/set_crypto YOUR_CRYPTO_TOKEN</code>");
    }

    if (text.startsWith("/set_commands") && tid === cfg.adminId) {
      const res = await setMyCommands(cfg.botToken);
      return sendMessage(cfg.botToken, tid, res.ok ? "✅ <b>Команды бота успешно зарегистрированы в Telegram!</b>" : "❌ Ошибка регистрации команд.");
    }

    if (text.startsWith("/start")) {
      return sendMessage(cfg.botToken, tid, welcomeText(user, isNew), {
        reply_markup: mainMenuKeyboard(),
      });
    }

    if (text.startsWith("/cabinet")) {
      return sendMessage(cfg.botToken, tid, cabinetText(user, subUrl), {
        reply_markup: cabinetKeyboard(subUrl),
      });
    }

    if (text.startsWith("/buy")) {
      return sendMessage(cfg.botToken, tid, buyMenuText(), {
        reply_markup: buyTariffsKeyboard(),
      });
    }

    if (text.startsWith("/sub")) {
      return sendMessage(cfg.botToken, tid, subLinksText(user, subUrl), {
        reply_markup: subLinksKeyboard(subUrl),
      });
    }

    if (text.startsWith("/status")) {
      const vipDataStr = await getSetting(env, "subscription_vip_data");
      const freeDataStr = await getSetting(env, "subscription_free_data");
      let vipCount = 15;
      let freeCount = 6;
      try {
        if (vipDataStr) vipCount = JSON.parse(vipDataStr).count || vipCount;
        if (freeDataStr) freeCount = JSON.parse(freeDataStr).count || freeCount;
      } catch {}
      return sendMessage(cfg.botToken, tid, statusText(vipCount, freeCount), {
        reply_markup: backToMenuKeyboard(),
      });
    }

    if (text.startsWith("/terms")) {
      return sendMessage(
        cfg.botToken,
        tid,
        `📜 <b>Условия использования сервиса ${cfg.channel}:</b>\n\n` +
        `1. Сервис предоставляет безопасный доступ к сети интернет по протоколам VLESS Reality и Trojan.\n` +
        `2. Запрещено использование серверов для спама, сканирования портов, DDoS-атак и любой вредоносной активности.\n` +
        `3. При нарушении правил аккаунт блокируется без возврата средств.\n` +
        `4. Поддержка пользователей: @${cfg.botUsername}`,
        { reply_markup: backToMenuKeyboard() }
      );
    }

    if (text.startsWith("/help")) {
      return sendMessage(cfg.botToken, tid, appsGuideText(subUrl), {
        reply_markup: backToMenuKeyboard(),
      });
    }

    return sendMessage(cfg.botToken, tid, welcomeText(user, false), {
      reply_markup: mainMenuKeyboard(),
    });
  }

  if (update.callback_query) {
    const cb = update.callback_query;
    const tid = cb.from.id;
    const data = cb.data || "";
    const msgId = cb.message?.message_id;
    const { user } = await getOrCreateUser(env, cb.from);
    const subUrl = getSubUrl(requestUrl, user.subscription_token);

    if (data === "main_menu") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      return editMessageText(cfg.botToken, tid, msgId, welcomeText(user, false), {
        reply_markup: mainMenuKeyboard(),
      });
    }

    if (data === "cabinet") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      return editMessageText(cfg.botToken, tid, msgId, cabinetText(user, subUrl), {
        reply_markup: cabinetKeyboard(subUrl),
      });
    }

    if (data === "buy_menu") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      return editMessageText(cfg.botToken, tid, msgId, buyMenuText(), {
        reply_markup: buyTariffsKeyboard(),
      });
    }

    if (data.startsWith("select_period_")) {
      const days = parseInt(data.replace("select_period_", ""), 10);
      await answerCallbackQuery(cfg.botToken, cb.id);
      return editMessageText(cfg.botToken, tid, msgId, paymentMethodText(days), {
        reply_markup: paymentMethodsKeyboard(days),
      });
    }

    if (data.startsWith("pay_stars_")) {
      const days = parseInt(data.replace("pay_stars_", ""), 10);
      await answerCallbackQuery(cfg.botToken, cb.id, "Генерируем счет в Stars...");
      return sendStarsInvoice(env, tid, days);
    }

    if (data.startsWith("pay_crypto_")) {
      const days = parseInt(data.replace("pay_crypto_", ""), 10);
      await answerCallbackQuery(cfg.botToken, cb.id, "Создаем счет в CryptoBot...");
      const invoice = await createCryptoInvoice(env, { telegramId: tid, days });
      if (!invoice.ok) {
        return sendMessage(
          cfg.botToken,
          tid,
          `⚠️ ${invoice.message || "Ошибка создания счета в CryptoBot. Пожалуйста, воспользуйтесь оплатой через Telegram Stars ⭐️"}`
        );
      }
      return sendMessage(
        cfg.botToken,
        tid,
        `🤖 <b>Счет CryptoBot сформирован!</b>\n\n` +
        `Срок: <b>${days} дней VIP</b>\n` +
        `К оплате: <b>${invoice.priceRub} ₽</b>\n\n` +
        `Нажмите <b>«Оплатить в @CryptoBot»</b> для выбора криптовалюты (USDT, TON, BTC или Карты РФ/СБП через P2P бота).\n` +
        `После совершения перевода нажмите <b>«Проверить оплату»</b> 👇`,
        { reply_markup: cryptoInvoiceKeyboard(invoice.payUrl, invoice.invoiceId) }
      );
    }

    if (data.startsWith("check_crypto_")) {
      const invoiceId = data.replace("check_crypto_", "");
      const check = await checkCryptoInvoice(env, invoiceId);
      if (check.ok && check.paid) {
        await answerCallbackQuery(cfg.botToken, cb.id, "🎉 Оплата подтверждена!", true);
        const expDateRu = check.newExp ? new Date(check.newExp).toLocaleDateString("ru-RU") : "";
        return editMessageText(
          cfg.botToken,
          tid,
          msgId,
          `🎉 <b>Оплата через CryptoBot успешно подтверждена!</b>\n\n` +
          `⚡ Тариф: <b>HQRay VIP (${check.days || 30} дней)</b>\n` +
          (expDateRu ? `📅 Активна до: <b>${expDateRu}</b>\n\n` : "\n") +
          `Ваша подписка в приложении обновится автоматически. Приятного пользования 🚀`,
          { reply_markup: subLinksKeyboard(subUrl) }
        );
      } else {
        return answerCallbackQuery(
          cfg.botToken,
          cb.id,
          "⏳ Оплата пока не зафиксирована. Если вы уже перевели средства, подождите 10-20 секунд и нажмите снова.",
          true
        );
      }
    }

    if (data === "get_sub_links") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      return sendMessage(cfg.botToken, tid, subLinksText(user, subUrl), {
        reply_markup: subLinksKeyboard(subUrl),
      });
    }

    if (data === "apps_guide") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      return sendMessage(cfg.botToken, tid, appsGuideText(subUrl), {
        reply_markup: backToMenuKeyboard(),
      });
    }

    if (data === "ref_menu") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      return editMessageText(cfg.botToken, tid, msgId, cabinetText(user, subUrl), {
        reply_markup: cabinetKeyboard(subUrl),
      });
    }

    if (data === "server_status") {
      await answerCallbackQuery(cfg.botToken, cb.id);
      const vipDataStr = await getSetting(env, "subscription_vip_data");
      const freeDataStr = await getSetting(env, "subscription_free_data");
      let vipCount = 15;
      let freeCount = 6;
      try {
        if (vipDataStr) vipCount = JSON.parse(vipDataStr).count || vipCount;
        if (freeDataStr) freeCount = JSON.parse(freeDataStr).count || freeCount;
      } catch {}
      return editMessageText(cfg.botToken, tid, msgId, statusText(vipCount, freeCount), {
        reply_markup: backToMenuKeyboard(),
      });
    }

    await answerCallbackQuery(cfg.botToken, cb.id);
  }
}
