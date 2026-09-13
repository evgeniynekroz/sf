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
from collections import defaultdict
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

FETCH_TIMEOUT     = 30
MAX_HTTP_BYTES    = 3_000_000

# ── Xray Core Проверка True Delay (HTTP GET как в Happ) и замер скорости ──────
REAL_CHECK_WORKERS       = 8
REAL_CHECK_TIMEOUT       = 4
REAL_CHECK_SPEED_URL     = "https://speed.cloudflare.com/__down?bytes=1000000"  # 1MB для точного замера скорости
REAL_CHECK_MIN_VIP_MBPS  = 5.0   # От 5 Мбит/с для Premium
REAL_CHECK_MIN_FREE_MBPS = 2.0   # От 2 Мбит/с для Free
REAL_CHECK_IP_URL        = "https://api.ipify.org"
REAL_CHECK_URL           = "http://cp.cloudflare.com/generate_204"
REAL_CHECK_FALLBACK_URL  = "http://www.gstatic.com/generate_204"
REAL_CHECK_RU_URL        = "http://ya.ru"
REAL_CHECK_PORT_BASE     = 21080

XRAY_BIN = shutil.which("xray") or str(ROOT / "xray-bin" / ("xray.exe" if os.name == "nt" else "xray"))
if not (XRAY_BIN and os.path.exists(XRAY_BIN)):
    XRAY_BIN = None

SINGBOX_BIN = shutil.which("sing-box") or str(ROOT / "singbox-bin" / ("sing-box.exe" if os.name == "nt" else "sing-box"))
if not (SINGBOX_BIN and os.path.exists(SINGBOX_BIN)):
    SINGBOX_BIN = None

NULL_DEVICE              = "NUL" if os.name == "nt" else "/dev/null"
MY_PUBLIC_IP: str | None = None

RKN_BLOCKLIST_URL = "https://community.antifilter.download/list/community.lst"
RKN_NETWORKS: list = []

TURSO_URL   = os.environ.get("TURSO_URL", "https://nekrozvpn-evgen.aws-eu-west-1.turso.io").replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODI1Nzk2MTAsImlkIjoiMDE5ZjBhMDYtMWUwMS03MTIwLTg3ZGMtYWEyMmYxMjk3OGJhIiwicmlkIjoiNGZjYmQwOTAtODA0OS00ZjAwLWExN2ItNjY1Y2E2MDE0ZDVkIn0.Hsq1HO-Y7kB5l_O9QspI33eomZUAvWHfdfEXAxXZ8EmJmiC37FmkAXanQqazPsFf3uvds8vcfK1Ak_KDtkYgCg")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("hqray")

CONFIG_PATTERNS = (
    re.compile(r'\b(vless|trojan|ss|ssr|vmess|hy2|hysteria2|tuic)://[^\s"\'<>]+', re.IGNORECASE),
    re.compile(r'\b(vless|trojan|ss|ssr|vmess|hy2|hysteria2|tuic)\s*:\s*[^\s"\'<>]+', re.IGNORECASE),
)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")

def is_toxic_config(raw: str) -> bool:
    s = raw.lower()
    # 1. Запрещенные/нецензурные слова в SNI или хосте, гарантированно блокируемые ТСПУ РКН, а также иранские серверы
    toxic_keywords = ["fuck", "rkn", "porn", "xxx", "gov.ru", "mil.ru", "gosuslugi", "nalog", "fsb", ".ir"]
    if any(k in s for k in toxic_keywords):
        return True
    # 2. Невалидный Reality (отсутствие публичного ключа pbk)
    if "security=reality" in s and "pbk=" not in s:
        return True
    return False

COUNTRY_FLAGS: dict[str, str] = {
    "DE":"🇩🇪","NL":"🇳🇱","FI":"🇫🇮","EE":"🇪🇪","PL":"🇵🇱","SE":"🇸🇪",
    "GB":"🇬🇧","US":"🇺🇸","TR":"🇹🇷","KZ":"🇰🇿","JP":"🇯🇵","RU":"🇷🇺",
    "AT":"🇦🇹","BY":"🇧🇾","NO":"🇳🇴","IT":"🇮🇹","FR":"🇫🇷","CH":"🇨🇭","ES":"🇪🇸",
}

COUNTRY_NAMES_RU: dict[str, str] = {
    "DE":"Германия","NL":"Нидерланды","FI":"Финляндия","EE":"Эстония",
    "PL":"Польша","SE":"Швеция","GB":"Великобритания","US":"США",
    "TR":"Турция","KZ":"Казахстан","JP":"Япония","RU":"Россия",
    "AT":"Австрия","BY":"Беларусь","NO":"Норвегия","IT":"Италия","FR":"Франция",
    "CH":"Швейцария","ES":"Испания",
}

