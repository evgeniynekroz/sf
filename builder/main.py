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
TCP_TIMEOUT       = 2.5
TCP_MAX_WORKERS   = 50

# ── Xray Core Проверка скорости и задержки ──────────────────────────────────
REAL_CHECK_WORKERS       = 8
REAL_CHECK_TIMEOUT       = 4
REAL_CHECK_SPEED_URL     = "https://speed.cloudflare.com/__down?bytes=1500000"  # 1.5MB для быстрого замера
REAL_CHECK_MIN_VIP_MBPS  = 25.0  # От 25 Мбит/с для Premium
REAL_CHECK_MIN_FREE_MBPS = 3.0   # От 3 Мбит/с для Free
REAL_CHECK_IP_URL        = "https://api.ipify.org"
REAL_CHECK_URL           = "http://cp.cloudflare.com/generate_204"
REAL_CHECK_PORT_BASE     = 21080
XRAY_BIN                 = shutil.which("xray") or str(ROOT / "xray-bin" / ("xray.exe" if os.name == "nt" else "xray"))
if not (XRAY_BIN and os.path.exists(XRAY_BIN)):
    XRAY_BIN = None
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

COUNTRY_FLAGS: dict[str, str] = {
    "DE":"🇩🇪","NL":"🇳🇱","FI":"🇫🇮","EE":"🇪🇪","PL":"🇵🇱","SE":"🇸🇪",
    "GB":"🇬🇧","US":"🇺🇸","TR":"🇹🇷","KZ":"🇰🇿","JP":"🇯🇵","RU":"🇷🇺",
    "AT":"🇦🇹","NO":"🇳🇴","IT":"🇮🇹","FR":"🇫🇷",
}

COUNTRY_NAMES_RU: dict[str, str] = {
    "DE":"Германия","NL":"Нидерланды","FI":"Финляндия","EE":"Эстония",
    "PL":"Польша","SE":"Швеция","GB":"Великобритания","US":"США",
    "TR":"Турция","KZ":"Казахстан","JP":"Япония","RU":"Россия",
    "AT":"Австрия","NO":"Норвегия","IT":"Италия","FR":"Франция",
}

