from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config_store import BASE_DIR, DATA_DIR

log = logging.getLogger(__name__)

REPORT_STORE_PATH = DATA_DIR / "reports.json"


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _message_excerpt(message: str, limit: int = 160) -> str:
    cleaned = " ".join(message.split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


@dataclass
class DeliveryReport:
    timestamp: str = field(default_factory=_now_iso)
    phone_number: str = ""
    status: str = "unknown"
    destination: str = ""
    message_excerpt: str = ""
    ami_action_id: str = ""
    error: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "DeliveryReport":
        return cls(
            timestamp=_coerce_text(data.get("timestamp")) or _now_iso(),
            phone_number=_coerce_text(data.get("phone_number")),
            status=_coerce_text(data.get("status")) or "unknown",
            destination=_coerce_text(data.get("destination")),
            message_excerpt=_coerce_text(data.get("message_excerpt")),
            ami_action_id=_coerce_text(data.get("ami_action_id")),
            error=_coerce_text(data.get("error")),
            source=_coerce_text(data.get("source") or data.get("provider")),
        )


def _load_report_list(path: Path | str = REPORT_STORE_PATH) -> list[dict[str, Any]]:
    store_path = Path(path)
    if not store_path.exists():
        return []

    try:
        with store_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        log.warning("Report store JSON is invalid at %s: %s", store_path, exc)
        return []
    except OSError as exc:
        log.warning("Unable to read report store at %s: %s", store_path, exc)
        return []

    if not isinstance(data, list):
        log.warning("Report store at %s did not contain a JSON list", store_path)
        return []

    return [item for item in data if isinstance(item, dict)]


def _save_report_list(values: list[dict[str, Any]], path: Path | str = REPORT_STORE_PATH) -> Path:
    store_path = Path(path)
    _ensure_parent_dir(store_path)

    with store_path.open("w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")

    return store_path


def append_report(
    report: DeliveryReport | dict[str, Any] | None = None,
    *,
    timestamp: str | None = None,
    phone_number: str = "",
    status: str = "unknown",
    destination: str = "",
    message_excerpt: str = "",
    ami_action_id: str = "",
    error: str = "",
    source: str = "",
    provider: str = "",
    path: Path | str = REPORT_STORE_PATH,
) -> Path:
    records = _load_report_list(path)

    if report is None:
        report_obj = DeliveryReport(
            timestamp=timestamp or _now_iso(),
            phone_number=phone_number,
            status=status or "unknown",
            destination=destination,
            message_excerpt=_message_excerpt(message_excerpt),
            ami_action_id=ami_action_id,
            error=error,
            source=source or provider,
        )
    elif isinstance(report, DeliveryReport):
        report_obj = report
    else:
        report_obj = DeliveryReport.from_mapping(report)

    records.append(report_obj.to_dict())
    return _save_report_list(records, path)


def list_reports(path: Path | str = REPORT_STORE_PATH) -> list[DeliveryReport]:
    return [DeliveryReport.from_mapping(item) for item in _load_report_list(path)]


def filter_reports(
    *,
    status: str | None = None,
    phone_number: str | None = None,
    source: str | None = None,
    limit: int | None = None,
    path: Path | str = REPORT_STORE_PATH,
) -> list[DeliveryReport]:
    reports = list_reports(path)

    def _matches(report: DeliveryReport) -> bool:
        if status and report.status != status:
            return False
        if phone_number and report.phone_number != phone_number:
            return False
        if source and report.source != source:
            return False
        return True

    filtered = [report for report in reports if _matches(report)]
    if limit is not None and limit >= 0:
        return filtered[-limit:]
    return filtered


def summarize_reports(path: Path | str = REPORT_STORE_PATH) -> dict[str, Any]:
    reports = list_reports(path)
    counts = {"success": 0, "error": 0, "pending": 0, "unknown": 0}
    for report in reports:
        key = report.status if report.status in counts else "unknown"
        counts[key] += 1

    return {
        "total": len(reports),
        "status_counts": [
            {"status": "success", "count": counts["success"]},
            {"status": "error", "count": counts["error"]},
            {"status": "pending", "count": counts["pending"]},
            {"status": "unknown", "count": counts["unknown"]},
        ],
    }


def clear_old_reports(
    max_items: int | None,
    *,
    path: Path | str = REPORT_STORE_PATH,
) -> int:
    if max_items is None or max_items < 0:
        return 0

    records = _load_report_list(path)
    if len(records) <= max_items:
        return 0

    trimmed = records[-max_items:]
    removed = len(records) - len(trimmed)
    _save_report_list(trimmed, path)
    return removed