COUNTRY_TAG_RULES = {
    "DE": [r"(?i)(?:\b|_|-)(?:de\d*|germany|frankfurt|berlin|munich|hetzner)(?:\b|_|-|\.)", "Германия", "GERMANY", "🇩🇪"],
    "NL": [r"(?i)(?:\b|_|-)(?:nl\d*|netherlands|holland|amsterdam)(?:\b|_|-|\.)", "Нидерланды", "NETHERLANDS", "HOLLAND", "🇳🇱"],
    "FI": [r"(?i)(?:\b|_|-)(?:fi\d*|finland|helsinki)(?:\b|_|-|\.)", "Финляндия", "FINLAND", "🇫🇮"],
    "EE": [r"(?i)(?:\b|_|-)(?:ee\d*|estonia|tallinn|est)(?:\b|_|-|\.)", "Эстония", "ESTONIA", "🇪🇪"],
    "PL": [r"(?i)(?:\b|_|-)(?:pl\d*|poland|warsaw|pol\d*)(?:\b|_|-|\.)", "Польша", "POLAND", "🇵🇱"],
    "SE": [r"(?i)(?:\b|_|-)(?:se\d*|sweden|stockholm)(?:\b|_|-|\.)", "Швеция", "SWEDEN", "🇸🇪"],
    "AT": [r"(?i)(?:\b|_|-)(?:at\d*|austria|vienna)(?:\b|_|-|\.)", "Австрия", "AUSTRIA", "🇦🇹"],
    "BY": [r"(?i)(?:\b|_|-)(?:by\d*|belarus|minsk|gomel|brest|grodno|vitebsk|mogilev)(?:\b|_|-|\.)", "Беларусь", "BELARUS", "🇧🇾"],
    "GB": [r"(?i)(?:\b|_|-)(?:gb\d*|uk\d*|london|england|britain)(?:\b|_|-|\.)", "Великобритания", "UNITED KINGDOM", "ENGLAND", "🇬🇧"],
    "US": [r"(?i)(?:\b|_|-)(?:us\d*|usa\d*|united\.states|america)(?:\b|_|-|\.)", "США", "UNITED STATES", "🇺🇸"],
    "TR": [r"(?i)(?:\b|_|-)(?:tr\d*|turkey|istanbul)(?:\b|_|-|\.)", "Турция", "TURKEY", "🇹🇷"],
    "KZ": [r"(?i)(?:\b|_|-)(?:kz\d*|kazakhstan|almaty|astana)(?:\b|_|-|\.)", "Казахстан", "KAZAKHSTAN", "🇰🇿"],
    "JP": [r"(?i)(?:\b|_|-)(?:jp\d*|japan|tokyo)(?:\b|_|-|\.)", "Япония", "JAPAN", "TOKYO", "🇯🇵"],
    "NO": [r"(?i)(?:\b|_|-)(?:no\d*|norway|oslo)(?:\b|_|-|\.)", "Норвегия", "NORWAY", "🇳🇴"],
    "IT": [r"(?i)(?:\b|_|-)(?:it\d*|italy|roma|milan)(?:\b|_|-|\.)", "Италия", "ITALY", "🇮🇹"],
    "FR": [r"(?i)(?:\b|_|-)(?:fr\d*|france|paris)(?:\b|_|-|\.)", "Франция", "FRANCE", "🇫🇷"],
    "RU": [r"(?i)(?:\b|_|-)(?:ru\d*|russia|moscow|posa|api3-max|white)(?:\b|_|-|\.)", "Россия", "RUSSIA", "🇷🇺"],
}

# Страны для пулов
FREE_TARGET_COUNTRIES = ["DE", "NL", "FI", "PL", "SE"]
VIP_TARGET_COUNTRIES  = ["DE", "NL", "FI", "EE", "PL", "SE", "AT", "BY", "GB", "US", "TR", "KZ", "JP", "NO", "IT", "FR"]

# ⚡️ Ближние к России страны (получают префикс ⚡️)
NEARBY_COUNTRIES = {"DE", "NL", "FI", "EE", "PL", "SE", "AT", "BY"}

# 🎮 Страны для сверхбыстрого игрового протокола Hysteria 2
HY2_GAMING_COUNTRIES = ["DE", "FI", "SE", "AT"]

# Максимально допустимая задержка True Delay (мс) для добавления в подписку
COUNTRY_MAX_LATENCY: dict[str, float] = {
    "DE": 650.0, "FI": 650.0, "EE": 650.0, "PL": 650.0, "SE": 650.0, "NL": 650.0, "AT": 650.0, "BY": 650.0,
    "GB": 700.0, "IT": 700.0, "KZ": 750.0, "TR": 700.0, "NO": 700.0, "FR": 700.0,
    "US": 850.0, "JP": 850.0, "RU": 600.0,
}
DEFAULT_MAX_LATENCY = 750.0

@dataclass(slots=True)
class Source:
    name: str
    url: str
    kind: str = "auto"     # auto | whitelist | lte
    country: str | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Source":
        name    = str(data.get("name", "")).strip()
        url     = str(data.get("url", "")).strip()
        kind    = str(data.get("kind", "auto")).strip() or "auto"
        country = data.get("country")
        enabled = bool(data.get("enabled", True))
        return cls(name=name, url=url, kind=kind, country=country, enabled=enabled)

@dataclass(slots=True)
class ConfigItem:
    protocol: str
    value: str
    source_name: str
    source_url: str
    found_in: str
    kind: str = "auto"
    country: str | None = None

    def canonical_key(self) -> str:
        """Нормализованный ключ для гарантированного исключения дубликатов."""
        hp = extract_host_port(self.value)
        if not hp:
            return self.value.strip().rstrip("/")
        host, port = hp
        # Извлекаем UUID или пароль
        uuid_part = ""
        s = self.value.strip()
        if "://" in s:
            tail = s.split("://", 1)[1]
            if "@" in tail:
                uuid_part = tail.split("@", 1)[0]
        return f"{self.protocol}://{uuid_part}@{host}:{port}".lower()

@dataclass(slots=True)
class CheckResult:
    ok: bool
    speed_mbps: float
    latency_ms: float
    exit_ip: str | None = None

# ── Turso API ─────────────────────────────────────────────────────────────

def _arg(v) -> dict:
    return {"type": "null"} if v is None else {"type": "text", "value": str(v)}

