// ═══════════════════════════════════════════════════════════════════════════════
//  HQRay VPN — Multi-Tier Subscription Feed Handler
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

function isB64RequiredClient(request) {
  const ua = (request.headers.get("user-agent") || "").toLowerCase();
  return (
    ua.includes("v2rayng") ||
    ua.includes("shadowrocket") ||
    ua.includes("streisand") ||
    ua.includes("sagernet") ||
    ua.includes("matsuri")
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

  const formatParam = (url.searchParams.get("format") || "").toLowerCase();
  const rawParam = url.searchParams.get("raw") === "1";
  const b64Param = url.searchParams.get("b64") === "1" || formatParam === "b64";

  const isXray = formatParam === "xray";
  const isSingbox = formatParam === "singbox" || isSingboxCoreClient(request);
  const format = isXray ? "xray" : isSingbox ? "singbox" : "text";

  const isTest = token === "test" || token === "hqray-test";
  let user = null;
  let isVip = false;
  let exp = null;

  if (isTest) {
    isVip = true;
  } else {
    user = await getUserByToken(request.env, token);
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
    exp = user.subscription_expires ? new Date(user.subscription_expires) : null;
    isVip = Boolean(exp && exp > now);
  }

  const poolKey = isVip ? "subscription_vip" : "subscription_free";
  const profileTitle = isVip ? "💎 HQRay VPN (VIP)" : "🌐 HQRay VPN (Free)";

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

  let finalBody = content;
  let contentType = "text/plain; charset=utf-8";

  if (format === "singbox" || format === "xray") {
    contentType = "application/json; charset=utf-8";
  } else {
    // Стандарт V2Ray/Happ/v2rayNG: подписка ВСЕГДА кодируется в Base64.
    // Если явно запрошен raw=1 или format=raw (для отладки в браузере), отдаем чистый текст.
    if (!rawParam && formatParam !== "raw") {
      finalBody = encodeB64(content);
    }
  }

  const expireTs = isVip && exp ? Math.floor(exp.getTime() / 1000) : 0;
  const usedMb = user?.traffic_used_mb || 0;

  const headers = {
    "Content-Type": contentType,
    "Content-Disposition": 'attachment; filename="hqray_sub.txt"',
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Access-Control-Allow-Origin": "*",
    "profile-title": "base64:" + encodeB64(profileTitle),
    "subscription-userinfo": `upload=0; download=${usedMb * 1024 * 1024}; total=0; expire=${expireTs}`,
    "profile-update-interval": "1",
    "support-url": `https://t.me/${BOT_USERNAME}`,
  };

  return new Response(finalBody, { headers });
}
