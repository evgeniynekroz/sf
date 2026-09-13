// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Bot Text Templates
// ═══════════════════════════════════════════════════════════════════════════════

import { BRAND_NAME, TRIAL_DAYS, REF_START_DAYS, REF_PAY_DAYS, PERIODS, BOT_USERNAME } from "../config.js";

export function welcomeText(user, isNew = false) {
  const trialMsg = isNew
    ? `🎁 <b>Вам начислен бесплатный VIP доступ на ${TRIAL_DAYS} дня!</b>\nПопробуйте максимальную скорость и все локации прямо сейчас.\n\n`
    : "";
  return (
    `👋 Привет, <b>${user.full_name || "друг"}</b>!\n` +
    `Добро пожаловать в <b>${BRAND_NAME}</b> — Народный VPN нового поколения!\n\n` +
    trialMsg +
    `🛡️ <b>У нас 2 уровня доступа:</b>\n` +
    `• 🌐 <b>Free (Базовый)</b>: 6 серверов (DE, NL, FI, PL, SE + LTE). Бесплатно навсегда.\n` +
    `• ⚡ <b>VIP (Премиум)</b>: 15+ быстрых серверов со скоростью <b>60+ Мбит/с</b> (DE, NL, FI, EE, PL, SE, GB, US, TR, KZ, JP, AT + LTE-обход блокировок). YouTube 4K, Discord, игры без лагов!\n\n` +
    `💳 <b>Народные цены:</b> от <b>59 ₽</b> за 14 дней или <b>99 ₽</b> за месяц!\n` +
    `Нажмите кнопку ниже, чтобы получить подписку или настроить приложение 👇`
  );
}

export function cabinetText(user, subUrl) {
  const now = new Date();
  const exp = user.subscription_expires ? new Date(user.subscription_expires) : null;
  const isVip = exp && exp > now;
  let statusBadge = "🌐 Базовый (Free)";
  let expStr = "Бессрочно (6 серверов)";
  if (isVip) {
    statusBadge = "⚡ VIP Премиум (60+ Мбит/с)";
    expStr = exp.toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "long",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } else if (exp) {
    statusBadge = "⏳ VIP истек (действует Free тариф)";
    expStr = `Истек ${exp.toLocaleDateString("ru-RU")}`;
  }
  const token = user.subscription_token || "—";
  const refLink = `https://t.me/${BOT_USERNAME}?start=ref_${user.telegram_id}`;

  return (
    `👤 <b>Личный кабинет — ${BRAND_NAME}</b>\n\n` +
    `🆔 ID: <code>${user.telegram_id}</code>\n` +
    `🎖️ Тариф: <b>${statusBadge}</b>\n` +
    `📅 Срок действия VIP: <b>${expStr}</b>\n` +
    `🔑 Ваш персональный ключ: <code>${token}</code>\n\n` +
    `🔗 <b>Ваша ссылка на подписку:</b>\n<code>${subUrl}</code>\n\n` +
    `👥 <b>Реферальная программа:</b>\n` +
    `Приглашайте друзей и пользуйтесь VIP бесплатно!\n` +
    `• <b>+${REF_START_DAYS} дня</b> — когда друг запускает бота\n` +
    `• <b>+${REF_PAY_DAYS} дней</b> — когда друг оплачивает любой тариф\n` +
    `🔗 Ваша ссылка: <code>${refLink}</code>`
  );
}

export function buyMenuText() {
  return (
    `💳 <b>Тарифы ${BRAND_NAME} — Народный VPN</b>\n\n` +
    `Подписка открывает <b>VIP-пул</b>: проверенные скоростные серверы (DE, NL, FI, EE, PL, SE, GB, US, TR, KZ, JP, AT) ` +
    `со скоростью <b>60+ Мбит/с</b>, европейские страны без ограничений, доступ ко всем заблокированным ресурсам и обход блокировок ТСПУ/РКН.\n\n` +
    `⚡ <b>14 дней</b> — <b>59 ₽</b> (35 ⭐️)\n` +
    `🔥 <b>30 дней (1 месяц)</b> — <b>99 ₽</b> (60 ⭐️) <i>[Хит продаж]</i>\n` +
    `💎 <b>90 дней (3 месяца)</b> — <b>249 ₽</b> (149 ⭐️) <i>[Выгода 20%]</i>\n\n` +
    `Принимаем <b>Telegram Stars ⭐️</b> и <b>CryptoBot 🤖</b> (USDT, TON, Карты РФ через P2P). Выберите подходящий срок:`
  );
}

export function paymentMethodText(days) {
  const period = PERIODS[days];
  return (
    `💳 <b>Оплата подписки: ${period.title}</b>\n\n` +
    `Стоимость: <b>${period.priceRub} ₽</b> или <b>${period.stars} ⭐️ Telegram Stars</b>\n\n` +
    `Выберите удобный способ оплаты:\n` +
    `• <b>⭐️ Telegram Stars</b> — моментальная оплата в 1 клик с карты или баланса Telegram.\n` +
    `• <b>🤖 CryptoBot</b> — оплата через @CryptoBot (USDT, TON, BTC, а также Карты/СБП через P2P).`
  );
}

export function appsGuideText(subUrl) {
  return (
    `📱 <b>Как подключить ${BRAND_NAME}:</b>\n\n` +
    `<b>1️⃣ Вариант 1 — Happ (Рекомендуется для iOS / Android / ПК)</b>\n` +
    `Happ — лучший клиент с поддержкой Xray Core и True Delay (честным замером задержки по HTTP GET).\n` +
    `1. Установите <b>Happ</b> из App Store или Google Play.\n` +
    `2. Нажмите кнопку <b>«⚡️ Добавить в Happ в 1 клик»</b> ниже.\n\n` +
    `<b>2️⃣ Вариант 2 — Любое другое приложение (Hiddify, v2rayNG, Streisand, Nekobox)</b>\n` +
    `1. Скопируйте вашу персональную ссылку подписки:\n` +
    `<code>${subUrl}</code>\n` +
    `2. В приложении нажмите <b>«+»</b> ➔ <b>«Добавить подписку из буфера»</b>.\n` +
    `3. Обновите список серверов и подключайтесь к любой локации!`
  );
}

export function subLinksText(user, subUrl) {
  const exp = user.subscription_expires ? new Date(user.subscription_expires) : null;
  const isVip = exp && exp > new Date();
  const tierName = isVip
    ? "⚡ VIP (60+ Мбит/с, Premium серверы)"
    : "🌐 Free (Базовый, 6 серверов)";
  return (
    `🔗 <b>Ваша подписка ${BRAND_NAME}</b>\n` +
    `Уровень: <b>${tierName}</b>\n\n` +
    `Ваша постоянная ссылка на конфигурацию:\n` +
    `<code>${subUrl}</code>\n\n` +
    `<i>Ссылка привязана к вашему аккаунту. При продлении подписки менять ссылку в приложении НЕ нужно — новые серверы загрузятся автоматически!</i>`
  );
}

export function statusText(vipCount = 0, freeCount = 0) {
  return (
    `📊 <b>Статус инфраструктуры ${BRAND_NAME}</b>\n\n` +
    `🟢 Все сервисы работают в штатном режиме.\n` +
    `• ⚡ <b>VIP серверы</b>: ${vipCount || "15+"} активных узлов (HTTP GET True Delay проверено)\n` +
    `• 🌐 <b>Free серверы</b>: ${freeCount || "6"} активных узлов\n` +
    `• 🔄 Автоматическое обновление пула: каждый час через GitHub Actions\n` +
    `• 🛡️ Фильтрация блокировок ТСПУ/РКН: активна`
  );
}
