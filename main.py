from __future__ import annotations

import base64
import bisect
import ipaddress
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
XRAY_FILE         = OUTPUT_DIR / "xray.json"

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
WHITELIST_MAX        = 60
LTE_MAX              = 40

# ── Реальная проверка через Xray-core (тот же движок, что у Happ — не sing-box!) ─
REAL_CHECK_TOP_N     = 5    # сколько топ-кандидатов на слот реально проверяем
REAL_CHECK_WORKERS   = 10   # параллельных xray-процессов
REAL_CHECK_TIMEOUT   = 5    # секунд на curl через прокси (проверка доступности)
REAL_CHECK_SPEED_URL = "https://speed.cloudflare.com/__down?bytes=500000"  # ~500KB для проверки скорости
REAL_CHECK_MIN_KBPS  = 50   # ниже этого — сервер перегружен/бесполезен, отбраковываем
REAL_CHECK_IP_URL    = "https://api.ipify.org"
REAL_CHECK_URL       = "https://www.gstatic.com/generate_204"
REAL_CHECK_PORT_BASE = 21080

# ── Проверка достижимости из России через Globalping ─────────────────────────
# Это НЕ заменяет Xray real_check. Xray проверяет, что протокол работает.
# Globalping проверяет, что host:port финалиста виден из РФ.
RU_CHECK_MODE       = os.environ.get("RU_CHECK_MODE", "soft").strip().lower()  # off | soft | strict
GLOBALPING_LOCATION = os.environ.get("GLOBALPING_LOCATION", "RU").strip() or "RU"
GLOBALPING_LIMIT    = int(os.environ.get("GLOBALPING_LIMIT", "50"))
GLOBALPING_WORKERS  = int(os.environ.get("GLOBALPING_WORKERS", "4"))
GLOBALPING_TIMEOUT  = int(os.environ.get("GLOBALPING_TIMEOUT", "90"))
GLOBALPING_API      = "https://api.globalping.io/v1/measurements"

XRAY_BIN             = shutil.which("xray")
MY_PUBLIC_IP: str | None = None  # определяется один раз в build(), см. detect_my_ip()

# ── RKN-блокировки: небольшой (899 подсетей) официальный+community список ────────
RKN_BLOCKLIST_URL = "https://community.antifilter.download/list/community.lst"
RKN_NETWORKS: list = []  # заполняется один раз в build(), см. load_rkn_blocklist()

CATEGORY_TITLES = {
    "auto":      "🌐 Авто серверы",
    "lte":       "🏎️ LTE локации",
    "whitelist": "🏳 Белые списки",
}

PROJECT_NAME = os.environ.get("PROJECT_NAME", "sf")
PROJECT_USER_AGENT = os.environ.get("PROJECT_USER_AGENT", f"{PROJECT_NAME}-builder/1.0")
REQUIRE_XRAY = os.environ.get("REQUIRE_XRAY", "0").lower() in {"1", "true", "yes", "on"}

TURSO_URL   = os.environ.get("TURSO_URL", "").strip()
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "").strip()
TURSO_HTTP  = TURSO_URL.replace("libsql://", "https://") if TURSO_URL else ""

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(PROJECT_NAME)

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


def turso_configured() -> bool:
    return bool(TURSO_URL and TURSO_TOKEN)


def turso_pipeline(statements: list[dict]) -> list:
    if not turso_configured():
        raise RuntimeError("TURSO_URL/TURSO_TOKEN are not configured")
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



def _globalping_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": PROJECT_USER_AGENT,
    }


