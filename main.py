from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

ROOT              = Path(__file__).resolve().parent
SOURCES_FILE      = ROOT / "sources.json"
OUTPUT_DIR        = ROOT / "output"
MANIFEST_FILE     = OUTPUT_DIR / "manifest.json"
SUBSCRIPTION_FILE = OUTPUT_DIR / "subscription.txt"
SINGBOX_FILE      = OUTPUT_DIR / "singbox.json"

FETCH_TIMEOUT    = 30
MAX_HTTP_BYTES   = 1_500_000
MAX_TREE_DEPTH   = 2
MAX_GITHUB_PAGES = 40
MAX_GITHUB_FILES = 120
GEO_HOST_LIMIT   = 8000   # максимум хостов на геолукап (см. пояснение у ip-api ниже)
GEO_BATCH_SLEEP  = 4.3    # ip-api free tier: лимит 15 запросов/мин = 1 запрос в 4с, берём с запасом
GEO_BATCH_RETRIES = 2      # повторных попыток на батч при 429/обрыве соединения
MAX_PER_SOURCE   = 6000   # страховка: один источник не должен задавить остальные
TCP_TIMEOUT      = 3      # секунд на попытку TCP-подключения
TCP_MAX_WORKERS  = 50     # параллельных проверок

AUTO_MAX_PER_COUNTRY = 3    # макс. "Авто N" на страну (по протоколам)
GAMING_COUNTRIES     = 6    # сколько ближайших к Москве стран берём для "Для игр"
WHITELIST_MAX        = 60
LTE_MAX              = 40
OTHER_MAX            = 300

# ── Реальная проверка через sing-box (не только TCP, а честное поднятие туннеля) ─
REAL_CHECK_TOP_N     = 5    # сколько топ-кандидатов на слот реально проверяем
REAL_CHECK_WORKERS   = 10   # параллельных sing-box процессов
REAL_CHECK_TIMEOUT   = 5    # секунд на curl через прокси
REAL_CHECK_URL       = "https://www.gstatic.com/generate_204"
REAL_CHECK_PORT_BASE = 21080
SINGBOX_BIN          = shutil.which("sing-box")

MOSCOW_LAT, MOSCOW_LON = 55.7558, 37.6173

CATEGORY_TITLES = {
    "auto":      "🌐 Авто серверы",
    "lte":       "🏎️ LTE локации",
    "gaming":    "🎮 Для игр",
    "whitelist": "🏳 Белые списки",
    "other":     "🔥 Остальные локации",
}

TURSO_URL   = "libsql://nekrozvpn-evgen.aws-eu-west-1.turso.io"
TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODI1Nzk2MTAsImlkIjoiMDE5ZjBhMDYtMWUwMS03MTIwLTg3ZGMtYWEyMmYxMjk3OGJhIiwicmlkIjoiNGZjYmQwOTAtODA0OS00ZjAwLWExN2ItNjY1Y2E2MDE0ZDVkIn0.Hsq1HO-Y7kB5l_O9QspI33eomZUAvWHfdfEXAxXZ8EmJmiC37FmkAXanQqazPsFf3uvds8vcfK1Ak_KDtkYgCg"
TURSO_HTTP  = TURSO_URL.replace("libsql://", "https://")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("nekrozvpn")

CONFIG_PATTERNS = (
    re.compile(r'\b(vless|trojan|ss|ssr|vmess|hy2|hysteria2|tuic)://[^\s"\'<>]+', re.IGNORECASE),
    re.compile(r'\b(vless|trojan|ss|ssr|vmess|hy2|hysteria2|tuic)\s*:\s*[^\s"\'<>]+', re.IGNORECASE),
)
HREF_RE   = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")

COUNTRY_FLAGS: dict[str, str] = {
    "AD":"🇦🇩","AE":"🇦🇪","AL":"🇦🇱","AM":"🇦🇲","AT":"🇦🇹","AU":"🇦🇺","AZ":"🇦🇿",
    "BA":"🇧🇦","BE":"🇧🇪","BG":"🇧🇬","BR":"🇧🇷","BY":"🇧🇾","CA":"🇨🇦","CH":"🇨🇭",
    "CY":"🇨🇾","CZ":"🇨🇿","DE":"🇩🇪","DK":"🇩🇰","EE":"🇪🇪","ES":"🇪🇸","FI":"🇫🇮",
    "FR":"🇫🇷","GB":"🇬🇧","GE":"🇬🇪","GR":"🇬🇷","HK":"🇭🇰","HR":"🇭🇷","HU":"🇭🇺",
    "ID":"🇮🇩","IE":"🇮🇪","IL":"🇮🇱","IN":"🇮🇳","IS":"🇮🇸","IT":"🇮🇹","JP":"🇯🇵",
    "KR":"🇰🇷","KZ":"🇰🇿","LT":"🇱🇹","LU":"🇱🇺","LV":"🇱🇻","MD":"🇲🇩","MT":"🇲🇹",
    "NL":"🇳🇱","NO":"🇳🇴","NZ":"🇳🇿","PL":"🇵🇱","PT":"🇵🇹","RO":"🇷🇴","RS":"🇷🇸",
    "RU":"🇷🇺","SE":"🇸🇪","SG":"🇸🇬","SI":"🇸🇮","SK":"🇸🇰","TR":"🇹🇷","TW":"🇹🇼",
    "UA":"🇺🇦","US":"🇺🇸","UZ":"🇺🇿",
}