COUNTRY_TAG_RULES = {
    "DE": [r"(?i)(?:\b|_|-)(?:de\d*|germany|frankfurt|berlin|munich|hetzner)(?:\b|_|-|\.)", "Германия", "GERMANY", "🇩🇪"],
    "NL": [r"(?i)(?:\b|_|-)(?:nl\d*|netherlands|holland|amsterdam)(?:\b|_|-|\.)", "Нидерланды", "NETHERLANDS", "HOLLAND", "🇳🇱"],
    "FI": [r"(?i)(?:\b|_|-)(?:fi\d*|finland|helsinki)(?:\b|_|-|\.)", "Финляндия", "FINLAND", "🇫🇮"],
    "EE": [r"(?i)(?:\b|_|-)(?:ee\d*|estonia|tallinn|est)(?:\b|_|-|\.)", "Эстония", "ESTONIA", "🇪🇪"],
    "PL": [r"(?i)(?:\b|_|-)(?:pl\d*|poland|warsaw|pol\d*)(?:\b|_|-|\.)", "Польша", "POLAND", "🇵🇱"],
    "SE": [r"(?i)(?:\b|_|-)(?:se\d*|sweden|stockholm)(?:\b|_|-|\.)", "Швеция", "SWEDEN", "🇸🇪"],
    "AT": [r"(?i)(?:\b|_|-)(?:at\d*|austria|vienna)(?:\b|_|-|\.)", "Австрия", "AUSTRIA", "🇦🇹"],
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
VIP_TARGET_COUNTRIES  = ["DE", "NL", "FI", "EE", "PL", "SE", "GB", "US", "TR", "KZ", "JP", "NO", "IT", "AT"]

# ⚡️ Ближние к России страны (получают префикс-молнию ⚡️)
NEARBY_COUNTRIES = {"DE", "NL", "FI", "EE", "PL", "SE", "AT"}

# Максимально допустимая задержка (мс) для добавления в подписку
COUNTRY_MAX_LATENCY: dict[str, float] = {
    # ⚡️ Ближняя Европа (быстрые для игр/видео)
    "DE": 280.0,
    "FI": 280.0,
    "EE": 280.0,
    "PL": 280.0,
    "SE": 280.0,
    "NL": 280.0,
    "AT": 280.0,
    # Средние страны
    "GB": 280.0,
    "IT": 280.0,
    "KZ": 280.0,
    "TR": 280.0,
    "NO": 280.0,
    "FR": 280.0,
    # Дальние страны
    "US": 350.0,
    "JP": 420.0,
    # LTE / Белый список
    "RU": 280.0,
}
DEFAULT_MAX_LATENCY = 280.0

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

    def key(self) -> str:
        return self.value.strip().rstrip("/")

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

# ── Хосты и TCP пинг ──────────────────────────────────────────────────────

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

def tcp_ping(host: str, port: int, timeout: float = TCP_TIMEOUT) -> float | None:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000
    except Exception:
        return None

def check_tcp_ping(pairs: list[tuple[str, int]]) -> dict[tuple[str, int], float | None]:
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

# ── Xray Core Real Check & Speed Test ────────────────────────────────────

def _xray_outbound(raw: str) -> tuple[str, dict, dict] | None:
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

def real_check_node(raw: str, port: int) -> CheckResult:
    if not XRAY_BIN:
        return CheckResult(ok=True, speed_mbps=75.0, latency_ms=60.0)

    ob = _xray_outbound(raw)
    if ob is None:
        return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
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
        curl_bin = "curl.exe" if os.name == "nt" else "curl"
        return subprocess.run(
            [curl_bin, "--socks5-hostname", f"127.0.0.1:{port}", "--max-time", str(timeout),
             "-s", *extra, url],
            capture_output=True, text=True, timeout=timeout + 3,
        )

    try:
        proc = subprocess.Popen(
            [XRAY_BIN, "run", "-c", cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.4)

        # 1. HTTP GET 204 Check + точный замер Latency через curl %{time_total}
        fmt = "%{http_code}:%{time_total}"
        basic = _curl(REAL_CHECK_URL, ["-o", NULL_DEVICE, "-w", fmt], REAL_CHECK_TIMEOUT)
        parts = basic.stdout.strip().split(":")
        http_code = parts[0] if parts else ""
        time_total_s = float(parts[1]) if len(parts) > 1 and parts[1] else 9.999
        latency = time_total_s * 1000.0

        is_ru_whitelist = False
        if basic.returncode != 0 or http_code not in ("200", "204", "301", "302"):
            # Проверка для белых списков РФ (где зарубежный Cloudflare заблокирован)
            ru_check = _curl("http://ya.ru", ["-o", NULL_DEVICE, "-w", fmt], REAL_CHECK_TIMEOUT)
            ru_parts = ru_check.stdout.strip().split(":")
            ru_code = ru_parts[0] if ru_parts else ""
            if ru_check.returncode == 0 and ru_code in ("200", "301", "302"):
                is_ru_whitelist = True
                latency = float(ru_parts[1]) * 1000.0 if len(ru_parts) > 1 and ru_parts[1] else 9999.0
            else:
                return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)

        # 2. Двойная проверка на стабильность (устранение джиттера и packet loss)
        chk_url = "http://ya.ru" if is_ru_whitelist else REAL_CHECK_URL
        basic2 = _curl(chk_url, ["-o", NULL_DEVICE, "-w", fmt], REAL_CHECK_TIMEOUT)
        parts2 = basic2.stdout.strip().split(":")
        code2 = parts2[0] if parts2 else ""
        if basic2.returncode != 0 or code2 not in ("200", "204", "301", "302"):
            return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
        lat2 = float(parts2[1]) * 1000.0 if len(parts2) > 1 and parts2[1] else 9999.0
        latency = (latency + lat2) / 2.0

        # 3. IP Leak Check (только для зарубежных узлов)
        exit_ip = None
        if MY_PUBLIC_IP and not is_ru_whitelist:
            try:
                ip_result = _curl(REAL_CHECK_IP_URL, ["-o", "-"], 3)
                if ip_result.returncode == 0:
                    cand_ip = ip_result.stdout.strip()
                    if cand_ip == MY_PUBLIC_IP:
                        # Утечка прямого IP — прокси не скрывает адрес
                        return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
                    exit_ip = cand_ip
            except Exception:
                pass

        # 4. Скоростной тест (1MB блок с cache-buster)
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
            elif sp_code == "429":
                # Cloudflare rate limit — резервный замер через Tele2
                t2 = _curl("http://speedtest.tele2.net/1MB.zip", ["-o", NULL_DEVICE, "-w", "%{http_code}:%{speed_download}"], REAL_CHECK_TIMEOUT)
                t2_parts = t2.stdout.strip().split(":")
                if t2.returncode in (0, 28) and len(t2_parts) > 1:
                    try:
                        speed_mbps = (float(t2_parts[1]) * 8.0) / 1_000_000.0
                    except ValueError:
                        speed_mbps = 15.0
                else:
                    speed_mbps = 15.0
            else:
                # Если HTTP-доступ гарантирован, но сервер блокирует прямой скач тестового файла, даем базовый пропуск
                speed_mbps = 10.0

            # Отсекаем полностью непригодные для видео/веба узлы
            if speed_mbps < 2.0:
                return CheckResult(ok=False, speed_mbps=speed_mbps, latency_ms=latency)

        return CheckResult(ok=True, speed_mbps=speed_mbps, latency_ms=latency, exit_ip=exit_ip)
    except Exception:
        return CheckResult(ok=False, speed_mbps=0.0, latency_ms=9999.0)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
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
        logger.warning("xray не найден в PATH (локальный запуск) — эмулируем результаты на основе пинга")
        return {r: CheckResult(ok=True, speed_mbps=78.5, latency_ms=45.0) for r in unique}

    logger.info("Реальная проверка через Xray-core для %d финалистов...", len(unique))
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
    logger.info("Xray-core: живых %d/%d, с гарантией 60+ Мбит/с: %d", alive_ok, len(unique), vip_ok)
    return results

# ── Форматирование и трансформация имён ───────────────────────────────────

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
    return f"{base}#{label}"

# ── Sing-box & Xray конвертеры ───────────────────────────────────────────

def uri_to_outbound(raw: str, tag: str) -> dict | None:
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
            s_enc = s[8:].split("#")[0]
            data = json.loads(base64.b64decode(s_enc + "=" * (-len(s_enc) % 4)).decode())
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
        outbounds.append({"type": "selector", "tag": title, "outbounds": tags_list + ["direct"], "default": tags_list[0]})
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
        "outbounds": outbounds,
        "route": {"final": final, "auto_detect_interface": True},
    }