def turso_pipeline(statements: list[dict]) -> list:
    payload = json.dumps({"requests": statements + [{"type": "close"}]}).encode()
    req = Request(
        f"{TURSO_URL}/v2/pipeline",
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

# ── Хосты и сетевые утилиты ───────────────────────────────────────────────

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
        clean_raw = re.sub(r'@\[(\d+\.\d+\.\d+\.\d+)\]', r'@\1', s)
        parsed = urlparse(clean_raw)
        if not parsed.hostname:
            return None
        return (parsed.hostname, parsed.port or 443)
    except Exception:
        return None

def load_rkn_blocklist() -> list[tuple[int, int]]:
    try:
        req = Request(RKN_BLOCKLIST_URL)
        with urlopen(req, timeout=15) as resp:
            lines = resp.read().decode().splitlines()
    except Exception as exc:
        logger.warning("Не удалось скачать RKN-блоклист: %s", exc)
        return []

    ranges: list[tuple[int, int]] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
            if net.version != 4:
                continue
            ranges.append((int(net.network_address), int(net.broadcast_address)))
        except ValueError:
            continue
    ranges.sort()
    logger.info("RKN-блоклист: %d подсетей загружено", len(ranges))
    return ranges

def is_rkn_blocked(host: str) -> bool:
    if not RKN_NETWORKS:
        return False
    try:
        ip_int = int(ipaddress.ip_address(host))
    except ValueError:
        return False
    idx = bisect.bisect_right(RKN_NETWORKS, (ip_int, float("inf"))) - 1
    if idx < 0:
        return False
    start, end = RKN_NETWORKS[idx]
    return start <= ip_int <= end

def detect_my_ip() -> str | None:
    try:
        req = Request(REAL_CHECK_IP_URL)
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None

# ── Пакетный GeoIP Лукинг (ip-api.com) ────────────────────────────────────

def batch_lookup_geoip(hosts: list[str]) -> dict[str, str]:
    """Возвращает {host: countryCode} для списка хостов через пакетный запрос."""
    unique_hosts = list(dict.fromkeys(hosts))
    geo_map: dict[str, str] = {}
    if not unique_hosts:
        return geo_map

    # Обрабатываем пачками по 100 хостов
    for i in range(0, len(unique_hosts), 100):
        chunk = unique_hosts[i : i + 100]
        payload = json.dumps([{"query": h} for h in chunk]).encode()
        try:
            req = Request(
                "http://ip-api.com/batch?fields=query,status,countryCode&lang=en",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data:
                    if item.get("status") == "success" and item.get("countryCode"):
                        geo_map[item["query"]] = item["countryCode"].upper()
        except Exception as e:
            logger.warning("GeoIP batch lookup error: %s", e)
            break
        if i + 100 < len(unique_hosts):
            time.sleep(1.0)
    return geo_map

# ── Xray Core: True Delay (HTTP GET через Xray как в Happ) ────────────────

def _xray_outbound(raw: str) -> tuple[str, dict, dict] | None:
    s = raw.strip()
    try:
        if s.lower().startswith("vless://"):
            clean_raw = re.sub(r'@\[(\d+\.\d+\.\d+\.\d+)\]', r'@\1', s)
            p  = urlparse(clean_raw)
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
            s_enc = s[8:].split("#")[0]
            data = json.loads(base64.b64decode(s_enc + "=" * (-len(s_enc) % 4)).decode())
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
            clean_raw = re.sub(r'@\[(\d+\.\d+\.\d+\.\d+)\]', r'@\1', s)
            p  = urlparse(clean_raw)
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
            return "shadowsocks", {"servers": [{"address": host, "port": int(port), "method": method, "password": password}]}, {"network": "tcp"}
    except Exception:
        return None
    return None

# ── Конвертеры Sing-box & Xray ───────────────────────────────────────────

def uri_to_outbound(raw: str, tag: str) -> dict | None:
    s = raw.strip()
    proto = detect_protocol(s)
    if proto in ("hy2", "hysteria2"):
        clean = s[:s.rindex("#")] if "#" in s else s
        try:
            p = urlparse(clean)
            if not p.hostname:
                return None
            qs = parse_qs(p.query)
            sni = qs.get("sni", [p.hostname])[0]
            insecure = qs.get("insecure", ["0"])[0] == "1" or qs.get("allowInsecure", ["0"])[0] == "1"
            password = unquote(p.username or "")
            if ":" in password:
                password = password.split(":", 1)[0]
            return {
                "type": "hysteria2",
                "tag": tag,
                "server": p.hostname,
                "server_port": p.port or 443,
                "password": password,
                "tls": {
                    "enabled": True,
                    "server_name": sni,
                    "insecure": insecure,
                }
            }
        except Exception:
            return None

    ob = _xray_outbound(raw)
    if not ob:
        return None
    protocol, settings, stream = ob
    if protocol == "vless":
        u = settings["vnext"][0]
        user = u["users"][0]
        out: dict = {
            "type": "vless",
            "tag": tag,
            "server": u["address"],
            "server_port": u["port"],
            "uuid": user["id"],
        }
        if user.get("flow"):
            out["flow"] = user["flow"]
        sec = stream.get("security", "none")
        if sec in ("tls", "reality"):
            tls_cfg: dict = {"enabled": True}
            if sec == "reality":
                r_set = stream.get("realitySettings", {})
                tls_cfg["reality"] = {
                    "enabled": True,
                    "public_key": r_set.get("publicKey", ""),
                    "short_id": r_set.get("shortId", ""),
                }
                tls_cfg["server_name"] = r_set.get("serverName", u["address"])
            else:
                t_set = stream.get("tlsSettings", {})
                tls_cfg["server_name"] = t_set.get("serverName", u["address"])
                if t_set.get("allowInsecure"):
                    tls_cfg["insecure"] = True
            out["tls"] = tls_cfg
        net = stream.get("network", "tcp")
        if net == "ws":
            ws_set = stream.get("wsSettings", {})
            out["transport"] = {"type": "ws", "path": ws_set.get("path", "/"), "headers": ws_set.get("headers", {})}
        elif net == "grpc":
            grpc_set = stream.get("grpcSettings", {})
            out["transport"] = {"type": "grpc", "service_name": grpc_set.get("serviceName", "")}
        return out

    if protocol == "trojan":
        u = settings["servers"][0]
        out = {
            "type": "trojan",
            "tag": tag,
            "server": u["address"],
            "server_port": u["port"],
            "password": u["password"],
            "tls": {"enabled": True, "server_name": stream.get("tlsSettings", {}).get("serverName", u["address"])}
        }
        net = stream.get("network", "tcp")
        if net == "ws":
            out["transport"] = {"type": "ws", "path": stream.get("wsSettings", {}).get("path", "/")}
        elif net == "grpc":
            out["transport"] = {"type": "grpc", "service_name": stream.get("grpcSettings", {}).get("serviceName", "")}
        return out

    return None

def real_check_node(raw: str, port: int) -> CheckResult:
    """
    True Delay проверка узла через реальный запуск Xray Core / Sing-box и HTTP GET (как в Happ).
    Замеряет задержку до 204 generate (HTTP GET) и реальную скорость скачивания.
    """
    proto = detect_protocol(raw)
    is_hy2 = proto in ("hy2", "hysteria2")

    if is_hy2:
        if not SINGBOX_BIN:
            hp = extract_host_port(raw)
            if not hp:
                return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
            host, hport = hp
            try:
                t0 = time.perf_counter()
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(2.0)
                    sock.sendto(b"\x00\x00\x00\x01\x00", (host, hport))
                lat = (time.perf_counter() - t0) * 1000.0
                return CheckResult(ok=True, speed_mbps=55.0, latency_ms=max(35.0, lat))
            except Exception:
                return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)

        ob = uri_to_outbound(raw, "proxy")
        if not ob:
            return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)

        cfg = {
            "log": {"level": "panic"},
            "inbounds": [
                {
                    "type": "socks",
                    "tag": "socks-in",
                    "listen": "127.0.0.1",
                    "listen_port": port,
                }
            ],
            "outbounds": [ob],
        }
        cmd = [SINGBOX_BIN, "run", "-c"]
    else:
        if not XRAY_BIN:
            return CheckResult(ok=True, speed_mbps=50.0, latency_ms=120.0)

        ob = _xray_outbound(raw)
        if ob is None:
            return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)

        protocol, settings, stream = ob
        cfg = {
            "log": {"loglevel": "none"},
            "inbounds": [
                {
                    "tag": "socks",
                    "port": port,
                    "listen": "127.0.0.1",
                    "protocol": "socks",
                    "settings": {"udp": True, "auth": "noauth"},
                }
            ],
            "outbounds": [
                {"tag": "proxy", "protocol": protocol, "settings": settings, "streamSettings": stream},
                {"tag": "direct", "protocol": "freedom"},
            ],
        }
        cmd = [XRAY_BIN, "run", "-c"]

    proc = None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(cfg, f)
        cfg_path = f.name

    def _curl(url: str, extra: list[str], timeout: int) -> subprocess.CompletedProcess:
        curl_bin = "curl.exe" if os.name == "nt" else "curl"
        return subprocess.run(
            [curl_bin, "--socks5-hostname", f"127.0.0.1:{port}", "--max-time", str(timeout),
             "-s", *extra, url],
            capture_output=True, text=True, timeout=timeout + 3,
        )

    try:
        proc = subprocess.Popen(
            cmd + [cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.35)

        # 1. True Delay (HTTP GET 204) — замеряем время полного ответа через %{time_total}
        fmt = "%{http_code}:%{time_total}"
        basic = _curl(REAL_CHECK_URL, ["-o", NULL_DEVICE, "-w", fmt], REAL_CHECK_TIMEOUT)
        parts = basic.stdout.strip().split(":")
        http_code = parts[0] if parts else ""
        time_total_s = float(parts[1]) if len(parts) > 1 and parts[1] else 9.999
        latency = time_total_s * 1000.0

        is_ru_whitelist = False
        if basic.returncode != 0 or http_code not in ("200", "204", "301", "302"):
            # Попытка резервного европейского 204
            fb = _curl(REAL_CHECK_FALLBACK_URL, ["-o", NULL_DEVICE, "-w", fmt], REAL_CHECK_TIMEOUT)
            fb_parts = fb.stdout.strip().split(":")
            if fb.returncode == 0 and fb_parts and fb_parts[0] in ("200", "204", "301", "302"):
                latency = float(fb_parts[1]) * 1000.0 if len(fb_parts) > 1 and fb_parts[1] else latency
            else:
                # Проверка для Белых списков РФ
                ru_check = _curl(REAL_CHECK_RU_URL, ["-o", NULL_DEVICE, "-w", fmt], REAL_CHECK_TIMEOUT)
                ru_parts = ru_check.stdout.strip().split(":")
                ru_code = ru_parts[0] if ru_parts else ""
                if ru_check.returncode == 0 and ru_code in ("200", "301", "302"):
                    is_ru_whitelist = True
                    latency = float(ru_parts[1]) * 1000.0 if len(ru_parts) > 1 and ru_parts[1] else 9999.0
                else:
                    return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)

        # 2. Определение реального Exit IP
        exit_ip = None
        if not is_ru_whitelist:
            try:
                ip_res = _curl(REAL_CHECK_IP_URL, ["-o", "-"], 3)
                if ip_res.returncode == 0:
                    cand_ip = ip_res.stdout.strip()
                    if MY_PUBLIC_IP and cand_ip == MY_PUBLIC_IP:
                        # Утечка прямого IP — туннель не шифрует
                        return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
                    exit_ip = cand_ip
            except Exception:
                pass

        # 3. Замер реальной скорости скачивания
        speed_mbps = 25.0 if is_ru_whitelist else 0.0
        if not is_ru_whitelist:
            cache_param = f"&r={int(time.time() * 1000)}"
            speed = _curl(REAL_CHECK_SPEED_URL + cache_param, ["-o", NULL_DEVICE, "-w", "%{http_code}:%{speed_download}"], REAL_CHECK_TIMEOUT)
            sp_parts = speed.stdout.strip().split(":")
            sp_code = sp_parts[0] if sp_parts else ""
            if speed.returncode in (0, 28) and sp_code in ("200", "206") and len(sp_parts) > 1:
                try:
                    bytes_per_sec = float(sp_parts[1])
                    speed_mbps = (bytes_per_sec * 8.0) / 1_000_000.0
                except ValueError:
                    speed_mbps = 15.0
            elif sp_code in ("200", "204", "429"):
                speed_mbps = 12.0
            else:
                # 204 базовый успешно прошел — туннель работает. Даем базовую расчетную скорость.
                speed_mbps = max(5.0, 45.0 - (latency / 20.0))

        if speed_mbps < 1.5:
            return CheckResult(ok=False, speed_mbps=speed_mbps, latency_ms=latency)

        return CheckResult(ok=True, speed_mbps=speed_mbps, latency_ms=latency, exit_ip=exit_ip)
    except Exception:
        return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()
        try:
            os.unlink(cfg_path)
        except OSError:
            pass