# ── Модели ────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Source:
    name: str
    url: str
    kind: str = "auto"     # auto | whitelist | lte
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Source":
        name    = str(data.get("name", "")).strip()
        url     = str(data.get("url", "")).strip()
        kind    = str(data.get("kind", "auto")).strip() or "auto"
        enabled = bool(data.get("enabled", True))
        if not name or not url:
            raise ValueError(f"bad source: {data}")
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError(f"bad url: {url}")
        if kind not in {"auto", "whitelist", "lte"}:
            raise ValueError(f"bad kind: {kind}")
        return cls(name=name, url=url, kind=kind, enabled=enabled)


@dataclass(slots=True)
class ConfigItem:
    protocol: str
    value: str
    source_name: str
    source_url: str
    found_in: str
    kind: str = "auto"

    def key(self) -> str:
        return self.value.strip().rstrip("/")


# ── Turso HTTP API ────────────────────────────────────────────────────────────

def _arg(v) -> dict:
    return {"type": "null"} if v is None else {"type": "text", "value": str(v)}


def turso_pipeline(statements: list[dict]) -> list:
    payload = json.dumps({"requests": statements + [{"type": "close"}]}).encode()
    req = Request(
        f"{TURSO_HTTP}/v2/pipeline",
        data    = payload,
        headers = {
            "Authorization":  f"Bearer {TURSO_TOKEN}",
            "Content-Type":   "application/json",
        },
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("results", [])


def turso_exec(sql: str, args: list = []) -> None:
    turso_pipeline([{"type": "execute", "stmt": {"sql": sql, "args": [_arg(v) for v in args]}}])


def turso_query(sql: str, args: list = []) -> list[dict]:
    results = turso_pipeline([
        {"type": "execute", "stmt": {"sql": sql, "args": [_arg(v) for v in args]}}
    ])
    result = results[0].get("response", {}).get("result", {}) if results else {}
    cols   = [c["name"] for c in result.get("cols", [])]
    return [
        dict(zip(cols, [cell.get("value") for cell in row]))
        for row in result.get("rows", [])
    ]


# ── Инициализация БД ──────────────────────────────────────────────────────────

def init_db() -> None:
    turso_pipeline([
        {"type": "execute", "stmt": {"sql": """
            CREATE TABLE IF NOT EXISTS users (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id          INTEGER UNIQUE NOT NULL,
                username             TEXT,
                full_name            TEXT,
                trial_used           INTEGER NOT NULL DEFAULT 0,
                subscription_token   TEXT UNIQUE,
                subscription_expires TEXT,
                created_at           TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """, "args": []}},
        {"type": "execute", "stmt": {"sql": """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """, "args": []}},
    ])
    logger.info("БД инициализирована")


# ── Гео-определение ───────────────────────────────────────────────────────────

def extract_host_port(value: str) -> tuple[str, int] | None:
    s = value.strip()
    if s.lower().startswith("vmess://"):
        try:
            padded = s[8:] + "=" * (-len(s[8:]) % 4)
            data = json.loads(base64.b64decode(padded).decode())
            host = str(data.get("add", "")).strip()
            port = int(data.get("port", 443))
            return (host, port) if host else None
        except Exception:
            return None
    if "#" in s:
        s = s[: s.rindex("#")]
    try:
        parsed = urlparse(s)
        if not parsed.hostname:
            return None
        return (parsed.hostname, parsed.port or 443)
    except Exception:
        return None


def extract_host(value: str) -> str | None:
    hp = extract_host_port(value)
    return hp[0] if hp else None


def tcp_ping(host: str, port: int, timeout: float = TCP_TIMEOUT) -> float | None:
    """Возвращает задержку TCP-подключения в мс, либо None если недоступен."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000
    except Exception:
        return None


def check_tcp_ping(pairs: list[tuple[str, int]]) -> dict[tuple[str, int], float | None]:
    """Параллельно измеряет задержку для уникальных (host, port). None = недоступен."""
    unique = list(dict.fromkeys(pairs))
    result: dict[tuple[str, int], float | None] = {}
    with ThreadPoolExecutor(max_workers=TCP_MAX_WORKERS) as pool:
        futures = {pool.submit(tcp_ping, h, p): (h, p) for h, p in unique}
        for fut in as_completed(futures):
            hp = futures[fut]
            try:
                result[hp] = fut.result()
            except Exception:
                result[hp] = None
    return result


@dataclass(slots=True)
class GeoInfo:
    cc: str
    country_ru: str
    lat: float
    lon: float


def lookup_geo(hosts: list[str]) -> dict[str, GeoInfo]:
    """Возвращает {host: GeoInfo}. Названия стран — сразу на русском (lang=ru)."""
    result: dict[str, GeoInfo] = {}
    for i in range(0, len(hosts), 100):
        batch   = hosts[i : i + 100]
        payload = json.dumps([{"query": h} for h in batch]).encode()

        for attempt in range(GEO_BATCH_RETRIES + 1):
            try:
                req = Request(
                    "http://ip-api.com/batch?fields=query,status,country,countryCode,lat,lon&lang=ru",
                    data    = payload,
                    headers = {"Content-Type": "application/json"},
                )
                with urlopen(req, timeout=15) as resp:
                    for item in json.loads(resp.read()):
                        if item.get("status") == "success":
                            result[item["query"]] = GeoInfo(
                                cc=item["countryCode"],
                                country_ru=item["country"],
                                lat=float(item.get("lat", 0.0)),
                                lon=float(item.get("lon", 0.0)),
                            )
                break  # успех — выходим из retry-цикла
            except Exception as exc:
                if attempt < GEO_BATCH_RETRIES:
                    backoff = GEO_BATCH_SLEEP * (attempt + 2)  # растущая пауза при повторе
                    logger.warning("GeoIP batch %d: %s — retry через %.1fс", i, exc, backoff)
                    time.sleep(backoff)
                else:
                    logger.warning("GeoIP batch %d: %s — сдаёмся после %d попыток", i, exc, GEO_BATCH_RETRIES + 1)

        if i + 100 < len(hosts):
            time.sleep(GEO_BATCH_SLEEP)
    return result


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    r = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi/2)**2 + cos(p1)*cos(p2)*sin(dlmb/2)**2
    return 2 * r * atan2(sqrt(a), sqrt(1-a))


def categorize(configs: list[ConfigItem], geo: dict[str, GeoInfo]) -> dict[str, list[tuple[str, str]]]:
    """
    Возвращает {category: [(label, raw), ...]} по всем категориям.
    Двухступенчатая проверка: (1) TCP+пинг по всем адресам — быстро, отсеивает
    полностью мёртвые; (2) реальная проверка через sing-box у топ-кандидатов
    auto/lte/whitelist — честно поднимает туннель и проверяет, что через него
    правда качается трафик (открытый TCP-порт ещё не значит рабочий VPN).
    Категория "Остальные" — только TCP+пинг (без реальной проверки, иначе долго).
    """
    # ── кандидаты с известным хостом/портом/гео ────────────────────────────────
    candidates: list[tuple] = []  # (cc, country_ru, protocol, raw, (host, port), kind)
    for item in configs:
        hp = extract_host_port(item.value)
        if not hp or hp[0] not in geo:
            continue
        g = geo[hp[0]]
        candidates.append((g.cc, g.country_ru, item.protocol, item.value, hp, item.kind))

    unique_pairs = list({c[4] for c in candidates})
    logger.info("проверка задержки для %d уникальных адресов (все, без обрезки)...", len(unique_pairs))
    ping = check_tcp_ping(unique_pairs)
    alive_count = sum(1 for v in ping.values() if v is not None)
    logger.info("живых адресов: %d/%d", alive_count, len(unique_pairs))

    # Живые + сортировка по задержке (самые быстрые — первые). Дальше по всем
    # категориям используется схема "первый попавшийся на слот" — теперь это
    # автоматически значит "самый быстрый попавшийся".
    alive = [(*c, ping[c[4]]) for c in candidates if ping.get(c[4]) is not None]
    alive.sort(key=lambda c: c[6])

    def fmt(country_ru: str, ms: float) -> str:
        return f"{country_ru} ({int(round(ms))} мс)"

    # ── Реальная проверка: берём топ-N (по пингу) кандидатов на каждый слот
    # (страна+протокол для auto, буфер для whitelist/lte) и честно поднимаем
    # через sing-box + curl. TCP-порт мог ответить, а VPN за ним — не работать.
    finalist_raws: set[str] = set()
    auto_slot_count: dict[tuple[str, str], int] = defaultdict(int)
    for c in alive:
        cc, country_ru, proto, raw, hp, kind, ms = c
        if kind != "auto":
            continue
        key = (cc, proto)
        if auto_slot_count[key] < REAL_CHECK_TOP_N:
            finalist_raws.add(raw)
            auto_slot_count[key] += 1

    def _add_finalists(kind: str, limit: int) -> None:
        seen_hosts: set[str] = set()
        count = 0
        for c in alive:
            if c[5] != kind or c[4][0] in seen_hosts:
                continue
            seen_hosts.add(c[4][0])
            finalist_raws.add(c[3])
            count += 1
            if count >= limit * REAL_CHECK_TOP_N:
                break

    _add_finalists("whitelist", WHITELIST_MAX)
    _add_finalists("lte", LTE_MAX)

    check_result = real_check_batch(list(finalist_raws))

    def real_ok(raw: str) -> bool:
        # Не финалист (например, кандидат для "Остальные") — реально не проверяли,
        # честно и не притворяемся: пропускаем как есть (только TCP+пинг гарантирован).
        return raw not in finalist_raws or check_result.get(raw, True)

    # ── AUTO: до AUTO_MAX_PER_COUNTRY конфигов на страну, по одному на протокол ─
    auto_by_country: dict[str, dict[str, tuple[str, str, float]]] = defaultdict(dict)  # cc -> {protocol: (country_ru, raw, ms)}
    for cc, country_ru, proto, raw, hp, kind, ms in alive:
        if kind != "auto" or not real_ok(raw):
            continue
        slot = auto_by_country[cc]
        if proto not in slot and len(slot) < AUTO_MAX_PER_COUNTRY:
            slot[proto] = (country_ru, raw, ms)

    auto_entries: list[tuple[str, str]] = []          # (label, raw)
    auto_first_by_cc: dict[str, tuple[str, str]] = {}  # cc -> (label, raw) — для "Для игр"
    cc_coords: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for c in alive:
        cc_coords[c[0]].append((geo[c[4][0]].lat, geo[c[4][0]].lon))

    for cc, protos in sorted(auto_by_country.items(), key=lambda kv: next(iter(kv[1].values()))[0]):
        flag  = COUNTRY_FLAGS.get(cc, "🌐")
        items = list(protos.values())  # [(country_ru, raw, ms), ...] — уже отсортированы по ms (alive был отсортирован)
        for idx, (country_ru, raw, ms) in enumerate(items, start=1):
            suffix = f" {idx}" if len(items) > 1 else ""
            auto_entries.append((f"{flag} {fmt(country_ru, ms)}{suffix}", raw))
        first_ru, first_raw, first_ms = items[0]
        auto_first_by_cc[cc] = (fmt(first_ru, first_ms), first_raw)

    # ── GAMING: ближайшие к Москве страны (по средним координатам живых хостов) ─
    dist_by_cc: list[tuple[float, str]] = []
    for cc, coords in cc_coords.items():
        if cc not in auto_first_by_cc or not coords:
            continue
        avg_lat = sum(p[0] for p in coords) / len(coords)
        avg_lon = sum(p[1] for p in coords) / len(coords)
        dist_by_cc.append((haversine_km(MOSCOW_LAT, MOSCOW_LON, avg_lat, avg_lon), cc))
    dist_by_cc.sort()

    gaming_entries: list[tuple[str, str]] = []
    for _, cc in dist_by_cc[:GAMING_COUNTRIES]:
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        label, raw = auto_first_by_cc[cc]
        gaming_entries.append((f"{flag} {label}", raw))

    # ── WHITELIST / LTE: живые из источников этого kind, самые быстрые первыми ──
    def collect(kind: str, limit: int, tag: str) -> list[tuple[str, str]]:
        seen_hosts: set[str] = set()
        out: list[tuple[str, str]] = []
        for cc, country_ru, proto, raw, hp, k, ms in alive:
            if k != kind or hp[0] in seen_hosts or not real_ok(raw):
                continue
            seen_hosts.add(hp[0])
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            out.append((f"{flag} {fmt(country_ru, ms)} [{tag}]", raw))
            if len(out) >= limit:
                break
        return out

    whitelist_entries = collect("whitelist", WHITELIST_MAX, "Whitelist")
    lte_entries        = collect("lte", LTE_MAX, "LTE")

    # ── OTHER: живые auto-конфиги, не попавшие в "Авто" ─────────────────────────
    used_raw = {raw for _, raw in auto_entries}
    other_entries: list[tuple[str, str]] = []
    seen_hosts: set[str] = set()
    for cc, country_ru, proto, raw, hp, kind, ms in alive:
        if kind != "auto" or raw in used_raw or hp[0] in seen_hosts:
            continue
        seen_hosts.add(hp[0])
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        other_entries.append((f"{flag} {fmt(country_ru, ms)}", raw))
        if len(other_entries) >= OTHER_MAX:
            break

    return {
        "auto": auto_entries,
        "lte": lte_entries,
        "gaming": gaming_entries,
        "whitelist": whitelist_entries,
        "other": other_entries,
    }


# ── Трансформация имён ────────────────────────────────────────────────────────

def apply_label(raw: str, label: str) -> str:
    """Переименовывает конфиг (vmess — через JSON 'ps', остальные — через #fragment)."""
    s = raw.strip()
    if s.lower().startswith("vmess://"):
        try:
            enc    = s[8:]
            padded = enc + "=" * (-len(enc) % 4)
            data   = json.loads(base64.b64decode(padded).decode())
            data["ps"] = label
            return "vmess://" + base64.b64encode(
                json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
            ).decode()
        except Exception:
            pass
    base = s[: s.rindex("#")] if "#" in s else s.rstrip()
    return f"{base}#{label}"


def header_entry(title: str) -> str:
    """Декоративная нерабочая строка-разделитель категории (недоступный адрес)."""
    return apply_label(
        "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1"
        "?encryption=none&type=tcp&security=none",
        title,
    )


# ── Конвертер URI → sing-box outbound ─────────────────────────────────────────

def _b64json(b64: str) -> dict:
    padded = b64 + "=" * (-len(b64) % 4)
    return json.loads(base64.b64decode(padded).decode())


def uri_to_outbound(raw: str, tag: str) -> dict | None:
    """Конвертирует vless/vmess/trojan/ss URI в sing-box outbound. None — если не смогли распарсить."""
    s = raw.strip()
    try:
        if s.lower().startswith("vless://"):
            p  = urlparse(s)
            qs = parse_qs(p.query)
            g  = lambda k, d=None: qs.get(k, [d])[0]
            host, port = p.hostname, p.port or 443
            if not host or not p.username:
                return None
            ob: dict = {"type": "vless", "tag": tag, "server": host, "server_port": port, "uuid": p.username}
            if g("flow"):
                ob["flow"] = g("flow")
            security = g("security", "none")
            if security in ("tls", "reality"):
                tls = {"enabled": True, "server_name": g("sni") or g("host") or host,
                       "utls": {"enabled": True, "fingerprint": g("fp", "chrome")}}
                if g("allowInsecure") == "1" or g("insecure") == "1":
                    tls["insecure"] = True
                if security == "reality" and g("pbk"):
                    tls["reality"] = {"enabled": True, "public_key": g("pbk"), "short_id": g("sid", "")}
                ob["tls"] = tls
            net = g("type", "tcp")
            if net == "ws":
                headers = {"Host": g("host")} if g("host") else {}
                ob["transport"] = {"type": "ws", "path": g("path", "/"), "headers": headers}
            elif net == "grpc":
                ob["transport"] = {"type": "grpc", "service_name": g("serviceName", "")}
            return ob

        if s.lower().startswith("vmess://"):
            data = _b64json(s[8:].split("#")[0])
            host, port = str(data.get("add", "")), int(data.get("port", 443) or 443)
            uuid = data.get("id")
            if not host or not uuid:
                return None
            ob = {"type": "vmess", "tag": tag, "server": host, "server_port": port,
                  "uuid": uuid, "security": "auto", "alter_id": int(data.get("aid", 0) or 0)}
            if str(data.get("tls", "")).lower() == "tls":
                ob["tls"] = {"enabled": True, "server_name": data.get("sni") or data.get("host") or host,
                             "utls": {"enabled": True, "fingerprint": "chrome"}}
            net = data.get("net", "tcp")
            if net == "ws":
                headers = {"Host": data["host"]} if data.get("host") else {}
                ob["transport"] = {"type": "ws", "path": data.get("path", "/"), "headers": headers}
            elif net == "grpc":
                ob["transport"] = {"type": "grpc", "service_name": data.get("path", "")}
            return ob

        if s.lower().startswith("trojan://"):
            p  = urlparse(s)
            qs = parse_qs(p.query)
            g  = lambda k, d=None: qs.get(k, [d])[0]
            host, port = p.hostname, p.port or 443
            if not host or not p.username:
                return None
            ob = {"type": "trojan", "tag": tag, "server": host, "server_port": port, "password": p.username,
                  "tls": {"enabled": True, "server_name": g("sni", host),
                          "utls": {"enabled": True, "fingerprint": g("fp", "chrome")}}}
            net = g("type", "tcp")
            if net == "ws":
                ob["transport"] = {"type": "ws", "path": g("path", "/")}
            elif net == "grpc":
                ob["transport"] = {"type": "grpc", "service_name": g("serviceName", "")}
            return ob

        if s.lower().startswith("ss://"):
            body = s[5:].split("#")[0]
            if "@" in body:
                cred_b64, hostport = body.split("@", 1)
                hostport = hostport.split("?")[0]
                try:
                    cred = base64.urlsafe_b64decode(cred_b64 + "=" * (-len(cred_b64) % 4)).decode()
                except Exception:
                    cred = base64.b64decode(cred_b64 + "=" * (-len(cred_b64) % 4)).decode()
                method, password = cred.split(":", 1)
                host, port = hostport.rsplit(":", 1)
            else:
                decoded = base64.b64decode(body + "=" * (-len(body) % 4)).decode()
                methodpass, hostport = decoded.split("@", 1)
                method, password = methodpass.split(":", 1)
                host, port = hostport.rsplit(":", 1)
            return {"type": "shadowsocks", "tag": tag, "server": host, "server_port": int(port),
                    "method": method, "password": password}
        if s.lower().startswith("hysteria2://") or s.lower().startswith("hy2://"):
            p  = urlparse(s)
            qs = parse_qs(p.query)
            g  = lambda k, d=None: qs.get(k, [d])[0]
            host, port = p.hostname, p.port or 443
            password = p.username or g("password")
            if not host or not password:
                return None
            tls = {"enabled": True, "server_name": g("sni") or g("peer") or host}
            if g("insecure") == "1":
                tls["insecure"] = True
            ob = {"type": "hysteria2", "tag": tag, "server": host, "server_port": port,
                  "password": password, "tls": tls}
            if g("obfs"):
                ob["obfs"] = {"type": g("obfs"), "password": g("obfs-password", "")}
            return ob

        if s.lower().startswith("tuic://"):
            p  = urlparse(s)
            qs = parse_qs(p.query)
            g  = lambda k, d=None: qs.get(k, [d])[0]
            host, port = p.hostname, p.port or 443
            if not host or not p.username:
                return None
            return {
                "type": "tuic", "tag": tag, "server": host, "server_port": port,
                "uuid": p.username, "password": p.password or g("password", ""),
                "congestion_control": g("congestion_control", "bbr"),
                "tls": {"enabled": True, "server_name": g("sni", host), "alpn": [g("alpn", "h3")]},
            }

        # SSR (ssr://) сознательно не конвертируем: sing-box в принципе не поддерживает
        # протокол/обфускацию ShadowsocksR (только обычный shadowsocks). Это ограничение
        # самого sing-box, а не наше — фейковый "рабочий" outbound тут только сломал бы клиента.
    except Exception:
        return None
    return None  # неподдерживаемый протокол — пропускаем


def real_check(raw: str, port: int) -> bool:
    """
    Честная проверка: поднимает конфиг через sing-box локально (SOCKS5-инбаунд)
    и пробует реально скачать через него страницу. TCP-порт может быть открыт,
    а VPN за ним — не работать (протухший ключ, не тот протокол и т.п.) — вот
    это отличие мы и ловим.
    Возвращает True только если curl через прокси реально получил ответ.
    """
    if not SINGBOX_BIN:
        return True  # sing-box не установлен (например, локальный прогон) — не блокируем пайплайн

    ob = uri_to_outbound(raw, "check")
    if ob is None:
        return True  # протокол, который мы не умеем поднять (ssr и т.п.) — пропускаем честно, не браним

    config = {
        "outbounds": [ob, {"type": "direct", "tag": "direct"}],
        "inbounds": [{"type": "socks", "tag": "in", "listen": "127.0.0.1", "listen_port": port}],
        "route": {"final": "check"},
        "log": {"level": "error"},
    }

    proc = None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        cfg_path = f.name

    try:
        proc = subprocess.Popen(
            [SINGBOX_BIN, "run", "-c", cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.6)  # дать инбаунду подняться
        result = subprocess.run(
            ["curl", "--socks5-hostname", f"127.0.0.1:{port}",
             "--max-time", str(REAL_CHECK_TIMEOUT), "-s", "-o", "/dev/null",
             "-w", "%{http_code}", REAL_CHECK_URL],
            capture_output=True, text=True, timeout=REAL_CHECK_TIMEOUT + 3,
        )
        return result.returncode == 0 and result.stdout.strip() in ("200", "204", "301", "302")
    except Exception:
        return False
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


def real_check_batch(raws: list[str]) -> dict[str, bool]:
    """Параллельно прогоняет real_check по списку сырых конфигов (без дублей)."""
    unique = list(dict.fromkeys(raws))
    if not unique:
        return {}
    if not SINGBOX_BIN:
        logger.warning("sing-box не найден в PATH — реальная проверка пропущена, остаёмся на TCP+пинге")
        return {r: True for r in unique}

    logger.info("реальная проверка (sing-box) для %d финалистов...", len(unique))
    ports: Queue = Queue()
    for i in range(REAL_CHECK_WORKERS):
        ports.put(REAL_CHECK_PORT_BASE + i)

    def _run(raw: str) -> bool:
        port = ports.get()
        try:
            return real_check(raw, port)
        finally:
            ports.put(port)

    result: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=REAL_CHECK_WORKERS) as pool:
        futures = {pool.submit(_run, r): r for r in unique}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                result[r] = fut.result()
            except Exception:
                result[r] = False
    passed = sum(1 for v in result.values() if v)
    logger.info("реально работают: %d/%d", passed, len(unique))
    return result


def build_singbox_config(categories: dict[str, list[tuple[str, str]]]) -> dict:
    """Полный sing-box конфиг: только 1.1.1.1 (основной) / 8.8.8.8 (резерв), без спец-роутинга."""
    outbounds: list[dict] = []
    used_tags: set[str] = set()
    group_tags: list[str] = []

    def unique_tag(label: str) -> str:
        tag, n = label, 2
        while tag in used_tags:
            tag = f"{label} #{n}"
            n += 1
        used_tags.add(tag)
        return tag

    for key in CATEGORY_ORDER:
        tags_in_group: list[str] = []
        for label, raw in categories.get(key, []):
            tag = unique_tag(label)
            ob  = uri_to_outbound(raw, tag)
            if ob is None:
                continue
            outbounds.append(ob)
            tags_in_group.append(tag)
        if tags_in_group:
            gtag = CATEGORY_TITLES[key]
            outbounds.append({"type": "selector", "tag": gtag, "outbounds": tags_in_group, "default": tags_in_group[0]})
            group_tags.append(gtag)

    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})

    if group_tags:
        outbounds.append({"type": "selector", "tag": "NekrozVPN", "outbounds": group_tags + ["direct"], "default": group_tags[0]})
        final = "NekrozVPN"
    else:
        final = "direct"

    return {
        "dns": {
            "servers": [
                {"tag": "cloudflare", "address": "tls://1.1.1.1"},
                {"tag": "google", "address": "tls://8.8.8.8"},
            ],
            "final": "cloudflare",
        },
        "outbounds": outbounds,
        "route": {"final": final, "auto_detect_interface": True},
    }