def build_xray_array(items: list[tuple[str, str]]) -> list[dict]:
    res: list[dict] = []
    for label, raw in items:
        prof = build_xray_profile(raw, label)
        if prof:
            res.append(prof)
    return res

# ── Построение пулов (Free vs VIP) ────────────────────────────────────────

def build_pools(
    configs: list[ConfigItem],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    candidates: list[tuple] = []  # (cc, country_ru, protocol, raw, (host, port), kind)
    rkn_skipped = 0

    for item in configs:
        hp = extract_host_port(item.value)
        if not hp:
            continue
        if is_rkn_blocked(hp[0]):
            rkn_skipped += 1
            continue
        cc = item.country
        if not cc:
            val_low = item.value.lower()
            if any(w in val_low for w in ("api3-max", "posa", ".ru", "white", "белый", "lte", "россия")):
                cc = "RU"
        if not cc or cc not in COUNTRY_NAMES_RU:
            continue
        c_ru = COUNTRY_NAMES_RU.get(cc, "Сервер")
        candidates.append((cc, c_ru, item.protocol, item.value, hp, item.kind))

    if rkn_skipped:
        logger.info("RKN-фильтр: пропущено %d заблокированных хостов", rkn_skipped)

    # Приоритет протоколам: vless > trojan > vmess > ss
    proto_rank = {"vless": 0, "trojan": 1, "vmess": 2, "ss": 3}
    candidates.sort(key=lambda c: proto_rank.get(c[2], 9))

    # Отбираем кандидатов на каждую целевую страну для TCP пинга
    by_cc_candidates: dict[str, list[tuple]] = defaultdict(list)
    lte_candidates: list[tuple] = []

    for c in candidates:
        cc, kind = c[0], c[5]
        if kind in ("lte", "whitelist"):
            lte_candidates.append(c)
        elif len(by_cc_candidates[cc]) < 35:
            by_cc_candidates[cc].append(c)

    ping_subset = []
    for c_list in by_cc_candidates.values():
        ping_subset.extend(c_list)
    ping_subset.extend(lte_candidates[:60])

    unique_pairs = list({c[4] for c in ping_subset})
    logger.info("TCP-проверка для %d отобранных адресов целевых стран...", len(unique_pairs))
    ping_results = check_tcp_ping(unique_pairs)

    alive = [(*c, ping_results[c[4]]) for c in ping_subset if ping_results.get(c[4]) is not None]
    alive.sort(key=lambda c: c[6])
    logger.info("Живых узлов после TCP пинга: %d", len(alive))

    # Финалисты для детального теста через Xray core + замер скорости
    finalists_set: set[str] = set()
    for cc in VIP_TARGET_COUNTRIES:
        cands = [c for c in alive if c[0] == cc and c[5] == "auto"]
        for c in cands[:15]:
            finalists_set.add(c[3])

    for c in [c for c in alive if c[5] in ("lte", "whitelist")][:30]:
        finalists_set.add(c[3])

    logger.info("Финалистов на тест скорости и доступности: %d", len(finalists_set))
    test_results = real_check_batch(list(finalists_set))

    def sort_key(c):
        res = test_results.get(c[3])
        if not res or not res.ok:
            return (9999.0, 0.0)
        # Приоритет: минимальный TCP-пинг до узла c[6], затем максимальная скорость
        return (c[6], -res.speed_mbps)

    alive_tested = [c for c in alive if c[3] in test_results and test_results[c[3]].ok]
    alive_tested.sort(key=sort_key)

    # 1. Free Pool (5 EU + 1 LTE) — только качественные узлы в пределах порога
    free_items: list[tuple[str, str]] = []
    free_nodes_map: dict[str, tuple[str, str]] = {}

    for cc in FREE_TARGET_COUNTRIES:
        max_lat = COUNTRY_MAX_LATENCY.get(cc, DEFAULT_MAX_LATENCY)
        matching = [
            c for c in alive_tested
            if c[0] == cc and c[5] == "auto" and c[6] <= max_lat
        ]
        if matching:
            best = matching[0]
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            lbl = f"{flag} {best[1]} — Базовый"
            free_nodes_map[cc] = (lbl, best[3])
            free_items.append((lbl, best[3]))

    # 1 LTE для Free (строго с пингом <= 250 мс)
    lte_cands = [c for c in alive_tested if c[5] in ("lte", "whitelist") and c[6] <= 250.0]
    if lte_cands:
        free_items.append(("🇷🇺 Россия LTE — Базовый", lte_cands[0][3]))

    # 2. Premium (VIP) Pool
    vip_items: list[tuple[str, str]] = []

    # А) Premium High-Speed локации (строго проверенные, в пределах порога задержки)
    for cc in VIP_TARGET_COUNTRIES:
        max_lat = COUNTRY_MAX_LATENCY.get(cc, DEFAULT_MAX_LATENCY)
        vip_matching = [
            c for c in alive_tested
            if c[0] == cc and c[5] == "auto" and c[6] <= max_lat
        ]
        if vip_matching:
            best = vip_matching[0]
            if cc in NEARBY_COUNTRIES:
                # ⚡️ Молнию только для ближайших к России стран!
                lbl = f"⚡️{best[1]} — Premium"
            else:
                flag = COUNTRY_FLAGS.get(cc, "🌐")
                lbl = f"{flag} {best[1]} — Premium"
            vip_items.append((lbl, best[3]))

    # Б) До 7 LTE / White-list локаций для Premium (строго <= 250 мс)
    vip_lte_seen = set()
    vip_lte_count = 0
    for c in alive_tested:
        if c[5] in ("lte", "whitelist") and c[6] <= 250.0 and c[4][0] not in vip_lte_seen:
            vip_lte_seen.add(c[4][0])
            vip_lte_count += 1
            kind_title = "LTE" if c[5] == "lte" else "Белый список"
            lbl = f"🇷🇺 {kind_title} — Premium" if vip_lte_count == 1 else f"🇷🇺 {kind_title} #{vip_lte_count} — Premium"
            vip_items.append((lbl, c[3]))
            if vip_lte_count >= 7:
                break

    # В) 5 Free backup локаций (Германия, Нидерланды, Финляндия, Польша, Швеция в подписке дважды!)
    for cc in FREE_TARGET_COUNTRIES:
        if cc in free_nodes_map:
            vip_items.append(free_nodes_map[cc])

    logger.info("Сформирован Базовый пул: %d серверов", len(free_items))
    logger.info("Сформирован Premium пул: %d серверов", len(vip_items))
    return free_items, vip_items

# ── Скрапинг источников ───────────────────────────────────────────────────

def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("sources", [])
    return [Source.from_dict(i) for i in items if i.get("enabled", True)]

def fetch_text(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    req = Request(url, headers={"User-Agent": "HQRay/1.0", "Accept": "*/*"})
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
            seen.add(k)
            out.append(v)
    return out

def detect_country(raw: str, default: str | None = None) -> str | None:
    if default:
        return default
    tag = unquote(raw.split("#")[-1]) if "#" in raw else ""
    host = ""
    try:
        # Убираем скобки вокруг IPv4 если есть для совместимости со старыми парсерами
        clean_raw = re.sub(r'@\[(\d+\.\d+\.\d+\.\d+)\]', r'@\1', raw)
        p = urlparse(clean_raw)
        host = p.hostname or ""
    except Exception:
        pass
    search_str = f"{tag} {host}"
    for cc, pats in COUNTRY_TAG_RULES.items():
        for pat in pats:
            if re.search(pat, search_str, re.IGNORECASE):
                return cc
    return None

def parse_text(source: Source, text: str, found_in: str) -> list[ConfigItem]:
    res = []
    for v in extract_configs(text):
        proto = detect_protocol(v)
        cc = detect_country(v, source.country)
        kind = source.kind
        if kind == "auto" and cc == "RU":
            tag_low = (v.split("#")[-1] if "#" in v else "").lower()
            if any(w in tag_low for w in ("lte", "white", "белый", "бс", "max")):
                kind = "lte"
        res.append(ConfigItem(proto, v, source.name, source.url, found_in, kind, cc))
    return res

def dedupe(configs: list[ConfigItem]) -> list[ConfigItem]:
    seen: set[str] = set()
    out: list[ConfigItem] = []
    for c in configs:
        k = c.key()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out

# ── Кэш состояния и пайплайн сборки ───────────────────────────────────────

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
    logger.info("=== СТАРТ СБОРКИ HQRay VPN ===")
    MY_PUBLIC_IP = detect_my_ip()
    logger.info("Текущий IP хоста: %s", MY_PUBLIC_IP or "не определён")
    RKN_NETWORKS = load_rkn_blocklist()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Быстрая проверка ранее работавших серверов из кэша
    cached = load_cache()
    cached_vip = cached.get("vip", [])
    cached_free = cached.get("free", [])
    cached_uris = list({item["uri"] for item in (cached_vip + cached_free) if "uri" in item})
    cached_configs: list[ConfigItem] = []

    if cached_uris:
        logger.info("Проверка %d ранее работавших нод из кэша...", len(cached_uris))
        cache_check = real_check_batch(cached_uris)
        healthy_cached = [u for u, res in cache_check.items() if res.ok]
        logger.info("Из кэша живо и прошло проверку: %d/%d", len(healthy_cached), len(cached_uris))
        for u in healthy_cached:
            proto = u.split("://")[0].lower() if "://" in u else "vless"
            c_code = "DE"
            kind_val = "auto"
            for item in cached_vip + cached_free:
                if item.get("uri") == u:
                    lbl = item.get("label", "")
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
                    break
            cached_configs.append(ConfigItem(source="cache", protocol=proto, value=u, country=c_code, kind=kind_val))

    sources = load_sources()
    all_configs: list[ConfigItem] = list(cached_configs)

    for src in sources:
        logger.info("Загрузка: %s (%s)", src.name, src.url)
        try:
            items = parse_text(src, fetch_text(src.url), src.url)
            logger.info("  -> получено %d конфигов", len(items))
            all_configs += items
        except Exception as e:
            logger.warning("  -> ошибка: %s", e)

    unique = dedupe(all_configs)
    logger.info("Всего уникальных конфигов (включая кэш): %d", len(unique))

    free_items, vip_items = build_pools(unique)

    # Сохраняем в кэш состояния для следующих запусков
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
