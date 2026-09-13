// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Telegram Bot API Client
// ═══════════════════════════════════════════════════════════════════════════════

export async function tgCall(token, method, data = {}) {
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    return await res.json();
  } catch (err) {
    console.error(`Telegram API error (${method}):`, err);
    return { ok: false, error: String(err) };
  }
}

export async function sendMessage(token, chatId, text, options = {}) {
  return tgCall(token, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    ...options,
  });
}

export async function editMessageText(token, chatId, messageId, text, options = {}) {
  return tgCall(token, "editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    ...options,
  });
}

export async function answerCallbackQuery(token, callbackQueryId, text = "", showAlert = false) {
  return tgCall(token, "answerCallbackQuery", {
    callback_query_id: callbackQueryId,
    text,
    show_alert: showAlert,
  });
}

export async function setMyCommands(token) {
  return tgCall(token, "setMyCommands", {
    commands: [
      { command: "start", description: "Главное меню" },
      { command: "cabinet", description: "Личный кабинет и ключи" },
      { command: "sub", description: "Моя ссылка на подписку" },
      { command: "buy", description: "Тарифы и оплата VIP" },
      { command: "status", description: "Статус серверов" },
      { command: "help", description: "Инструкция по настройке" },
    ],
  });
}
