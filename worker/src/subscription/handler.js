// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN  —  Subscription Feed Handler (Multi-Tier Free / VIP)
// ═══════════════════════════════════════════════════════════════════════════════

import { BRAND_NAME, BOT_USERNAME } from "../config.js";
import { getUserByToken, getSetting } from "../db/turso.js";


function isSingboxCoreClient(request) {
  const ua = (request.headers.get("user-agent") || "").toLowerCase();
  return (
    (ua.includes("sing-box") || ua.includes("hiddify") || ua.includes("nekobox") || ua.includes("karing")) &&
    !ua.includes("happ")
  );
}

function encodeB64(str) {
  try {
    return btoa(unescape(encodeURIComponent(str)));
  } catch {
    return btoa(str);
  }
}

export async function handleSubscription(request) {
  const url = new URL(request.url);
  const token = url.searchParams.get("token");

  if (!token) {
    return new Response(
      `# ${BRAND_NAME} — Народный VPN\n# Ошибка: не указан токен доступа.\n# Получите персональную ссылку в боте: @${BOT_USERNAME}\n`,
      {
        status: 400,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      }
    );
  }

  const formatParam = url.searchParams.get("format");
  const isXray = formatParam === "xray";
  const isSingbox = formatParam === "singbox" || isSingboxCoreClient(request);
  const format = isXray ? "xray" : isSingbox ? "singbox" : "plain";

  // Тестовый токен
  if (token === "test" || token === "hqray-test") {
    const vipContent =
      format === "xray"
        ? await getSetting(request.env, "subscription_vip_xray")
        : format === "singbox"
        ? await getSetting(request.env, "subscription_vip_singbox")
        : await getSetting(request.env, "subscription_vip");

    return new Response(vipContent || "# Конфигурации временно обновляются\n", {
      headers: {
        "Content-Type": format === "plain" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
        "profile-title": encodeB64("⚡ HQRay VIP [Тест]"),
      },
    });
  }

  // Проверка пользователя в Turso DB
  const user = await getUserByToken(request.env, token);
  if (!user) {
    return new Response(
      `# ${BRAND_NAME}\n# Ошибка: токен не найден или был отозван.\n# Запустите @${BOT_USERNAME} для получения нового ключа.\n`,
      {
        status: 403,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      }
    );
  }

  if (user.banned) {
    return new Response(
      `# ${BRAND_NAME}\n# Ваш доступ заблокирован: ${user.ban_reason || "нарушение правил"}.\n# Поддержка: @${BOT_USERNAME}\n`,
      {
        status: 403,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      }
    );
  }

  const now = new Date();
  const exp = user.subscription_expires ? new Date(user.subscription_expires) : null;
  const isVip = exp && exp > now;

  let poolKey = isVip ? "subscription_vip" : "subscription_free";
  let profileTitle = isVip ? "⚡ HQRay VIP [60M+]" : "🌐 HQRay Free (6 серверов)";

  let content = null;
  if (format === "xray") {
    content = await getSetting(request.env, `${poolKey}_xray`);
    if (!content && !isVip) content = await getSetting(request.env, "subscription_xray");
  } else if (format === "singbox") {
    content = await getSetting(request.env, `${poolKey}_singbox`);
    if (!content && !isVip) content = await getSetting(request.env, "subscription_singbox");
  } else {
    content = await getSetting(request.env, poolKey);
    if (!content && !isVip) content = await getSetting(request.env, "subscription");
  }

  if (!content) {
    return new Response(
      `# ${BRAND_NAME}\n# Серверы обновляются, пожалуйста повторите через 1 минуту.\n`,
      {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      }
    );
  }

  const expireTs = isVip ? Math.floor(exp.getTime() / 1000) : 0;
  const usedMb = user.traffic_used_mb || 0;

  const headers = {
    "Content-Type": format === "plain" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8",
    "Content-Disposition": 'attachment; filename="hqray"',
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "profile-title": encodeB64(profileTitle),
    "subscription-userinfo": `upload=0; download=${usedMb * 1024 * 1024}; total=0; expire=${expireTs}`,
    "profile-update-interval": "1",
    "support-url": `https://t.me/${BOT_USERNAME}`,
  };

  return new Response(content, { headers });
}