# ── Генерация подписки ────────────────────────────────────────────────────────

# ── Генерация подписки ────────────────────────────────────────────────────────

CATEGORY_ORDER = ["auto", "lte", "gaming", "whitelist", "other"]


def build_subscription_data(categories: dict[str, list[tuple[str, str]]], generated_at: str) -> dict:
    cats = []
    for key in CATEGORY_ORDER:
        entries = categories.get(key, [])
        header  = header_entry(CATEGORY_TITLES[key] + " ⬇️")
        items   = [header] + [apply_label(raw, label) for label, raw in entries]
        cats.append({"key": key, "title": CATEGORY_TITLES[key], "count": len(entries), "items": items})
    return {"generated_at": generated_at, "categories": cats}


def flatten_subscription_text(data: dict) -> str:
    lines = ["# NekrozVPN subscription", f"# generated_at: {data['generated_at']}", ""]
    for cat in data["categories"]:
        lines.extend(cat["items"])
    return "\n".join(lines) + "\n"


def save_subscription_to_turso(data: dict, flat_text: str, singbox: dict) -> None:
    # 'subscription_data'    — структурированный JSON для фильтрации по категориям в воркере.
    # 'subscription'         — плоский текст (обратная совместимость / TEST_TOKEN).
    # 'subscription_singbox' — полный sing-box конфиг (для Happ/Hiddify/NekoBox).
    turso_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('subscription_data', ?)",
        [json.dumps(data, ensure_ascii=False)],
    )
    turso_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('subscription', ?)",
        [flat_text],
    )
    turso_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('subscription_singbox', ?)",
        [json.dumps(singbox, ensure_ascii=False)],
    )
    logger.info("Подписка сохранена в Turso (subscription_data + subscription + subscription_singbox)")


