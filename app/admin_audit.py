from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config_store import DATA_DIR

log = logging.getLogger(__name__)

AUDIT_STORE_PATH = DATA_DIR / "admin_audit.json"


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _truncate(value: Any, limit: int = 240) -> str:
    text = " ".join(_text(value).split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class AuditEntry:
    timestamp: str = field(default_factory=_now_iso)
    action: str = ""
    section: str = ""
    status: str = "success"
    actor: str = "admin"
    detail: str = ""
    target: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "AuditEntry":
        metadata = data.get("metadata", {})
        return cls(
            timestamp=_text(data.get("timestamp")) or _now_iso(),
            action=_text(data.get("action")),
            section=_text(data.get("section")),
            status=_text(data.get("status")) or "success",
            actor=_text(data.get("actor")) or "admin",
            detail=_text(data.get("detail")),
            target=_text(data.get("target")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


def _load_entries(path: Path | str = AUDIT_STORE_PATH) -> list[dict[str, Any]]:
    store_path = Path(path)
    if not store_path.exists():
        return []

    try:
        with store_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        log.warning("Audit store JSON is invalid at %s: %s", store_path, exc)
        return []
    except OSError as exc:
        log.warning("Unable to read audit store at %s: %s", store_path, exc)
        return []

    if not isinstance(data, list):
        log.warning("Audit store at %s did not contain a JSON list", store_path)
        return []

    return [item for item in data if isinstance(item, dict)]


def _save_entries(entries: list[dict[str, Any]], path: Path | str = AUDIT_STORE_PATH) -> Path:
    store_path = Path(path)
    _ensure_parent_dir(store_path)
    with store_path.open("w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    return store_path


def record_audit_event(
    *,
    action: str,
    section: str,
    status: str = "success",
    actor: str = "admin",
    detail: str = "",
    target: str = "",
    metadata: dict[str, Any] | None = None,
    path: Path | str = AUDIT_STORE_PATH,
    max_items: int = 300,
) -> Path:
    entries = _load_entries(path)
    entry = AuditEntry(
        action=action.strip(),
        section=section.strip(),
        status=(status or "success").strip(),
        actor=(actor or "admin").strip(),
        detail=_truncate(detail),
        target=_truncate(target, 120),
        metadata=metadata or {},
    )
    entries.append(entry.to_dict())
    if max_items > 0 and len(entries) > max_items:
        entries = entries[-max_items:]
    return _save_entries(entries, path)


def list_audit_entries(limit: int = 20, path: Path | str = AUDIT_STORE_PATH) -> list[dict[str, Any]]:
    entries = [AuditEntry.from_mapping(item).to_dict() for item in _load_entries(path)]
    if limit >= 0:
        entries = entries[-limit:]
    return list(reversed(entries))