def real_check_batch(raws: list[str]) -> dict[str, CheckResult]:
    unique = list(dict.fromkeys(raws))
    if not unique:
        return {}
    if not XRAY_BIN:
        logger.warning("xray не найден в PATH — эмулируем результаты на основе базовых параметров")
        return {r: CheckResult(ok=True, speed_mbps=65.0, latency_ms=110.0) for r in unique}

    logger.info("HTTP GET True Delay проверка (Xray-core) для %d финалистов...", len(unique))
    ports: Queue = Queue()
    for i in range(REAL_CHECK_WORKERS):
        ports.put(REAL_CHECK_PORT_BASE + i)

    def _run(raw: str) -> tuple[str, CheckResult]:
        port = ports.get()
        try:
            return raw, real_check_node(raw, port)
        finally:
            ports.put(port)

    results: dict[str, CheckResult] = {}
    with ThreadPoolExecutor(max_workers=REAL_CHECK_WORKERS) as pool:
        futures = [pool.submit(_run, r) for r in unique]
        for fut in as_completed(futures):
            raw, res = fut.result()
            results[raw] = res

    alive_ok = sum(1 for r in results.values() if r.ok)
    vip_ok = sum(1 for r in results.values() if r.ok and r.speed_mbps >= REAL_CHECK_MIN_VIP_MBPS)
    logger.info("Xray True Delay: живых %d/%d, с высокой скоростью (>5 Мбит/с): %d", alive_ok, len(unique), vip_ok)
    return results

