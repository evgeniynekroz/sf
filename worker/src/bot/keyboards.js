// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Telegram Bot Keyboards
// ═══════════════════════════════════════════════════════════════════════════════

import { CHANNEL_USERNAME, PERIOD_LIST, PERIODS } from "../config.js";

export function mainMenuKeyboard() {
  return {
    inline_keyboard: [
      [{ text: "🚀 Подключить VPN (Ссылка и Happ)", callback_data: "get_sub_links" }],
      [{ text: "💳 Купить VIP (Народные цены от 59 ₽)", callback_data: "buy_menu" }],
      [
        { text: "👤 Личный кабинет", callback_data: "cabinet" },
        { text: "🎁 Бесплатные дни", callback_data: "ref_menu" },
      ],
      [
        { text: "📱 Инструкция", callback_data: "apps_guide" },
        { text: "📊 Статус серверов", callback_data: "server_status" },
      ],
      [
        { text: "📢 Наш канал", url: `https://t.me/${CHANNEL_USERNAME.replace("@", "")}` },
      ],
    ],
  };
}

export function cabinetKeyboard(subUrl) {
  const encoded = encodeURIComponent(subUrl);
  return {
    inline_keyboard: [
      [
        { text: "⚡️ Добавить в Happ (1 клик)", url: `happ://add/${encoded}` },
        { text: "📋 Инструкция", callback_data: "apps_guide" },
      ],
      [
        { text: "💳 Продлить VIP", callback_data: "buy_menu" },
        { text: "🎁 Рефералы (+3 дня)", callback_data: "ref_menu" },
      ],
      [{ text: "« Главное меню", callback_data: "main_menu" }],
    ],
  };
}

export function buyTariffsKeyboard() {
  const rows = PERIOD_LIST.map((days) => {
    const p = PERIODS[days];
    return [
      {
        text: `${p.badge || "⚡"} ${p.title} — ${p.priceRub} ₽ (${p.stars} ⭐️)`,
        callback_data: `select_period_${days}`,
      },
    ];
  });
  rows.push([{ text: "« Назад в меню", callback_data: "main_menu" }]);
  return { inline_keyboard: rows };
}

export function paymentMethodsKeyboard(days) {
  const p = PERIODS[days];
  return {
    inline_keyboard: [
      [{ text: `⭐️ Оплатить Stars (${p.stars} ⭐️)`, callback_data: `pay_stars_${days}` }],
      [{ text: `🤖 Оплатить CryptoBot (${p.priceRub} ₽)`, callback_data: `pay_crypto_${days}` }],
      [{ text: "« Выбрать другой срок", callback_data: "buy_menu" }],
    ],
  };
}

export function cryptoInvoiceKeyboard(payUrl, invoiceId) {
  return {
    inline_keyboard: [
      [{ text: "💳 Оплатить в @CryptoBot", url: payUrl }],
      [{ text: "🔄 Проверить оплату", callback_data: `check_crypto_${invoiceId}` }],
      [{ text: "« Назад к тарифам", callback_data: "buy_menu" }],
    ],
  };
}

export function subLinksKeyboard(subUrl) {
  const encoded = encodeURIComponent(subUrl);
  return {
    inline_keyboard: [
      [{ text: "⚡ Добавить в Happ (1 клик)", url: `happ://add/${encoded}` }],
      [{ text: "📱 Как настроить (инструкция)", callback_data: "apps_guide" }],
      [{ text: "💳 Купить VIP (60+ Мбит/с)", callback_data: "buy_menu" }],
      [{ text: "« Главное меню", callback_data: "main_menu" }],
    ],
  };
}

export function backToMenuKeyboard() {
  return {
    inline_keyboard: [[{ text: "« Главное меню", callback_data: "main_menu" }]],
  };
}
