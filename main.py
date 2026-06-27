from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
OUTPUT_DIR = ROOT / "output"
MANIFEST_FILE = OUTPUT_DIR / "manifest.json"
SUBSCRIPTION_FILE = OUTPUT_DIR / "subscription.txt"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("nekrozvpn")


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
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"source '{name}' has invalid url")

        return cls(name=name, url=url, enabled=enabled)


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


def dedupe_sources(sources: list[Source]) -> list[Source]:
    seen: set[str] = set()
    result: list[Source] = []

    for source in sources:
        key = source.url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        result.append(source)

    return result


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def build_manifest(sources: list[Source]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "project": "NekrozVPN",
        "generated_at": now,
        "enabled_sources": len(sources),
        "sources": [
            {
                "name": source.name,
                "url": source.url,
            }
            for source in sources
        ],
    }


def write_manifest(manifest: dict[str, Any]) -> None:
    ensure_output_dir()
    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_subscription_text(manifest: dict[str, Any]) -> None:
    ensure_output_dir()

    lines = [
        "# NekrozVPN subscription",
        f"# generated_at: {manifest['generated_at']}",
        f"# sources: {manifest['enabled_sources']}",
        "",
    ]

    for item in manifest["sources"]:
        lines.append(f"{item['name']} | {item['url']}")

    SUBSCRIPTION_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build() -> None:
    sources = load_sources()
    sources = dedupe_sources(sources)

    manifest = build_manifest(sources)
    write_manifest(manifest)
    write_subscription_text(manifest)

    logger.info("built %d source(s)", len(sources))
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