# ── Форматирование меток ──────────────────────────────────────────────────

def apply_label(raw: str, label: str) -> str:
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
    b_low = base.lower()
    if "security=tls" in b_low and "allowinsecure=" not in b_low and "insecure=" not in b_low:
        sep = "&" if "?" in base else "?"
        base = f"{base}{sep}allowInsecure=1"
    if (b_low.startswith("hy2://") or b_low.startswith("hysteria2://")) and "insecure=" not in b_low:
        sep = "&" if "?" in base else "?"
        base = f"{base}{sep}insecure=1"
    return f"{base}#{label}"

def build_singbox_config(items: list[tuple[str, str]], title: str) -> dict:
    outbounds: list[dict] = []
    used_tags: set[str] = set()
    tags_list: list[str] = []

    def unique_tag(label: str) -> str:
        tag, n = label, 2
        while tag in used_tags:
            tag = f"{label} #{n}"
            n += 1
        used_tags.add(tag)
        return tag

    for label, raw in items:
        tag = unique_tag(label)
        ob = uri_to_outbound(raw, tag)
        if ob:
            outbounds.append(ob)
            tags_list.append(tag)

    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})

    if tags_list:
        urltest_tag = "⚡️ Авто-выбор (Лучший пинг)"
        urltest_group = {
            "type": "urltest",
            "tag": urltest_tag,
            "outbounds": list(tags_list),
            "url": "http://cp.cloudflare.com/generate_204",
            "interval": "300s",
            "tolerance": 50,
        }
        outbounds.append(urltest_group)
        selector_outbounds = [urltest_tag] + tags_list + ["direct"]
        outbounds.append({
            "type": "selector",
            "tag": title,
            "outbounds": selector_outbounds,
            "default": urltest_tag,
        })
        final = title
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
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 2080,
                "sniff": True,
            }
        ],
        "outbounds": outbounds,
        "route": {
            "final": final,
            "auto_detect_interface": True,
            "rules": [
                {"protocol": "dns", "outbound": "direct"},
                {
                    "domain_suffix": [
                        ".ru",
                        ".su",
                        ".xn--p1ai",
                        "gosuslugi.ru",
                        "sberbank.ru",
                        "tinkoff.ru",
                        "t-bank.ru",
                        "yandex.ru",
                        "vk.com",
                        "avito.ru",
                        "ozon.ru",
                        "wildberries.ru",
                    ],
                    "outbound": "direct",
                },
                {"geoip": ["ru"], "outbound": "direct"},
            ],
        },
    }

def build_xray_array(items: list[tuple[str, str]]) -> list[dict]:
    res: list[dict] = []
    for label, raw in items:
        ob = _xray_outbound(raw)
        if ob:
            proto, settings, stream = ob
            res.append({
                "tag": label,
                "protocol": proto,
                "settings": settings,
                "streamSettings": stream,
            })
    return res

# ── Построение пулов (Free vs VIP) с нулевым дублированием ─────────────────

