from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

ROOT              = Path(__file__).resolve().parent
SOURCES_FILE      = ROOT / "sources.json"
OUTPUT_DIR        = ROOT / "output"
MANIFEST_FILE     = OUTPUT_DIR / "manifest.json"
SUBSCRIPTION_FILE = OUTPUT_DIR / "subscription.txt"

FETCH_TIMEOUT    = 30
MAX_HTTP_BYTES   = 1_500_000
MAX_TREE_DEPTH   = 2
MAX_GITHUB_PAGES = 40
MAX_GITHUB_FILES = 120

TURSO_URL   = "libsql://nekrozvpn-evgen.aws-eu-west-1.turso.io"
TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODI1Nzk2MTAsImlkIjoiMDE5ZjBhMDYtMWUwMS03MTIwLTg3ZGMtYWEyMmYxMjk3OGJhIiwicmlkIjoiNGZjYmQwOTAtODA0OS00ZjAwLWExN2ItNjY1Y2E2MDE0ZDVkIn0.Hsq1HO-Y7kB5l_O9QspI33eomZUAvWHfdfEXAxXZ8EmJmiC37FmkAXanQqazPsFf3uvds8vcfK1Ak_KDtkYgCg"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("nekrozvpn")

CONFIG_PATTERNS = (
    re.compile(
        r'\b(vless|trojan|ss|ssr|vmess|hy2|hysteria2|tuic)://[^\s"\'<>]+',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(vless|trojan|ss|ssr|vmess|hy2|hysteria2|tuic)\s*:\s*[^\s"\'<>]+',
        re.IGNORECASE,
    ),
)

HREF_RE   = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")

# Флаги стран для удобочитаемых имён в VPN-приложении
COUNTRY_FLAGS: dict[str, str] = {
    "AD": "🇦🇩", "AE": "🇦🇪", "AF": "🇦🇫", "AL": "🇦🇱", "AM": "🇦🇲",
    "AT": "🇦🇹", "AU": "🇦🇺", "AZ": "🇦🇿", "BA": "🇧🇦", "BE": "🇧🇪",
    "BG": "🇧🇬", "BR": "🇧🇷", "BY": "🇧🇾", "CA": "🇨🇦", "CH": "🇨🇭",
    "CY": "🇨🇾", "CZ": "🇨🇿", "DE": "🇩🇪", "DK": "🇩🇰", "EE": "🇪🇪",
    "ES": "🇪🇸", "FI": "🇫🇮", "FR": "🇫🇷", "GB": "🇬🇧", "GE": "🇬🇪",
    "GR": "🇬🇷", "HK": "🇭🇰", "HR": "🇭🇷", "HU": "🇭🇺", "ID": "🇮🇩",
    "IE": "🇮🇪", "IL": "🇮🇱", "IN": "🇮🇳", "IS": "🇮🇸", "IT": "🇮🇹",
    "JP": "🇯🇵", "KR": "🇰🇷", "KZ": "🇰🇿", "LT": "🇱🇹", "LU": "🇱🇺",
    "LV": "🇱🇻", "MD": "🇲🇩", "ME": "🇲🇪", "MK": "🇲🇰", "MT": "🇲🇹",
    "NL": "🇳🇱", "NO": "🇳🇴", "NZ": "🇳🇿", "PL": "🇵🇱", "PT": "🇵🇹",
    "RO": "🇷🇴", "RS": "🇷🇸", "RU": "🇷🇺", "SE": "🇸🇪", "SG": "🇸🇬",
    "SI": "🇸🇮", "SK": "🇸🇰", "TR": "🇹🇷", "TW": "🇹🇼", "UA": "🇺🇦",
    "US": "🇺🇸", "UZ": "🇺🇿",
}


# ── Модели данных ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Source:
    name: str
    url: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        if not isinstance(data, dict):
            raise ValueError("each source entry must be an object")
        name    = str(data.get("name", "")).strip()
        url     = str(data.get("url", "")).strip()
        enabled = bool(data.get("enabled", True))
        if not name:
            raise ValueError("source name is empty")
        if not url:
            raise ValueError(f"source '{name}' has empty url")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"source '{name}' has invalid url")
        return cls(name=name, url=url, enabled=enabled)


@dataclass(slots=True)
class ConfigItem:
    protocol:    str
    value:       str
    source_name: str
    source_url:  str
    found_in:    str

    def normalized_key(self) -> str:
        return self.value.strip().rstrip("/")