# ── Скрапинг (без изменений) ──────────────────────────────────────────────────

def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    raw   = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("sources")
    if not isinstance(items, list):
        raise ValueError("'sources' must be a list")
    return [Source.from_dict(i) for i in items if i.get("enabled", True)]


def fetch_text(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    req = Request(url, headers={"User-Agent": "NekrozVPN/1.0", "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_HTTP_BYTES + 1)[:MAX_HTTP_BYTES]
        return data.decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def normalize_text(t: str) -> str:
    return unescape(t).replace("\r", "")


def try_decode_base64(text: str) -> str | None:
    compact = "".join(text.split())
    if len(compact) < 32 or len(compact) % 4 != 0 or not BASE64_RE.fullmatch(text):
        return None
    try:
        dec = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False).decode("utf-8", errors="replace")
        if any(p in dec.lower() for p in ("vless://","trojan://","ss://","vmess://","hy2://","tuic://")):
            return dec
    except Exception:
        pass
    return None


def detect_protocol(v: str) -> str:
    low = v.lower().strip()
    for p in ("vless","trojan","ssr","ss","vmess","hy2","hysteria2","tuic"):
        if low.startswith(p + "://"):
            return p
    return "unknown"


def normalize_config(v: str) -> str:
    return v.strip().strip('"\'').rstrip(",;)").replace("\n","").replace("\t","")


def extract_configs(text: str, depth: int = 0) -> list[str]:
    text  = normalize_text(text)
    found = []
    for pat in CONFIG_PATTERNS:
        found += [normalize_config(m.group(0)) for m in pat.finditer(text)]
    if depth == 0:
        dec = try_decode_base64(text)
        if dec:
            found += extract_configs(dec, 1)
    seen, out = set(), []
    for v in found:
        k = v.strip()
        if k and k not in seen:
            seen.add(k); out.append(v)
    return out


def parse_text(source: Source, text: str, found_in: str) -> list[ConfigItem]:
    return [ConfigItem(detect_protocol(v), v, source.name, source.url, found_in, source.kind)
            for v in extract_configs(text)]


def is_github(url: str) -> bool:
    return urlparse(url).netloc.lower() in {"github.com","www.github.com"}


def gh_raw(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path.lstrip('/')}"


def gh_tree(owner: str, repo: str, branch: str, path: str = "") -> str:
    base = f"https://github.com/{owner}/{repo}/tree/{branch}"
    return f"{base}/{path.lstrip('/')}" if path else base


def crawl_github(source: Source) -> list[ConfigItem]:
    parts = [p for p in urlparse(source.url).path.split("/") if p]
    if len(parts) < 2:
        return []
    owner, repo = parts[0], parts[1].removesuffix(".git")
    queue: list[tuple[str,int]] = [(source.url.rstrip("/"), 0)]
    vp: set[str] = set()
    vf: set[str] = set()
    out: list[ConfigItem] = []

    while queue and len(vp) < MAX_GITHUB_PAGES and len(vf) < MAX_GITHUB_FILES:
        url, depth = queue.pop(0)
        if url in vp: continue
        vp.add(url)
        try:
            html = fetch_text(url)
        except Exception as e:
            logger.warning("skip %s: %s", url, e); continue

        prefix = f"/{owner}/{repo}/"
        links  = []
        for href in HREF_RE.findall(html):
            href = unquote(href.split("?")[0].split("#")[0])
            if href.startswith(prefix):
                rest = href[len(prefix):]
                if rest.startswith(("blob/","tree/")):
                    links.append(href)

        if not links:
            out += parse_text(source, html, url); continue

        for href in dict.fromkeys(links):
            parts2 = href[len(prefix):].split("/", 2)
            if len(parts2) < 2: continue
            kind, branch = parts2[0], parts2[1]
            path = parts2[2] if len(parts2) > 2 else ""
            if kind == "blob" and path:
                raw = gh_raw(owner, repo, branch, path)
                if raw in vf: continue
                vf.add(raw)
                try: out += parse_text(source, fetch_text(raw), raw)
                except Exception as e: logger.warning("skip %s: %s", raw, e)
            elif kind == "tree" and depth < MAX_TREE_DEPTH:
                t = gh_tree(owner, repo, branch, path)
                if t not in vp: queue.append((t, depth+1))
    try:
        out += parse_text(source, fetch_text(source.url), source.url)
    except Exception:
        pass
    return out


def dedupe(configs: list[ConfigItem]) -> list[ConfigItem]:
    """
    Дедупликация по сырой строке конфига. ВАЖНО: один и тот же сервер может
    встречаться в разных источниках с разным kind (например, в общем списке
    ("auto") и в отдельном "топ самых быстрых для мобильных" ("lte") —
    буквально та же строка). При совпадении оставляем более специальный kind
    (whitelist/lte), а не первый попавшийся — иначе такие категории будут
    пустыми, хотя источник честно что-то нашёл.
    """
    KIND_PRIORITY = {"whitelist": 0, "lte": 0, "auto": 1}
    best: dict[str, ConfigItem] = {}
    order: list[str] = []
    for c in configs:
        k = c.key()
        if k not in best:
            best[k] = c
            order.append(k)
        elif KIND_PRIORITY.get(c.kind, 1) < KIND_PRIORITY.get(best[k].kind, 1):
            best[k] = c  # апгрейд на более специальный kind, позиция в порядке не меняется
    return [best[k] for k in order]


# ── Точка входа ───────────────────────────────────────────────────────────────

def build() -> None:
    sources = load_sources()
    all_configs: list[ConfigItem] = []

    for src in sources:
        logger.info("processing %s", src.name)
        if is_github(src.url):
            items = crawl_github(src)
        else:
            try:
                items = parse_text(src, fetch_text(src.url), src.url)
            except Exception as e:
                logger.warning("skip %s: %s", src.name, e)
                continue
        logger.info("found %d in %s", len(items), src.name)
        if len(items) > MAX_PER_SOURCE:
            logger.warning("%s даёт %d конфигов — обрезаю до %d, чтобы не задавить мелкие источники",
                            src.name, len(items), MAX_PER_SOURCE)
            items = items[:MAX_PER_SOURCE]
        all_configs += items

    unique = dedupe(all_configs)
    logger.info("итого уникальных: %d", len(unique))

    # Гео-определение: берём уникальные хосты (не более GEO_HOST_LIMIT)
    host_to_configs: dict[str, list[ConfigItem]] = defaultdict(list)
    for c in unique:
        h = extract_host(c.value)
        if h:
            host_to_configs[h].append(c)

    unique_hosts = list(host_to_configs.keys())[:GEO_HOST_LIMIT]
    logger.info("геолукап для %d уникальных хостов...", len(unique_hosts))
    geo = lookup_geo(unique_hosts)
    logger.info("получено гео для %d хостов", len(geo))

    # Группируем по категориям (auto/lte/gaming/whitelist/other), с TCP-проверкой
    categories = categorize(unique, geo)
    for key in CATEGORY_ORDER:
        logger.info("категория %-10s: %d конфигов", key, len(categories.get(key, [])))

    # Записываем файлы
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    sub_data     = build_subscription_data(categories, generated_at)
    sub_content  = flatten_subscription_text(sub_data)
    singbox      = build_singbox_config(categories)
    ensure_output_dir()
    SUBSCRIPTION_FILE.write_text(sub_content, encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps(sub_data, ensure_ascii=False, indent=2), encoding="utf-8")
    SINGBOX_FILE.write_text(json.dumps(singbox, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("subscription.txt, manifest.json и singbox.json записаны")

    # Сохраняем в Turso (два запроса: init + insert)
    init_db()
    save_subscription_to_turso(sub_data, sub_content, singbox)


def main() -> int:
    try:
        build()
    except Exception as exc:
        logger.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