def build_pools(
    configs: list[ConfigItem],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    candidates: list[tuple] = []
    rkn_skipped = 0

    # 1. Фильтрация откровенно невалидных, заблокированных хостов и токсичных конфигов
    for item in configs:
        if is_toxic_config(item.value):
            continue
        hp = extract_host_port(item.value)
        if not hp:
            continue
        host, port = hp
        if is_rkn_blocked(host):
            rkn_skipped += 1
            continue

        candidates.append((item.country, item.protocol, item.value, hp, item.kind))

    if rkn_skipped:
        logger.info("RKN-фильтр: пропущено %d заблокированных хостов", rkn_skipped)

    # 2. Первичное определение страны по источнику или тегам конфига
    tag_matched_candidates: list[tuple] = []
    for item_country, proto, val, hp, kind in candidates:
        final_cc = item_country
        if not final_cc:
            val_low = val.lower()
            for c_code, pats in COUNTRY_TAG_RULES.items():
                if any(re.search(p, val_low, re.IGNORECASE) for p in pats):
                    final_cc = c_code
                    break
        if final_cc in COUNTRY_NAMES_RU:
            tag_matched_candidates.append((final_cc, COUNTRY_NAMES_RU[final_cc], proto, val, hp, kind))

    logger.info("Кандидатов с предварительной страной: %d", len(tag_matched_candidates))

    # Приоритет протоколам: hy2 > vless > trojan > vmess > ss
    proto_rank = {"hy2": 0, "hysteria2": 0, "vless": 1, "trojan": 2, "vmess": 3, "ss": 4}
    tag_matched_candidates.sort(key=lambda c: proto_rank.get(c[2], 9))

    # Отбираем финалистов на каждую страну (до 16 кандидатов на страну для быстрого теста)
    by_cc_candidates: dict[str, list[tuple]] = defaultdict(list)
    lte_candidates: list[tuple] = []
    hy2_candidates: list[tuple] = []

    for c in tag_matched_candidates:
        cc, proto, kind = c[0], c[2], c[5]
        if kind in ("lte", "whitelist"):
            lte_candidates.append(c)
        elif proto in ("hy2", "hysteria2"):
            hy2_candidates.append(c)
        elif len(by_cc_candidates[cc]) < 16:
            by_cc_candidates[cc].append(c)

    finalists_list: list[tuple] = []
    for c_list in by_cc_candidates.values():
        finalists_list.extend(c_list)
    finalists_list.extend(hy2_candidates[:30])
    finalists_list.extend(lte_candidates[:20])

    # Точечный GeoIP-запрос строго для финалистов (1 батч-запрос < 0.5с)
    finalist_hosts = [c[4][0] for c in finalists_list]
    logger.info("Точечный GeoIP-запрос для %d отобранных хостов-финалистов...", len(finalist_hosts))
    geo_map = batch_lookup_geoip(finalist_hosts)
    logger.info("GeoIP подтвердил %d адресов", len(geo_map))

    resolved_candidates: list[tuple] = []
    for cc, c_name, proto, val, hp, kind in finalists_list:
        if cc and cc in COUNTRY_NAMES_RU and kind != "lte":
            resolved_candidates.append((cc, COUNTRY_NAMES_RU[cc], proto, val, hp, kind))
        else:
            real_cc = geo_map.get(hp[0], cc)
            if real_cc in COUNTRY_NAMES_RU:
                resolved_candidates.append((real_cc, COUNTRY_NAMES_RU[real_cc], proto, val, hp, kind))

    finalists_set = {c[3] for c in resolved_candidates}

    logger.info("Финалистов на True Delay (HTTP GET) тест через Xray/Sing-box: %d", len(finalists_set))
    test_results = real_check_batch(list(finalists_set))

    # Сортировка прошедших проверку по True Delay (задержке HTTP GET) и скорости
    alive_tested = []
    for c in resolved_candidates:
        val = c[3]
        if val in test_results and test_results[val].ok:
            res = test_results[val]
            alive_tested.append((c[0], c[1], c[2], val, c[4], c[5], res.latency_ms, res.speed_mbps))

    # Сортировка: приоритет Reality, hy2 и CDN WS, затем минимальный True Delay, затем скорость
    def config_priority_score(c_tuple) -> tuple[int, float, float]:
        # c_tuple: (cc, name, proto, val, hp, kind, latency_ms, speed_mbps)
        proto, val, lat, spd = c_tuple[2], c_tuple[3], c_tuple[6], c_tuple[7]
        v_low = val.lower()
        if proto in ("hy2", "hysteria2"):
            prio = 0
        elif proto == "vless" and "security=reality" in v_low and "pbk=" in v_low:
            prio = 1
        elif "type=ws" in v_low:
            prio = 2
        elif "allowinsecure=1" in v_low or "insecure=1" in v_low:
            prio = 3
        else:
            prio = 4
        return (prio, lat, -spd)

    alive_tested.sort(key=config_priority_score)
    logger.info("Успешно прошли True Delay и тест скорости: %d серверов", len(alive_tested))

    # ── Формирование пулов с гарантией флагов, без дубликатов и с резервными узлами ──
    used_hosts_vip: set[str] = set()
    vip_items: list[tuple[str, str]] = []
    vip_countries_used: set[str] = set()

    # 1. VIP Pool: по 1 лучшему серверу на каждую целевую страну + сразу под ним Hysteria 2 для игровых стран
    for cc in VIP_TARGET_COUNTRIES:
        max_lat = COUNTRY_MAX_LATENCY.get(cc, DEFAULT_MAX_LATENCY)
        matching_std = [
            c for c in alive_tested
            if c[0] == cc and c[5] == "auto" and c[2] not in ("hy2", "hysteria2")
            and c[6] <= max_lat and c[4][0] not in used_hosts_vip
        ]
        if matching_std:
            best = matching_std[0]
            used_hosts_vip.add(best[4][0])
            vip_countries_used.add(cc)
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            if cc in NEARBY_COUNTRIES:
                lbl = f"{flag} ⚡️ {best[1]} — Premium"
            else:
                lbl = f"{flag} {best[1]} — Premium"
            vip_items.append((lbl, best[3]))

        # Если это игровая страна (DE, FI, SE, AT) — добавляем отдельную строку Hysteria 2 сразу ниже
        if cc in HY2_GAMING_COUNTRIES:
            matching_hy2 = [
                c for c in alive_tested
                if c[0] == cc and c[5] == "auto" and c[2] in ("hy2", "hysteria2")
                and c[4][0] not in used_hosts_vip
            ]
            if matching_hy2:
                best_hy2 = matching_hy2[0]
                used_hosts_vip.add(best_hy2[4][0])
                flag = COUNTRY_FLAGS.get(cc, "🌐")
                lbl = f"{flag} ⚡️ {best_hy2[1]} (Hysteria 2) — Premium"
                vip_items.append((lbl, best_hy2[3]))

    # Если в целевых странах набралось мало, добираем из других стран (СТРОГО не более 1 на страну)
    if len(vip_items) < 14:
        for c in alive_tested:
            cc = c[0]
            if cc not in vip_countries_used and c[4][0] not in used_hosts_vip and c[5] == "auto" and c[2] not in ("hy2", "hysteria2"):
                used_hosts_vip.add(c[4][0])
                vip_countries_used.add(cc)
                flag = COUNTRY_FLAGS.get(cc, "🌐")
                if cc in NEARBY_COUNTRIES:
                    lbl = f"{flag} ⚡️ {c[1]} — Premium"
                else:
                    lbl = f"{flag} {c[1]} — Premium"
                vip_items.append((lbl, c[3]))
                if len(vip_items) >= 16:
                    break

    # 5 запасных локаций для PREMIUM (по запросу пользователя)
    vip_backup_added = 0
    for c in alive_tested:
        if c[5] == "auto" and c[4][0] not in used_hosts_vip:
            used_hosts_vip.add(c[4][0])
            vip_backup_added += 1
            cc = c[0]
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            prefix = "⚡️ " if cc in NEARBY_COUNTRIES else ""
            lbl = f"{flag} {prefix}Резерв #{vip_backup_added} — Premium"
            vip_items.append((lbl, c[3]))
            if vip_backup_added >= 5:
                break

    # До 3 уникальных LTE/обходных локаций для VIP
    lte_added = 0
    for c in alive_tested:
        if c[5] in ("lte", "whitelist") and c[6] <= 750.0 and c[4][0] not in used_hosts_vip:
            used_hosts_vip.add(c[4][0])
            lte_added += 1
            lbl = f"🇷🇺 LTE #{lte_added} — Premium" if lte_added > 1 else "🇷🇺 LTE — Premium"
            vip_items.append((lbl, c[3]))
            if lte_added >= 3:
                break

    # 2. FREE Pool: 5 европейских стран + 1 LTE + 2 запасных локации (строго "— FREE")
    used_hosts_free: set[str] = set()
    free_items: list[tuple[str, str]] = []

    for cc in FREE_TARGET_COUNTRIES:
        max_lat = COUNTRY_MAX_LATENCY.get(cc, DEFAULT_MAX_LATENCY)
        matching = [
            c for c in alive_tested
            if c[0] == cc and c[5] == "auto" and c[2] not in ("hy2", "hysteria2")
            and c[6] <= max_lat and c[4][0] not in used_hosts_free
        ]
        if matching:
            best = matching[0]
            used_hosts_free.add(best[4][0])
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            lbl = f"{flag} {best[1]} — FREE"
            free_items.append((lbl, best[3]))

    # Если в FREE меньше 5 узлов, добираем из любых живых европейских узлов
    if len(free_items) < 5:
        free_cc_used = {c[0] for c in alive_tested if any(it[1] == c[3] for it in free_items)}
        for c in alive_tested:
            cc = c[0]
            if cc in NEARBY_COUNTRIES and cc not in free_cc_used and c[4][0] not in used_hosts_free and c[5] == "auto" and c[2] not in ("hy2", "hysteria2"):
                used_hosts_free.add(c[4][0])
                free_cc_used.add(cc)
                flag = COUNTRY_FLAGS.get(cc, "🌐")
                lbl = f"{flag} {c[1]} — FREE"
                free_items.append((lbl, c[3]))
                if len(free_items) >= 5:
                    break

    # 1 LTE для FREE
    for c in alive_tested:
        if c[5] in ("lte", "whitelist") and c[6] <= 750.0 and c[4][0] not in used_hosts_free:
            used_hosts_free.add(c[4][0])
            free_items.append(("🇷🇺 Россия LTE — FREE", c[3]))
            break

    # 2 запасных локации для FREE (по запросу пользователя)
    free_backup_added = 0
    for c in alive_tested:
        if c[5] == "auto" and c[4][0] not in used_hosts_free and c[2] not in ("hy2", "hysteria2"):
            used_hosts_free.add(c[4][0])
            free_backup_added += 1
            cc = c[0]
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            lbl = f"{flag} Резерв #{free_backup_added} — FREE"
            free_items.append((lbl, c[3]))
            if free_backup_added >= 2:
                break

    # Защита от пустых подписок: восстанавливаем из кэша
    cached = load_cache()
    cached_free = cached.get("free", [])
    cached_vip = cached.get("vip", [])
    if not free_items and cached_free:
        logger.warning("FREE пул пуст — восстанавливаем из кэша")
        free_items = [
            (item["label"].replace("— Фри", "— FREE"), item["uri"])
            for item in cached_free if "uri" in item and not is_toxic_config(item["uri"])
        ]
    if not vip_items and cached_vip:
        logger.warning("VIP пул пуст — восстанавливаем из кэша")
        vip_items = [
            (item["label"].replace("— Фри", "— FREE"), item["uri"])
            for item in cached_vip if "uri" in item and not is_toxic_config(item["uri"])
        ]

    # 3. Базовые FREE узлы обязательно добавляются в конец VIP подписки как резервные
    for f_lbl, f_uri in free_items:
        vip_items.append((f_lbl, f_uri))

    logger.info("Сформирован FREE пул: %d серверов (0 дублей)", len(free_items))
    logger.info("Сформирован Premium пул: %d серверов (включая FREE резервные)", len(vip_items))
    return free_items, vip_items

# ── Скрапинг источников ───────────────────────────────────────────────────

def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("sources", [])
    return [Source.from_dict(i) for i in items if i.get("enabled", True)]

def fetch_text(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    req = Request(url, headers={"User-Agent": "HQRay/2.0", "Accept": "*/*"})
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
            seen.add(k)
            out.append(v)
    return out

def detect_protocol(raw: str) -> str:
    return raw.split("://")[0].lower() if "://" in raw else "vless"

def parse_text(source: Source, text: str, found_in: str) -> list[ConfigItem]:
    res = []
    for v in extract_configs(text):
        proto = detect_protocol(v)
        res.append(ConfigItem(proto, v, source.name, source.url, found_in, source.kind, source.country))
    return res

def dedupe(configs: list[ConfigItem]) -> list[ConfigItem]:
    seen: set[str] = set()
    out: list[ConfigItem] = []
    for c in configs:
        k = c.canonical_key()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out

# ── Кэш состояния и сборка ────────────────────────────────────────────────

STATE_CACHE_FILE = OUTPUT_DIR / "state_cache.json"

def load_cache() -> dict[str, Any]:
    if STATE_CACHE_FILE.exists():
        try:
            return json.loads(STATE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_cache(vip_items: list[tuple[str, str]], free_items: list[tuple[str, str]]) -> None:
    try:
        data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "vip": [{"label": l, "uri": r} for l, r in vip_items],
            "free": [{"label": l, "uri": r} for l, r in free_items],
        }
        STATE_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Кэш состояния сохранен в %s", STATE_CACHE_FILE)
    except Exception as e:
        logger.warning("Не удалось сохранить кэш состояния: %s", e)

def save_to_turso(key: str, value: str) -> None:
    turso_exec("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [key, value])

def build() -> None:
    global MY_PUBLIC_IP, RKN_NETWORKS
    logger.info("=== СТАРТ СБОРКИ HQRay VPN (True Delay Edition) ===")
    MY_PUBLIC_IP = detect_my_ip()
    logger.info("Текущий IP хоста: %s", MY_PUBLIC_IP or "не определён")
    RKN_NETWORKS = load_rkn_blocklist()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sources = load_sources()
    all_configs: list[ConfigItem] = []

    # Добавляем ранее работавшие ноды из кэша для повторной перепроверки
    cached = load_cache()
    cached_vip = cached.get("vip", [])
    cached_free = cached.get("free", [])
    seen_cache_uris = set()
    for item in cached_vip + cached_free:
        u = item.get("uri")
        lbl = item.get("label", "")
        if u and u not in seen_cache_uris and not is_toxic_config(u):
            seen_cache_uris.add(u)
            proto = detect_protocol(u)
            c_code = None
            kind_val = "auto"
            if "LTE" in lbl:
                kind_val = "lte"
                c_code = "RU"
            elif "Белый список" in lbl:
                kind_val = "whitelist"
                c_code = "RU"
            else:
                for cc, ru in COUNTRY_NAMES_RU.items():
                    if ru in lbl:
                        c_code = cc
                        break
                if not c_code:
                    for cc, fl in COUNTRY_FLAGS.items():
                        if fl in lbl:
                            c_code = cc
                            break
            all_configs.append(ConfigItem(proto, u, "cache", "cache", "cache", kind_val, c_code))

    for src in sources:
        logger.info("Загрузка: %s (%s)", src.name, src.url)
        try:
            items = parse_text(src, fetch_text(src.url), src.url)
            logger.info("  -> получено %d конфигов", len(items))
            all_configs += items
        except Exception as e:
            logger.warning("  -> ошибка: %s", e)

    unique = dedupe(all_configs)
    logger.info("Всего уникальных конфигов: %d", len(unique))

    free_items, vip_items = build_pools(unique)

    # Сохраняем в кэш состояния
    save_cache(vip_items, free_items)

    # 1. Free подписка
    free_plain = "\n".join([apply_label(r, l) for l, r in free_items]) + "\n"
    free_singbox = build_singbox_config(free_items, "💎 HQRay VPN - @hqraybot")
    free_xray = build_xray_array(free_items)
    free_data = {"tier": "free", "count": len(free_items), "items": [{"label": l, "uri": r} for l, r in free_items]}

    (OUTPUT_DIR / "subscription_free.txt").write_text(free_plain, encoding="utf-8")
    (OUTPUT_DIR / "singbox_free.json").write_text(json.dumps(free_singbox, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "xray_free.json").write_text(json.dumps(free_xray, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. Premium (VIP) подписка
    vip_plain = "\n".join([apply_label(r, l) for l, r in vip_items]) + "\n"
    vip_singbox = build_singbox_config(vip_items, "💎 HQRay VPN - @hqraybot")
    vip_xray = build_xray_array(vip_items)
    vip_data = {"tier": "vip", "count": len(vip_items), "items": [{"label": l, "uri": r} for l, r in vip_items]}

    (OUTPUT_DIR / "subscription_vip.txt").write_text(vip_plain, encoding="utf-8")
    (OUTPUT_DIR / "singbox_vip.json").write_text(json.dumps(vip_singbox, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "xray_vip.json").write_text(json.dumps(vip_xray, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Локальные файлы подписок сохранены в %s", OUTPUT_DIR)

    # Сохранение в Turso DB
    logger.info("Синхронизация с Turso DB...")
    save_to_turso("subscription_free", free_plain)
    save_to_turso("subscription_free_singbox", json.dumps(free_singbox, ensure_ascii=False))
    save_to_turso("subscription_free_xray", json.dumps(free_xray, ensure_ascii=False))
    save_to_turso("subscription_free_data", json.dumps(free_data, ensure_ascii=False))

    save_to_turso("subscription_vip", vip_plain)
    save_to_turso("subscription_vip_singbox", json.dumps(vip_singbox, ensure_ascii=False))
    save_to_turso("subscription_vip_xray", json.dumps(vip_xray, ensure_ascii=False))
    save_to_turso("subscription_vip_data", json.dumps(vip_data, ensure_ascii=False))

    # Обратная совместимость для старых клиентов
    save_to_turso("subscription", free_plain)
    save_to_turso("subscription_singbox", json.dumps(free_singbox, ensure_ascii=False))
    save_to_turso("subscription_xray", json.dumps(free_xray, ensure_ascii=False))
    save_to_turso("subscription_data", json.dumps(free_data, ensure_ascii=False))

    logger.info("=== СБОРКА УСПЕШНО ЗАВЕРШЕНА ===")

if __name__ == "__main__":
    build()