# ── База данных (Turso / libsql) ──────────────────────────────────────────────

def _get_db():
    try:
        import libsql_experimental as libsql
        return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
    except ImportError:
        logger.warning("libsql-experimental не установлен — пропускаем сохранение в БД")
        return None


def init_db(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS configs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            protocol     TEXT NOT NULL,
            raw_value    TEXT NOT NULL UNIQUE,
            clean_value  TEXT NOT NULL,
            source_name  TEXT,
            source_url   TEXT,
            is_active    INTEGER NOT NULL DEFAULT 1,
            latency_ms   INTEGER,
            country_code TEXT,
            country      TEXT,
            last_seen    TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
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
    """)
    conn.commit()
    # Безопасная миграция — добавляем колонки если их нет (старая БД)
    for table, col, typedef in [
        ("configs", "latency_ms",         "INTEGER"),
        ("configs", "country_code",        "TEXT"),
        ("configs", "country",             "TEXT"),
        ("users",   "subscription_token",  "TEXT"),
        ("users",   "subscription_expires","TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass  # колонка уже существует
    logger.info("БД инициализирована")


def save_configs_to_db(conn, configs: list[ConfigItem]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE configs SET is_active = 0")
    for i, item in enumerate(configs, start=1):
        clean = transform_config_name(item.value, i)
        conn.execute("""
            INSERT INTO configs
                (protocol, raw_value, clean_value, source_name, source_url, is_active, last_seen)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(raw_value) DO UPDATE SET
                clean_value = excluded.clean_value,
                source_name = excluded.source_name,
                source_url  = excluded.source_url,
                is_active   = 1,
                last_seen   = excluded.last_seen
        """, [item.protocol, item.value, clean, item.source_name, item.source_url, now])
    conn.commit()
    active = conn.execute("SELECT COUNT(*) FROM configs WHERE is_active = 1").fetchone()[0]
    total  = conn.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
    logger.info("БД: %d активных из %d всего", active, total)


# ── Определение страны по IP ──────────────────────────────────────────────────

def extract_host_from_config(value: str) -> str | None:
    """Вытаскивает hostname или IP из конфига."""
    stripped = value.strip()

    # VMess: base64 JSON, хост в поле "add"
    if stripped.lower().startswith("vmess://"):
        encoded = stripped[8:]
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            data   = json.loads(base64.b64decode(padded).decode("utf-8"))
            host   = str(data.get("add", "")).strip()
            return host or None
        except Exception:
            return None

    # Для остальных: убираем #fragment и парсим как URL
    clean = stripped
    if "#" in clean:
        clean = clean[: clean.rindex("#")]
    try:
        host = urlparse(clean).hostname
        return host if host else None
    except Exception:
        return None


def lookup_countries_batch(hosts: list[str]) -> dict[str, dict]:
    """
    Запрашивает страну для списка IP/hostname через ip-api.com batch API.
    Возвращает {host: {"country": "Netherlands", "country_code": "NL"}}.
    Лимит: 100 адресов за запрос, 45 запросов в минуту (бесплатный план).
    """
    results: dict[str, dict] = {}
    batch_size = 100

    for batch_start in range(0, len(hosts), batch_size):
        batch   = hosts[batch_start : batch_start + batch_size]
        payload = json.dumps([{"query": h} for h in batch]).encode()
        try:
            req = Request(
                "http://ip-api.com/batch?fields=query,status,country,countryCode",
                data    = payload,
                headers = {"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            for item in data:
                if item.get("status") == "success":
                    results[item["query"]] = {
                        "country":      item["country"],
                        "country_code": item["countryCode"],
                    }
        except Exception as exc:
            logger.warning("GeoIP batch [%d..%d] ошибка: %s", batch_start, batch_start + batch_size, exc)

        # Не превышаем rate limit
        if batch_start + batch_size < len(hosts):
            time.sleep(1.5)

    return results


def enrich_with_geo(conn) -> None:
    """Заполняет country_code/country для активных конфигов у которых нет гео-данных."""
    rows = conn.execute(
        "SELECT id, raw_value FROM configs WHERE is_active = 1 AND country_code IS NULL LIMIT 500"
    ).fetchall()

    if not rows:
        logger.info("Гео-обогащение: все конфиги уже имеют данные о стране")
        return

    # Собираем уникальные хосты
    id_to_host: dict[int, str] = {}
    for row_id, raw_value in rows:
        host = extract_host_from_config(raw_value)
        if host:
            id_to_host[row_id] = host

    if not id_to_host:
        return

    # Дедупликация хостов для API-запроса
    unique_hosts   = list(set(id_to_host.values()))
    geo_by_host    = lookup_countries_batch(unique_hosts)

    updated = 0
    for row_id, host in id_to_host.items():
        if host in geo_by_host:
            info = geo_by_host[host]
            conn.execute(
                "UPDATE configs SET country_code = ?, country = ? WHERE id = ?",
                [info["country_code"], info["country"], row_id],
            )
            updated += 1
    conn.commit()
    logger.info("Гео-обогащение: %d из %d конфигов обновлено", updated, len(rows))


def get_best_per_country(conn) -> list[tuple[str, str, str]]:
    """
    Возвращает [(country_code, country, raw_value), ...] — лучший конфиг на страну.
    Приоритет: наименьший latency_ms → первый по id (порядку добавления).
    """
    rows = conn.execute("""
        SELECT country_code, country, raw_value
        FROM configs
        WHERE is_active    = 1
          AND country_code IS NOT NULL
          AND country_code != ''
        ORDER BY country_code,
                 CASE WHEN latency_ms IS NULL THEN 1 ELSE 0 END,
                 latency_ms ASC,
                 id ASC
    """).fetchall()

    seen:   set[str]                      = set()
    result: list[tuple[str, str, str]]    = []
    for country_code, country, raw_value in rows:
        if country_code not in seen:
            seen.add(country_code)
            result.append((country_code, country, raw_value))

    # Сортируем по названию страны (алфавит)
    result.sort(key=lambda x: x[1])
    return result


# ── Трансформация имён ────────────────────────────────────────────────────────

def transform_config_name(value: str, index: int) -> str:
    """Заменяет отображаемое имя на NekrozVPN-XXXX."""
    stripped = value.strip()
    label    = f"NekrozVPN-{index:04d}"

    if stripped.lower().startswith("vmess://"):
        encoded = stripped[8:]
        try:
            padded  = encoded + "=" * (-len(encoded) % 4)
            data    = json.loads(base64.b64decode(padded).decode("utf-8"))
            data["ps"] = label
            new_enc = base64.b64encode(
                json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
            ).decode()
            return f"vmess://{new_enc}"
        except Exception:
            pass

    if "#" in stripped:
        base = stripped[: stripped.rindex("#")]
    else:
        base = stripped.rstrip()
    return f"{base}#{label}"


def country_config_name(raw_value: str, country_code: str, country: str) -> str:
    """Создаёт конфиг с именем вида '🇳🇱 Auto Netherlands'."""
    flag  = COUNTRY_FLAGS.get(country_code, "🌐")
    label = f"{flag} Auto {country}"

    stripped = raw_value.strip()

    if stripped.lower().startswith("vmess://"):
        encoded = stripped[8:]
        try:
            padded  = encoded + "=" * (-len(encoded) % 4)
            data    = json.loads(base64.b64decode(padded).decode("utf-8"))
            data["ps"] = label
            new_enc = base64.b64encode(
                json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
            ).decode()
            return f"vmess://{new_enc}"
        except Exception:
            pass

    if "#" in stripped:
        base = stripped[: stripped.rindex("#")]
    else:
        base = stripped.rstrip()
    return f"{base}#{label}"


# ── Парсинг и сбор конфигов (без изменений) ───────────────────────────────────

def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    if not path.exists():
        raise FileNotFoundError(f"missing file: {path.name}")
    raw   = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("sources")
    if not isinstance(items, list):
        raise ValueError("'sources' must be a list")
    sources: list[Source] = []
    for item in items:
        source = Source.from_dict(item)
        if source.enabled:
            sources.append(source)
    return sources


def fetch_text(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "NekrozVPN/1.0",
            "Accept":     "text/plain,text/html,application/json,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        data    = response.read(MAX_HTTP_BYTES + 1)
        if len(data) > MAX_HTTP_BYTES:
            data = data[:MAX_HTTP_BYTES]
        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


def normalize_text(text: str) -> str:
    return unescape(text).replace("\r", "")


def is_probable_base64(text: str) -> bool:
    compact = "".join(text.split())
    if len(compact) < 32 or len(compact) % 4 != 0:
        return False
    return bool(BASE64_RE.fullmatch(text))


def try_decode_base64(text: str) -> str | None:
    compact = "".join(text.split())
    if not is_probable_base64(compact):
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        decoded_text = base64.b64decode(padded, validate=False).decode("utf-8", errors="replace")
        if any(p in decoded_text.lower() for p in (
            "vless://", "trojan://", "ss://", "ssr://",
            "vmess://", "hy2://", "hysteria2://", "tuic://",
        )):
            return decoded_text
    except Exception:
        pass
    return None


def detect_protocol(value: str) -> str:
    lowered = value.lower().strip()
    for proto in ("vless", "trojan", "ssr", "ss", "vmess", "hy2", "hysteria2", "tuic"):
        if lowered.startswith(proto + "://"):
            return proto
    return "unknown"


def normalize_config(value: str) -> str:
    return value.strip().strip('"').strip("'").rstrip(",;)").replace("\n", "").replace("\t", "")


def extract_config_values(text: str, depth: int = 0) -> list[str]:
    text  = normalize_text(text)
    found: list[str] = []
    for pattern in CONFIG_PATTERNS:
        for match in pattern.finditer(text):
            found.append(normalize_config(match.group(0)))
    if depth == 0:
        decoded = try_decode_base64(text)
        if decoded:
            found.extend(extract_config_values(decoded, depth=1))
    result: list[str] = []
    seen:   set[str]  = set()
    for item in found:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def parse_plain_text(source: Source, text: str, found_in: str) -> list[ConfigItem]:
    return [
        ConfigItem(
            protocol    = detect_protocol(v),
            value       = v,
            source_name = source.name,
            source_url  = source.url,
            found_in    = found_in,
        )
        for v in extract_config_values(text)
    ]


def is_github_url(url: str) -> bool:
    return urlparse(url).netloc.lower() in {"github.com", "www.github.com"}


def github_owner_repo_from_url(url: str) -> tuple[str, str] | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return (parts[0], parts[1].removesuffix(".git")) if len(parts) >= 2 else None


def github_raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path.lstrip('/')}"


def github_repo_root_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}"


def github_tree_url(owner: str, repo: str, branch: str, path: str = "") -> str:
    if path:
        return f"https://github.com/{owner}/{repo}/tree/{branch}/{path.lstrip('/')}"
    return f"https://github.com/{owner}/{repo}/tree/{branch}"


def extract_github_links(html: str, owner: str, repo: str) -> list[str]:
    prefix  = f"/{owner}/{repo}/"
    deduped: list[str] = []
    seen:    set[str]  = set()
    for href in HREF_RE.findall(html):
        href = unquote(href.split("?", 1)[0].split("#", 1)[0])
        if not href.startswith(prefix):
            continue
        rest = href[len(prefix):]
        if rest.startswith(("blob/", "tree/")) and href not in seen:
            seen.add(href)
            deduped.append(href)
    return deduped


def parse_github_link_path(href: str, owner: str, repo: str) -> tuple[str, str, str] | None:
    prefix = f"/{owner}/{repo}/"
    if not href.startswith(prefix):
        return None
    parts = href[len(prefix):].split("/", 2)
    if len(parts) < 2:
        return None
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ""


def crawl_github_repo(source: Source) -> list[ConfigItem]:
    owner_repo = github_owner_repo_from_url(source.url)
    if owner_repo is None:
        return []
    owner, repo    = owner_repo
    start_url      = source.url.rstrip("/")
    queue:         list[tuple[str, int]] = [(start_url, 0)]
    visited_pages: set[str]             = set()
    visited_files: set[str]             = set()
    collected:     list[ConfigItem]     = []

    while queue and len(visited_pages) < MAX_GITHUB_PAGES and len(visited_files) < MAX_GITHUB_FILES:
        page_url, depth = queue.pop(0)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)
        try:
            html = fetch_text(page_url)
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.warning("skip page %s: %s", page_url, exc)
            continue
        links = extract_github_links(html, owner, repo)
        if not links and page_url == github_repo_root_url(owner, repo):
            collected.extend(parse_plain_text(source, html, page_url))
            continue
        for href in links:
            parsed = parse_github_link_path(href, owner, repo)
            if parsed is None:
                continue
            kind, branch, path = parsed
            if kind == "blob" and path:
                raw_url = github_raw_url(owner, repo, branch, path)
                if raw_url in visited_files:
                    continue
                visited_files.add(raw_url)
                try:
                    collected.extend(parse_plain_text(source, fetch_text(raw_url), raw_url))
                except (HTTPError, URLError, TimeoutError) as exc:
                    logger.warning("skip file %s: %s", raw_url, exc)
            elif kind == "tree" and depth < MAX_TREE_DEPTH:
                tree_url = github_tree_url(owner, repo, branch, path)
                if tree_url not in visited_pages:
                    queue.append((tree_url, depth + 1))
    try:
        collected.extend(parse_plain_text(source, fetch_text(start_url), start_url))
    except Exception:
        pass
    return collected


def dedupe_configs(configs: list[ConfigItem]) -> list[ConfigItem]:
    seen:   set[str]         = set()
    result: list[ConfigItem] = []
    for item in configs:
        key = item.normalized_key()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ── Запись файлов ─────────────────────────────────────────────────────────────

def build_manifest(configs: list[ConfigItem], sources_count: int) -> dict[str, Any]:
    counts = Counter(item.protocol for item in configs)
    return {
        "project":             "NekrozVPN",
        "generated_at":        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources_total":       sources_count,
        "configs_total":       len(configs),
        "configs_by_protocol": dict(sorted(counts.items())),
        "configs": [
            {
                "protocol":    item.protocol,
                "value":       item.value,
                "clean_value": transform_config_name(item.value, i),
                "source_name": item.source_name,
                "source_url":  item.source_url,
                "found_in":    item.found_in,
            }
            for i, item in enumerate(configs, start=1)
        ],
    }


def write_outputs(manifest: dict[str, Any], configs: list[ConfigItem]) -> None:
    """Пишет manifest.json и subscription.txt (с NekrozVPN именами, без гео-группировки)."""
    ensure_output_dir()
    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# NekrozVPN subscription",
        f"# generated_at: {manifest['generated_at']}",
        f"# configs_total: {manifest['configs_total']}",
        "",
    ]
    for i, item in enumerate(configs, start=1):
        lines.append(transform_config_name(item.value, i))
    SUBSCRIPTION_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_grouped_subscription(
    best_configs: list[tuple[str, str, str]],
    generated_at: str,
) -> None:
    """
    Перезаписывает subscription.txt: один конфиг на страну.
    Формат имени: '🇳🇱 Auto Netherlands', '🇩🇪 Auto Germany', ...
    """
    ensure_output_dir()
    lines = [
        "# NekrozVPN subscription — by country",
        f"# generated_at: {generated_at}",
        f"# locations: {len(best_configs)}",
        "",
    ]
    for country_code, country, raw_value in best_configs:
        lines.append(country_config_name(raw_value, country_code, country))

    SUBSCRIPTION_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(
        "Сгруппированная подписка: %d локаций → %s",
        len(best_configs),
        ", ".join(cc for cc, _, _ in best_configs),
    )


# ── Точка входа ───────────────────────────────────────────────────────────────

def build() -> None:
    sources    = load_sources()
    collected: list[ConfigItem] = []

    for source in sources:
        logger.info("processing %s", source.name)
        if is_github_url(source.url):
            items = crawl_github_repo(source)
        else:
            try:
                items = parse_plain_text(source, fetch_text(source.url), source.url)
            except (HTTPError, URLError, TimeoutError) as exc:
                logger.warning("skip %s: %s", source.name, exc)
                continue
        logger.info("found %d candidate(s) in %s", len(items), source.name)
        collected.extend(items)

    unique_configs = dedupe_configs(collected)
    generated_at   = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest       = build_manifest(unique_configs, len(sources))

    # Сначала пишем «сырую» подписку (на случай если БД недоступна)
    write_outputs(manifest, unique_configs)
    logger.info("saved %d unique config(s)", len(unique_configs))

    # Сохраняем в Turso
    conn = _get_db()
    if conn is None:
        return

    init_db(conn)
    save_configs_to_db(conn, unique_configs)

    # Определяем страны для конфигов без гео-данных
    enrich_with_geo(conn)

    # Формируем и записываем сгруппированную подписку (1 конфиг / страна)
    best = get_best_per_country(conn)
    if best:
        write_grouped_subscription(best, generated_at)
    else:
        logger.warning("Гео-данных нет — subscription.txt остаётся без группировки")


def main() -> int:
    try:
        build()
    except Exception as exc:  # noqa: BLE001
        logger.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
