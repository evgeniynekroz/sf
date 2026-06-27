from __future__ import annotations

import base64
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
OUTPUT_DIR = ROOT / "output"
MANIFEST_FILE = OUTPUT_DIR / "manifest.json"
SUBSCRIPTION_FILE = OUTPUT_DIR / "subscription.txt"

FETCH_TIMEOUT = 30
MAX_HTTP_BYTES = 1_500_000
MAX_TREE_DEPTH = 2
MAX_GITHUB_PAGES = 40
MAX_GITHUB_FILES = 120

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

HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


@dataclass(slots=True)
class Source:
    name: str
    url: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        if not isinstance(data, dict):
            raise ValueError("each source entry must be an object")

        name = str(data.get("name", "")).strip()
        url = str(data.get("url", "")).strip()
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
    protocol: str
    value: str
    source_name: str
    source_url: str
    found_in: str

    def normalized_key(self) -> str:
        return self.value.strip().rstrip("/")


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    if not path.exists():
        raise FileNotFoundError(f"missing file: {path.name}")

    raw = json.loads(path.read_text(encoding="utf-8"))
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
            "Accept": "text/plain,text/html,application/json,*/*",
        },
    )

    with urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_HTTP_BYTES + 1)
        if len(data) > MAX_HTTP_BYTES:
            data = data[:MAX_HTTP_BYTES]

        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


def normalize_text(text: str) -> str:
    return unescape(text).replace("\r", "")


def is_probable_base64(text: str) -> bool:
    compact = "".join(text.split())
    if len(compact) < 32:
        return False
    if len(compact) % 4 != 0:
        return False
    return bool(BASE64_RE.fullmatch(text))


def try_decode_base64(text: str) -> str | None:
    compact = "".join(text.split())
    if not is_probable_base64(compact):
        return None

    padded = compact + "=" * (-len(compact) % 4)

    try:
        decoded = base64.b64decode(padded, validate=False)
    except Exception:
        return None

    decoded_text = decoded.decode("utf-8", errors="replace")
    if any(proto in decoded_text.lower() for proto in ("vless://", "trojan://", "ss://", "ssr://", "vmess://", "hy2://", "hysteria2://", "tuic://")):
        return decoded_text

    return None


def detect_protocol(value: str) -> str:
    lowered = value.lower().strip()
    for proto in ("vless", "trojan", "ssr", "ss", "vmess", "hy2", "hysteria2", "tuic"):
        if lowered.startswith(proto + "://"):
            return proto
    return "unknown"


def normalize_config(value: str) -> str:
    cleaned = value.strip()
    cleaned = cleaned.strip('"').strip("'")
    cleaned = cleaned.rstrip(",;)")
    cleaned = cleaned.replace("\n", "").replace("\t", "")
    return cleaned


def extract_config_values(text: str, depth: int = 0) -> list[str]:
    text = normalize_text(text)
    found: list[str] = []

    for pattern in CONFIG_PATTERNS:
        for match in pattern.finditer(text):
            found.append(normalize_config(match.group(0)))

    if depth == 0:
        decoded = try_decode_base64(text)
        if decoded:
            found.extend(extract_config_values(decoded, depth=1))

    result: list[str] = []
    seen: set[str] = set()

    for item in found:
        key = item.strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(key)

    return result


def parse_plain_text(source: Source, text: str, found_in: str) -> list[ConfigItem]:
    items: list[ConfigItem] = []
    values = extract_config_values(text)

    for value in values:
        items.append(
            ConfigItem(
                protocol=detect_protocol(value),
                value=value,
                source_name=source.name,
                source_url=source.url,
                found_in=found_in,
            )
        )

    return items


def is_github_url(url: str) -> bool:
    return urlparse(url).netloc.lower() in {"github.com", "www.github.com"}


def github_owner_repo_from_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]

    if len(parts) < 2:
        return None

    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo


def github_raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path.lstrip('/')}"


def github_repo_root_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}"


def github_tree_url(owner: str, repo: str, branch: str, path: str = "") -> str:
    if path:
        return f"https://github.com/{owner}/{repo}/tree/{branch}/{path.lstrip('/')}"
    return f"https://github.com/{owner}/{repo}/tree/{branch}"


