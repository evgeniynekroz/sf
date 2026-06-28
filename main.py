from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
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
GEO_HOST_LIMIT   = 2000   # максимум хостов на геолукап

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
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Source":
        name    = str(data.get("name", "")).strip()
        url     = str(data.get("url", "")).strip()
        enabled = bool(data.get("enabled", True))
        if not name or not url:
            raise ValueError(f"bad source: {data}")
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError(f"bad url: {url}")
        return cls(name=name, url=url, enabled=enabled)


@dataclass(slots=True)
class ConfigItem:
    protocol: str
    value: str
    source_name: str
    source_url: str
    found_in: str

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

def extract_host(value: str) -> str | None:
    s = value.strip()
    if s.lower().startswith("vmess://"):
        try:
            padded = s[8:] + "=" * (-len(s[8:]) % 4)
            return str(json.loads(base64.b64decode(padded).decode()).get("add", "")).strip() or None
        except Exception:
            return None
    if "#" in s:
        s = s[: s.rindex("#")]
    try:
        return urlparse(s).hostname or None
    except Exception:
        return None


def lookup_geo(hosts: list[str]) -> dict[str, tuple[str, str]]:
    """Возвращает {host: (country_code, country)}."""
    result: dict[str, tuple[str, str]] = {}
    for i in range(0, len(hosts), 100):
        batch   = hosts[i : i + 100]
        payload = json.dumps([{"query": h} for h in batch]).encode()
        try:
            req = Request(
                "http://ip-api.com/batch?fields=query,status,country,countryCode",
                data    = payload,
                headers = {"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=15) as resp:
                for item in json.loads(resp.read()):
                    if item.get("status") == "success":
                        result[item["query"]] = (item["countryCode"], item["country"])
        except Exception as exc:
            logger.warning("GeoIP batch %d: %s", i, exc)
        if i + 100 < len(hosts):
            time.sleep(1.5)
    return result


def group_by_country(
    configs: list[ConfigItem],
    geo: dict[str, tuple[str, str]],
) -> list[tuple[str, str, str]]:
    """
    Возвращает [(country_code, country, raw_value), ...] — один конфиг на страну.
    Сортировка по названию страны.
    """
    best: dict[str, tuple[str, str, str]] = {}
    for item in configs:
        host = extract_host(item.value)
        if not host or host not in geo:
            continue
        cc, country = geo[host]
        if cc not in best:
            best[cc] = (cc, country, item.value)
    return sorted(best.values(), key=lambda x: x[1])


# ── Трансформация имён ────────────────────────────────────────────────────────

def country_label(raw: str, cc: str, country: str) -> str:
    flag  = COUNTRY_FLAGS.get(cc, "🌐")
    label = f"{flag} Auto {country}"
    s     = raw.strip()
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


# ── Генерация подписки ────────────────────────────────────────────────────────

def build_subscription(winners: list[tuple[str, str, str]], generated_at: str) -> str:
    lines = [
        "# NekrozVPN subscription",
        f"# generated_at: {generated_at}",
        f"# locations: {len(winners)}",
        "",
    ]
    for cc, country, raw in winners:
        lines.append(country_label(raw, cc, country))
    return "\n".join(lines) + "\n"


def save_subscription_to_turso(content: str) -> None:
    turso_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('subscription', ?)",
        [content],
    )
    logger.info("Подписка сохранена в Turso")


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
    return [ConfigItem(detect_protocol(v), v, source.name, source.url, found_in)
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
    seen, out = set(), []
    for c in configs:
        k = c.key()
        if k not in seen:
            seen.add(k); out.append(c)
    return out


# ── Точка входа ───────────────────────────────────────────────────────────────

def build() -> None:
    sources = load_sources()
    all_configs: list[ConfigItem] = []

    for src in sources:
        logger.info("processing %s", src.name)
        items = crawl_github(src) if is_github(src.url) else []
        if not is_github(src.url):
            try: items = parse_text(src, fetch_text(src.url), src.url)
            except Exception as e: logger.warning("skip %s: %s", src.name, e); continue
        logger.info("found %d in %s", len(items), src.name)
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

    # Группируем по стране, один победитель на страну
    winners = group_by_country(unique, geo)
    logger.info("локаций: %d → %s", len(winners), ", ".join(cc for cc,_,_ in winners))

    # Записываем файлы
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    sub_content  = build_subscription(winners, generated_at)
    ensure_output_dir()
    SUBSCRIPTION_FILE.write_text(sub_content, encoding="utf-8")
    logger.info("subscription.txt записан")

    # Сохраняем в Turso (два запроса: init + insert)
    init_db()
    save_subscription_to_turso(sub_content)


def main() -> int:
    try:
        build()
    except Exception as exc:
        logger.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