def _globalping_result_ok(payload: dict) -> bool | None:
    """Best-effort parser for Globalping MTR/TCP response.

    Returns:
      True  — at least one RU probe produced a non-error result;
      False — all probes explicitly failed;
      None  — response shape/status is unclear or measurement is not finished.
    """
    status = str(payload.get("status", "")).lower()
    if status and status not in {"finished", "completed"}:
        return None

    results = payload.get("results") or []
    if not results:
        return None

    saw_explicit_failure = False
    for item in results:
        result = item.get("result") or {}
        if not result:
            continue

        blob = json.dumps(result, ensure_ascii=False).lower()
        bad_markers = (
            "network unreachable",
            "host unreachable",
            "no route to host",
            "timed out",
            "timeout",
            "100% packet loss",
            "100.0%",
            "connection refused",
        )
        if any(m in blob for m in bad_markers):
            saw_explicit_failure = True
            continue

        # MTR/TCP reached at least some route/probe output without explicit error.
        # It is not a VLESS handshake check — Xray real_check handles that earlier.
        return True

    return False if saw_explicit_failure else None


def globalping_tcp_check(host: str, port: int) -> bool | None:
    """Checks host:port reachability from Russia using Globalping MTR/TCP.

    This is a Russia reachability signal, not a protocol check.
    """
    if not host or not port:
        return None

    payload = {
        "type": "mtr",
        "target": host,
        "locations": [{"country": GLOBALPING_LOCATION, "limit": 1}],
        "measurementOptions": {
            "protocol": "TCP",
            "port": int(port),
        },
    }

    try:
        req = Request(
            GLOBALPING_API,
            data=json.dumps(payload).encode(),
            headers=_globalping_headers(),
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            created = json.loads(resp.read().decode())
        measurement_id = created.get("id")
        if not measurement_id:
            return None

        deadline = time.time() + GLOBALPING_TIMEOUT
        while time.time() < deadline:
            req = Request(f"{GLOBALPING_API}/{measurement_id}", headers=_globalping_headers())
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            ok = _globalping_result_ok(data)
            if ok is not None:
                return ok
            time.sleep(2.0)
    except Exception as exc:
        logger.debug("Globalping RU-check %s:%s failed: %s", host, port, exc)
        return None
    return None


def check_ru_reachability(pairs: list[tuple[str, int]]) -> dict[tuple[str, int], bool | None]:
    """Best-effort RU reachability check for top finalists.

    RU_CHECK_MODE:
      off    — disabled;
      soft   — prefer RU-reachable nodes, but do not drop unknown nodes;
      strict — drop nodes unless Globalping confirms RU reachability.
    """
    mode = RU_CHECK_MODE
    if mode not in {"off", "soft", "strict"}:
        logger.warning("unknown RU_CHECK_MODE=%r, using soft", mode)
        mode = "soft"

    if mode == "off" or GLOBALPING_LIMIT <= 0:
        return {}

    unique = list(dict.fromkeys(pairs))[:GLOBALPING_LIMIT]
    if not unique:
        return {}

    logger.info(
        "RU reachability check via Globalping: %d host:port, location=%s, mode=%s",
        len(unique), GLOBALPING_LOCATION, mode,
    )

    result: dict[tuple[str, int], bool | None] = {}
    workers = max(1, min(GLOBALPING_WORKERS, len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(globalping_tcp_check, h, p): (h, p) for h, p in unique}
        for fut in as_completed(futures):
            hp = futures[fut]
            try:
                result[hp] = fut.result()
            except Exception:
                result[hp] = None

    yes = sum(1 for v in result.values() if v is True)
    no = sum(1 for v in result.values() if v is False)
    unknown = sum(1 for v in result.values() if v is None)
    logger.info("RU reachability: yes=%d no=%d unknown=%d", yes, no, unknown)
    return result

def categorize(configs: list[ConfigItem], geo: dict[str, GeoInfo]) -> dict[str, list[tuple[str, str]]]:
    """
    Возвращает {category: [(label, raw), ...]} — только auto/lte/whitelist.
    Три ступени фильтрации: (0) RKN-блоклист — заведомо заблокированные в РФ
    адреса выкидываются, не тратя на них время; (1) TCP+пинг — отсеивает
    полностью мёртвые; (2) реальная проверка через Xray-core у топ-кандидатов —
    честно поднимает туннель и проверяет, что через него правда качается трафик.
    """
    # ── кандидаты с известным хостом/портом/гео, за вычетом RKN-заблокированных ──
    candidates: list[tuple] = []  # (cc, country_ru, protocol, raw, (host, port), kind)
    rkn_skipped = 0
    for item in configs:
        hp = extract_host_port(item.value)
        if not hp or hp[0] not in geo:
            continue
        if is_rkn_blocked(hp[0]):
            rkn_skipped += 1
            continue
        g = geo[hp[0]]
        candidates.append((g.cc, g.country_ru, item.protocol, item.value, hp, item.kind))
    if rkn_skipped:
        logger.info("RKN-фильтр: пропущено %d адресов из заблокированных в России подсетей", rkn_skipped)

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
        return country_ru  # ms используется только для выбора победителя, не для показа —
        # это пинг с раннера GitHub Actions до сервера, а не реальный пинг пользователя,
        # и показывать его как будто это "твоя" скорость было бы нечестно.

    # ── Реальная проверка: берём топ-N (по пингу) кандидатов на каждый слот
    # (страна+протокол для auto, буфер для whitelist/lte) и честно поднимаем
    # через Xray-core + curl. TCP-порт мог ответить, а VPN за ним — не работать.
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
        # Все категории теперь обязаны реально пройти проверку — раньше
        # "Остальные" были исключением, но эту категорию убрали целиком.
        return check_result.get(raw, False)

    # ── RU reachability: проверяем из России только финалистов, которые уже
    # прошли Xray real_check. Это экономит API и не заменяет проверку протокола.
    ru_pairs = [c[4] for c in alive if c[3] in finalist_raws and real_ok(c[3])]
    ru_reachability = check_ru_reachability(ru_pairs)

    def ru_ok(hp: tuple[str, int]) -> bool:
        if RU_CHECK_MODE == "strict":
            return ru_reachability.get(hp) is True
        return True

    # В soft-режиме RU-доступные узлы выбираются первыми, но неизвестные не
    # выбрасываются — это защищает от временной недоступности Globalping/RU probe.
    selection_alive = sorted(alive, key=lambda c: (ru_reachability.get(c[4]) is not True, c[6]))

    # ── AUTO: до AUTO_MAX_PER_COUNTRY конфигов на страну, по одному на протокол ─
    auto_by_country: dict[str, dict[str, tuple[str, str, float]]] = defaultdict(dict)  # cc -> {protocol: (country_ru, raw, ms)}
    for cc, country_ru, proto, raw, hp, kind, ms in selection_alive:
        if kind != "auto" or raw not in finalist_raws or not real_ok(raw) or not ru_ok(hp):
            continue
        slot = auto_by_country[cc]
        if proto not in slot and len(slot) < AUTO_MAX_PER_COUNTRY:
            slot[proto] = (country_ru, raw, ms)

    auto_entries: list[tuple[str, str]] = []  # (label, raw)
    for cc, protos in sorted(auto_by_country.items(), key=lambda kv: next(iter(kv[1].values()))[0]):
        flag  = COUNTRY_FLAGS.get(cc, "🌐")
        items = list(protos.values())  # [(country_ru, raw, ms), ...] — уже отсортированы по ms (alive был отсортирован)
        for idx, (country_ru, raw, ms) in enumerate(items, start=1):
            suffix = f" {idx}" if len(items) > 1 else ""
            auto_entries.append((f"{flag} {fmt(country_ru, ms)}{suffix}", raw))

    # ── WHITELIST / LTE: живые из источников этого kind, самые быстрые первыми,
    #    и обязательно прошедшие реальную проверку ──────────────────────────────
    def collect(kind: str, limit: int, tag: str) -> list[tuple[str, str]]:
        seen_hosts: set[str] = set()
        out: list[tuple[str, str]] = []
        for cc, country_ru, proto, raw, hp, k, ms in selection_alive:
            if k != kind or hp[0] in seen_hosts or raw not in finalist_raws or not real_ok(raw) or not ru_ok(hp):
                continue
            seen_hosts.add(hp[0])
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            out.append((f"{flag} {fmt(country_ru, ms)} [{tag}]", raw))
            if len(out) >= limit:
                break
        return out

    whitelist_entries = collect("whitelist", WHITELIST_MAX, "Whitelist")
    lte_entries        = collect("lte", LTE_MAX, "LTE")

    return {
        "auto": auto_entries,
        "lte": lte_entries,
        "whitelist": whitelist_entries,
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


def load_rkn_blocklist() -> list[tuple[int, int]]:
    """
    Скачивает community.antifilter.download список (899 подсетей, заблокированных
    РКН+community). Возвращает отсортированные (start_int, end_int) для быстрого
    поиска через bisect — чтобы не тратить время на TCP/реальную проверку
    заведомо заблокированных в России адресов.
    """
    try:
        req = Request(RKN_BLOCKLIST_URL)
        with urlopen(req, timeout=15) as resp:
            lines = resp.read().decode().splitlines()
    except Exception as exc:
        logger.warning("не удалось скачать RKN-блоклист: %s — фильтр пропущен", exc)
        return []

    ranges: list[tuple[int, int]] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
            if net.version != 4:
                continue  # у нас только IPv4-хосты в конфигах
            ranges.append((int(net.network_address), int(net.broadcast_address)))
        except ValueError:
            continue
    ranges.sort()
    logger.info("RKN-блоклист: %d подсетей загружено", len(ranges))
    return ranges


def is_rkn_blocked(host: str) -> bool:
    """True, если host (IPv4-литерал) попадает в один из заблокированных РКН диапазонов."""
    if not RKN_NETWORKS:
        return False
    try:
        ip_int = int(ipaddress.ip_address(host))
    except ValueError:
        return False  # это домен, не IP — пропускаем проверку (см. ограничение ниже)
    idx = bisect.bisect_right(RKN_NETWORKS, (ip_int, float("inf"))) - 1
    if idx < 0:
        return False
    start, end = RKN_NETWORKS[idx]
    return start <= ip_int <= end


def detect_my_ip() -> str | None:
    """IP самого раннера GitHub Actions — чтобы поймать случаи, когда трафик
    по ошибке идёт мимо туннеля напрямую (тогда curl вернёт IP раннера, а не сервера)."""
    try:
        req = Request(REAL_CHECK_IP_URL)
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode().strip()
    except Exception as exc:
        logger.warning("не удалось определить свой IP: %s", exc)
        return None


def real_check(raw: str, port: int) -> bool:
    """
    Честная проверка через Xray-core (тот же движок, что использует Happ — НЕ sing-box,
    у них есть нюансы совместимости, особенно на Reality/XTLS). Три уровня проверки:
    1. Реально ли поднимается прокси и отвечает ли эталонный сайт через него
    2. Действительно ли трафик идёт ЧЕРЕЗ туннель (сверяем исходящий IP с IP раннера —
       если совпал, значит соединение случайно пошло напрямую, а не через сервер)
    3. Не настолько ли сервер перегружен, что толку от него ноль (мини-проверка скорости)
    """
    if not XRAY_BIN:
        return True  # xray не установлен (например, локальный прогон) — не блокируем пайплайн

    ob = _xray_outbound(raw)
    if ob is None:
        return True  # протокол, который мы не умеем поднять (ssr и т.п.) — пропускаем честно, не браним
    protocol, settings, stream = ob

    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{"tag": "socks", "port": port, "listen": "127.0.0.1", "protocol": "socks",
                       "settings": {"udp": False, "auth": "noauth"}}],
        "outbounds": [
            {"tag": "proxy", "protocol": protocol, "settings": settings, "streamSettings": stream},
            {"tag": "direct", "protocol": "freedom"},
        ],
    }

    proc = None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        cfg_path = f.name

    def _curl(url: str, extra: list[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["curl", "--socks5-hostname", f"127.0.0.1:{port}", "--max-time", str(timeout),
             "-s", *extra, url],
            capture_output=True, text=True, timeout=timeout + 3,
        )

    try:
        proc = subprocess.Popen(
            [XRAY_BIN, "run", "-c", cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.7)  # дать инбаунду подняться

        # 1. Базовая доступность
        basic = _curl(REAL_CHECK_URL, ["-o", "/dev/null", "-w", "%{http_code}"], REAL_CHECK_TIMEOUT)
        if basic.returncode != 0 or basic.stdout.strip() not in ("200", "204", "301", "302"):
            return False

        # 2. Действительно ли идёт через туннель, а не напрямую
        if MY_PUBLIC_IP:
            ip_result = _curl(REAL_CHECK_IP_URL, ["-o", "-"], REAL_CHECK_TIMEOUT)
            exit_ip = ip_result.stdout.strip()
            if ip_result.returncode != 0 or not exit_ip or exit_ip == MY_PUBLIC_IP:
                return False  # либо не ответил, либо утечка мимо прокси на сам раннер

        # 3. Скорость — сервер не должен быть настолько перегружен, что бесполезен
        speed = _curl(REAL_CHECK_SPEED_URL, ["-o", "/dev/null", "-w", "%{speed_download}"], REAL_CHECK_TIMEOUT)
        if speed.returncode == 0 and speed.stdout.strip():
            try:
                kbps = float(speed.stdout.strip()) / 1024
                if kbps < REAL_CHECK_MIN_KBPS:
                    return False
            except ValueError:
                pass  # не смогли распарсить скорость — не браним из-за этого одного пункта

        return True
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
    if not XRAY_BIN:
        if REQUIRE_XRAY:
            raise RuntimeError("xray не найден в PATH, а REQUIRE_XRAY=1")
        logger.warning("xray не найден в PATH — реальная проверка пропущена, остаёмся на TCP+пинге")
        return {r: True for r in unique}

    logger.info("реальная проверка (Xray-core) для %d финалистов...", len(unique))
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


def _xray_outbound(raw: str) -> tuple[str, dict, dict] | None:
    """Возвращает (protocol, settings, streamSettings) для Xray-core outbound "proxy". None — не смогли распарсить."""
    s = raw.strip()
    try:
        if s.lower().startswith("vless://"):
            p  = urlparse(s)
            qs = parse_qs(p.query)
            g  = lambda k, d=None: qs.get(k, [d])[0]
            host, port = p.hostname, p.port or 443
            if not host or not p.username:
                return None
            user: dict = {"id": p.username, "encryption": "none"}
            if g("flow"):
                user["flow"] = g("flow")
            settings = {"vnext": [{"address": host, "port": port, "users": [user]}]}
            net = g("type", "tcp")
            stream: dict = {"network": net}
            security = g("security", "none")
            stream["security"] = security if security in ("tls", "reality") else "none"
            if security == "tls":
                stream["tlsSettings"] = {"serverName": g("sni") or g("host") or host,
                                          "fingerprint": g("fp", "chrome")}
                if g("allowInsecure") == "1" or g("insecure") == "1":
                    stream["tlsSettings"]["allowInsecure"] = True
            elif security == "reality" and g("pbk"):
                stream["realitySettings"] = {"serverName": g("sni", host), "publicKey": g("pbk"),
                                              "shortId": g("sid", ""), "fingerprint": g("fp", "chrome")}
            if net == "ws":
                stream["wsSettings"] = {"path": g("path", "/")}
                if g("host"):
                    stream["wsSettings"]["headers"] = {"Host": g("host")}
            elif net == "grpc":
                stream["grpcSettings"] = {"serviceName": g("serviceName", "")}
            return "vless", settings, stream

        if s.lower().startswith("vmess://"):
            data = _b64json(s[8:].split("#")[0])
            host, port = str(data.get("add", "")), int(data.get("port", 443) or 443)
            uuid = data.get("id")
            if not host or not uuid:
                return None
            settings = {"vnext": [{"address": host, "port": port, "users": [
                {"id": uuid, "alterId": int(data.get("aid", 0) or 0), "security": "auto"}]}]}
            net = data.get("net", "tcp")
            stream = {"network": net}
            if str(data.get("tls", "")).lower() == "tls":
                stream["security"] = "tls"
                stream["tlsSettings"] = {"serverName": data.get("sni") or data.get("host") or host,
                                          "fingerprint": "chrome"}
            else:
                stream["security"] = "none"
            if net == "ws":
                stream["wsSettings"] = {"path": data.get("path", "/")}
                if data.get("host"):
                    stream["wsSettings"]["headers"] = {"Host": data["host"]}
            elif net == "grpc":
                stream["grpcSettings"] = {"serviceName": data.get("path", "")}
            return "vmess", settings, stream

        if s.lower().startswith("trojan://"):
            p  = urlparse(s)
            qs = parse_qs(p.query)
            g  = lambda k, d=None: qs.get(k, [d])[0]
            host, port = p.hostname, p.port or 443
            if not host or not p.username:
                return None
            settings = {"servers": [{"address": host, "port": port, "password": p.username}]}
            net = g("type", "tcp")
            stream = {"network": net, "security": "tls",
                      "tlsSettings": {"serverName": g("sni", host), "fingerprint": g("fp", "chrome")}}
            if net == "ws":
                stream["wsSettings"] = {"path": g("path", "/")}
            elif net == "grpc":
                stream["grpcSettings"] = {"serviceName": g("serviceName", "")}
            return "trojan", settings, stream

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
            settings = {"servers": [{"address": host, "port": int(port), "method": method, "password": password}]}
            return "shadowsocks", settings, {"network": "tcp", "security": "none"}
    except Exception:
        return None
    return None


XRAY_INBOUNDS = [
    {"tag": "socks", "port": 10808, "listen": "127.0.0.1", "protocol": "socks",
     "settings": {"udp": True, "auth": "noauth"},
     "sniffing": {"enabled": True, "routeOnly": False, "destOverride": ["http", "tls", "quic"]}},
    {"tag": "http", "port": 10809, "listen": "127.0.0.1", "protocol": "http",
     "settings": {"allowTransparent": False},
     "sniffing": {"enabled": True, "routeOnly": False, "destOverride": ["http", "tls", "quic"]}},
]
XRAY_ROUTING = {
    "rules": [
        {"type": "field", "protocol": ["bittorrent"], "outboundTag": "direct"},
    ],
    "domainMatcher": "hybrid",
    "domainStrategy": "IPIfNonMatch",
}


def build_xray_profile(raw: str, remarks: str) -> dict | None:
    """Полный Xray-core JSON-профиль (формат, который Happ реально понимает как ОДИН сервер в массиве подписки)."""
    ob = _xray_outbound(raw)
    if ob is None:
        return None
    protocol, settings, stream = ob
    return {
        "dns": {"servers": ["1.1.1.1", "8.8.8.8"], "queryStrategy": "UseIP"},
        "routing": XRAY_ROUTING,
        "inbounds": XRAY_INBOUNDS,
        "outbounds": [
            {"tag": "proxy", "protocol": protocol, "settings": settings, "streamSettings": stream},
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "remarks": remarks,
    }


def build_xray_header(title: str) -> dict:
    """Декоративная нерабочая 'локация' — разделитель категории (blackhole, никуда не подключается)."""
    return {
        "dns": {"servers": ["1.1.1.1", "8.8.8.8"], "queryStrategy": "UseIP"},
        "inbounds": XRAY_INBOUNDS,
        "outbounds": [
            {"tag": "proxy", "protocol": "blackhole"},
            {"tag": "direct", "protocol": "freedom"},
        ],
        "remarks": f"{title} ⬇️",
    }


def build_xray_by_category(categories: dict[str, list[tuple[str, str]]]) -> dict[str, list[dict]]:
    """То же самое, но по категориям отдельно — чтобы воркер мог включать/выключать категории на пользователя."""
    result: dict[str, list[dict]] = {}
    for key in CATEGORY_ORDER:
        entries = categories.get(key, [])
        if not entries:
            continue
        items = [build_xray_header(CATEGORY_TITLES[key])]
        for label, raw in entries:
            prof = build_xray_profile(raw, label)
            if prof is not None:
                items.append(prof)
        result[key] = items
    return result


def build_xray_array(categories: dict[str, list[tuple[str, str]]]) -> list[dict]:
    """Массив Xray-профилей — по одному конфигу на сервер, ровно формат, который Happ показывает списком локаций."""
    by_cat = build_xray_by_category(categories)
    profiles: list[dict] = []
    for key in CATEGORY_ORDER:
        profiles.extend(by_cat.get(key, []))
    return profiles


def build_singbox_config(categories: dict[str, list[tuple[str, str]]]) -> dict:
    """Полный sing-box конфиг (для Hiddify/NekoBox — реально sing-box-ядро, в отличие от Happ)."""
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
        outbounds.append({"type": "selector", "tag": PROJECT_NAME, "outbounds": group_tags + ["direct"], "default": group_tags[0]})
        final = PROJECT_NAME
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

CATEGORY_ORDER = ["auto", "lte", "whitelist"]


def build_subscription_data(categories: dict[str, list[tuple[str, str]]], generated_at: str) -> dict:
    cats = []
    for key in CATEGORY_ORDER:
        entries = categories.get(key, [])
        header  = header_entry(CATEGORY_TITLES[key] + " ⬇️")
        items   = [header] + [apply_label(raw, label) for label, raw in entries]
        cats.append({"key": key, "title": CATEGORY_TITLES[key], "count": len(entries), "items": items})
    return {"generated_at": generated_at, "categories": cats}


def flatten_subscription_text(data: dict) -> str:
    lines = ["# sf subscription", f"# generated_at: {data['generated_at']}", ""]
    for cat in data["categories"]:
        lines.extend(cat["items"])
    return "\n".join(lines) + "\n"


def save_subscription_to_turso(data: dict, flat_text: str, singbox: dict, xray_array: list[dict], xray_by_cat: dict[str, list[dict]]) -> None:
    # 'subscription_data'       — структурированный JSON для фильтрации по категориям в воркере (plain-текст).
    # 'subscription'            — плоский текст (обратная совместимость / TEST_TOKEN, все категории).
    # 'subscription_singbox'    — sing-box конфиг (для реально sing-box-ядра: Hiddify/NekoBox).
    # 'subscription_xray'       — массив Xray-core профилей, все категории (для Happ, TEST_TOKEN).
    # 'subscription_xray_bycat' — то же самое, но по категориям отдельно — для фильтрации на пользователя.
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
    turso_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('subscription_xray', ?)",
        [json.dumps(xray_array, ensure_ascii=False)],
    )
    turso_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('subscription_xray_bycat', ?)",
        [json.dumps(xray_by_cat, ensure_ascii=False)],
    )
    logger.info("Подписка сохранена в Turso (data + flat + singbox + xray + xray_bycat)")


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
    req = Request(url, headers={"User-Agent": PROJECT_USER_AGENT, "Accept": "*/*"})
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
    global MY_PUBLIC_IP, RKN_NETWORKS
    MY_PUBLIC_IP = detect_my_ip()
    logger.info("свой IP (для проверки утечки мимо туннеля): %s", MY_PUBLIC_IP or "не определён")
    RKN_NETWORKS = load_rkn_blocklist()

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
    xray_by_cat  = build_xray_by_category(categories)
    xray_array   = build_xray_array(categories)
    ensure_output_dir()
    SUBSCRIPTION_FILE.write_text(sub_content, encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps(sub_data, ensure_ascii=False, indent=2), encoding="utf-8")
    SINGBOX_FILE.write_text(json.dumps(singbox, ensure_ascii=False, indent=2), encoding="utf-8")
    XRAY_FILE.write_text(json.dumps(xray_array, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("subscription.txt, manifest.json, singbox.json и xray.json записаны")

    # Сохраняем в Turso, если заданы секреты. Для публичного репо секреты должны
    # приходить только из GitHub Actions Secrets / env, а не из исходников.
    if turso_configured():
        init_db()
        save_subscription_to_turso(sub_data, sub_content, singbox, xray_array, xray_by_cat)
    else:
        logger.warning("TURSO_URL/TURSO_TOKEN не заданы — файлы собраны локально, публикация в БД пропущена")


def main() -> int:
    try:
        build()
    except Exception as exc:
        logger.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