def extract_github_links(html: str, owner: str, repo: str) -> list[str]:
    links: list[str] = []
    prefix = f"/{owner}/{repo}/"

    for href in HREF_RE.findall(html):
        href = unquote(href.split("?", 1)[0].split("#", 1)[0])
        if not href.startswith(prefix):
            continue
        rest = href[len(prefix):]
        if rest.startswith(("blob/", "tree/")):
            links.append(href)

    deduped: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        deduped.append(link)

    return deduped


def parse_github_link_path(href: str, owner: str, repo: str) -> tuple[str, str, str] | None:
    prefix = f"/{owner}/{repo}/"
    if not href.startswith(prefix):
        return None

    rest = href[len(prefix):]
    parts = rest.split("/", 2)
    if len(parts) < 2:
        return None

    kind = parts[0]
    branch = parts[1]
    path = parts[2] if len(parts) > 2 else ""
    return kind, branch, path


def crawl_github_repo(source: Source) -> list[ConfigItem]:
    owner_repo = github_owner_repo_from_url(source.url)
    if owner_repo is None:
        return []

    owner, repo = owner_repo
    start_url = source.url.rstrip("/")

    queue: list[tuple[str, int]] = [(start_url, 0)]
    visited_pages: set[str] = set()
    visited_files: set[str] = set()
    collected: list[ConfigItem] = []

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
            # Some repos expose enough info on the root page for direct parsing.
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
                    raw_text = fetch_text(raw_url)
                except (HTTPError, URLError, TimeoutError) as exc:
                    logger.warning("skip file %s: %s", raw_url, exc)
                    continue

                collected.extend(parse_plain_text(source, raw_text, raw_url))

            elif kind == "tree" and path and depth < MAX_TREE_DEPTH:
                tree_url = github_tree_url(owner, repo, branch, path)
                if tree_url not in visited_pages:
                    queue.append((tree_url, depth + 1))

            elif kind == "tree" and not path and depth < MAX_TREE_DEPTH:
                tree_url = github_tree_url(owner, repo, branch)
                if tree_url not in visited_pages:
                    queue.append((tree_url, depth + 1))

    # Also parse the start page itself; sometimes configs are in README or raw text shown there.
    try:
        start_text = fetch_text(start_url)
        collected.extend(parse_plain_text(source, start_text, start_url))
    except Exception:
        pass

    return collected


def dedupe_configs(configs: list[ConfigItem]) -> list[ConfigItem]:
    seen: set[str] = set()
    result: list[ConfigItem] = []

    for item in configs:
        key = item.normalized_key()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)

    return result


def build_manifest(configs: list[ConfigItem], sources_count: int) -> dict[str, Any]:
    counts = Counter(item.protocol for item in configs)
    return {
        "project": "NekrozVPN",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources_total": sources_count,
        "configs_total": len(configs),
        "configs_by_protocol": dict(sorted(counts.items())),
        "configs": [
            {
                "protocol": item.protocol,
                "value": item.value,
                "source_name": item.source_name,
                "source_url": item.source_url,
                "found_in": item.found_in,
            }
            for item in configs
        ],
    }


def write_outputs(manifest: dict[str, Any], configs: list[ConfigItem]) -> None:
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
    lines.extend(item.value for item in configs)

    SUBSCRIPTION_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build() -> None:
    sources = load_sources()
    collected: list[ConfigItem] = []

    for source in sources:
        logger.info("processing %s", source.name)

        if is_github_url(source.url):
            items = crawl_github_repo(source)
        else:
            try:
                text = fetch_text(source.url)
            except (HTTPError, URLError, TimeoutError) as exc:
                logger.warning("skip %s: %s", source.name, exc)
                continue
            items = parse_plain_text(source, text, source.url)

        logger.info("found %d candidate(s) in %s", len(items), source.name)
        collected.extend(items)

    unique_configs = dedupe_configs(collected)
    manifest = build_manifest(unique_configs, len(sources))
    write_outputs(manifest, unique_configs)

    logger.info("saved %d unique config(s)", len(unique_configs))
    logger.info("wrote %s", MANIFEST_FILE.relative_to(ROOT))
    logger.info("wrote %s", SUBSCRIPTION_FILE.relative_to(ROOT))


def main() -> int:
    try:
        build()
    except Exception as exc:  # noqa: BLE001
        logger.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
