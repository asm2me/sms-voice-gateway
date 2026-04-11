from __future__ import annotations

import json
import logging
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Deque, Optional

from .config import Settings

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_json_loads(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    return data if isinstance(data, list) else []


@dataclass(slots=True)
class DeliveryReport:
    timestamp: str
    status: str
    provider: str
    phone_number: str
    message: str = ""
    error: str | None = None
    ami_action_id: str | None = None
    audio_cached: bool | None = None
    text_spoken: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp
        return payload


class DeliveryReportCollector:
    def record(self, report: DeliveryReport) -> None:
        raise NotImplementedError

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    def summary(self) -> dict[str, Any]:
        raise NotImplementedError


class InMemoryDeliveryReportCollector(DeliveryReportCollector):
    def __init__(self, max_items: int = 500) -> None:
        self._items: Deque[DeliveryReport] = deque(maxlen=max_items)
        self._lock = Lock()

    def record(self, report: DeliveryReport) -> None:
        with self._lock:
            self._items.append(report)

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)[-max(limit, 0):]
        return [item.to_dict() for item in reversed(items)]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items)
        counts = Counter(item.status for item in items)
        return {
            "total": len(items),
            "by_status": dict(sorted(counts.items())),
            "latest_timestamp": items[-1].timestamp if items else None,
        }


class FileBackedDeliveryReportCollector(DeliveryReportCollector):
    def __init__(self, path: str, max_items: int = 1000) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._memory = InMemoryDeliveryReportCollector(max_items=max_items)

        if self.path.exists():
            try:
                for item in _safe_json_loads(self.path.read_text(encoding="utf-8")):
                    report = DeliveryReport(
                        timestamp=str(item.get("timestamp") or _isoformat(_utc_now())),
                        status=str(item.get("status") or "unknown"),
                        provider=str(item.get("provider") or "unknown"),
                        phone_number=str(item.get("phone_number") or ""),
                        message=str(item.get("message") or ""),
                        error=item.get("error"),
                        ami_action_id=item.get("ami_action_id"),
                        audio_cached=item.get("audio_cached"),
                        text_spoken=item.get("text_spoken"),
                        details=item.get("details") if isinstance(item.get("details"), dict) else None,
                    )
                    self._memory.record(report)
            except Exception as exc:
                log.warning("Failed to load delivery report store %s: %s", self.path, exc)

    def _persist(self) -> None:
        with self._lock:
            items = self._memory.list_reports(limit=self.max_items)
            self.path.write_text(json.dumps(list(reversed(items)), indent=2, ensure_ascii=False), encoding="utf-8")

    def record(self, report: DeliveryReport) -> None:
        self._memory.record(report)
        try:
            self._persist()
        except Exception as exc:
            log.warning("Failed to persist delivery report: %s", exc)

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._memory.list_reports(limit=limit)

    def summary(self) -> dict[str, Any]:
        return self._memory.summary()


_report_collector: Optional[DeliveryReportCollector] = None


def get_delivery_report_collector(settings: Settings) -> DeliveryReportCollector:
    global _report_collector
    if _report_collector is None:
        report_path = getattr(settings, "delivery_report_store_path", "") or ""
        if report_path:
            _report_collector = FileBackedDeliveryReportCollector(report_path)
        else:
            _report_collector = InMemoryDeliveryReportCollector()
    return _report_collector


def record_delivery_report(
    settings: Settings,
    *,
    status: str,
    provider: str,
    phone_number: str,
    message: str = "",
    error: str | None = None,
    ami_action_id: str | None = None,
    audio_cached: bool | None = None,
    text_spoken: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    report = DeliveryReport(
        timestamp=_isoformat(_utc_now()),
        status=status,
        provider=provider,
        phone_number=phone_number,
        message=message,
        error=error,
        ami_action_id=ami_action_id,
        audio_cached=audio_cached,
        text_spoken=text_spoken,
        details=details,
    )
    get_delivery_report_collector(settings).record(report)