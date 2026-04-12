from __future__ import annotations

import csv
import io
import json
import logging
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Deque, Optional

from openpyxl import Workbook

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


def _coerce_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _clean_preview(value: Any, limit: int = 160) -> str:
    text = " ".join(_coerce_text(value).split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _status_class(status: str) -> str:
    mapping = {
        "success": "success",
        "delivered": "success",
        "received": "success",
        "queued": "pending",
        "processing": "pending",
        "retry_scheduled": "warning",
        "pending": "pending",
        "failed": "error",
        "error": "error",
        "cancelled": "warning",
        "canceled": "warning",
    }
    return mapping.get((status or "").lower(), "unknown")


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
        payload["status_class"] = _status_class(self.status)
        payload["message_excerpt"] = _clean_preview(self.message)
        payload["destination"] = self.phone_number
        payload["source"] = self.provider
        return payload


@dataclass(slots=True)
class InboxMessage:
    id: str
    created_at: str
    updated_at: str
    from_number: str
    to_number: str
    provider: str
    body: str
    body_preview: str
    source: str = ""
    status: str = "received"
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["phone_number"] = self.from_number
        payload["destination"] = self.to_number
        payload["status_class"] = _status_class(self.status)
        return payload

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "InboxMessage":
        return cls(
            id=_coerce_text(data.get("id")),
            created_at=_coerce_text(data.get("created_at")) or _isoformat(_utc_now()),
            updated_at=_coerce_text(data.get("updated_at")) or _coerce_text(data.get("created_at")) or _isoformat(_utc_now()),
            from_number=_coerce_text(data.get("from_number") or data.get("phone_number")),
            to_number=_coerce_text(data.get("to_number") or data.get("destination")),
            provider=_coerce_text(data.get("provider") or data.get("source")),
            body=_coerce_text(data.get("body")),
            body_preview=_clean_preview(data.get("body_preview") or data.get("body")),
            source=_coerce_text(data.get("source")),
            status=_coerce_text(data.get("status")) or "received",
            last_error=_coerce_text(data.get("last_error")),
        )


@dataclass(slots=True)
class QueueItem:
    id: str
    created_at: str
    updated_at: str
    phone_number: str
    provider: str
    body: str
    body_preview: str
    status: str = "queued"
    attempts: int = 0
    max_attempts: int = 0
    retry_interval_seconds: int = 0
    next_attempt_at: str | None = None
    last_error: str = ""
    ami_action_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status_class"] = _status_class(self.status)
        return payload

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "QueueItem":
        body = _coerce_text(data.get("body"))
        return cls(
            id=_coerce_text(data.get("id")),
            created_at=_coerce_text(data.get("created_at")) or _isoformat(_utc_now()),
            updated_at=_coerce_text(data.get("updated_at")) or _coerce_text(data.get("created_at")) or _isoformat(_utc_now()),
            phone_number=_coerce_text(data.get("phone_number")),
            provider=_coerce_text(data.get("provider")),
            body=body,
            body_preview=_clean_preview(data.get("body_preview") or body),
            status=_coerce_text(data.get("status")) or "queued",
            attempts=_coerce_int(data.get("attempts")),
            max_attempts=_coerce_int(data.get("max_attempts")),
            retry_interval_seconds=_coerce_int(data.get("retry_interval_seconds")),
            next_attempt_at=_coerce_text(data.get("next_attempt_at")) or None,
            last_error=_coerce_text(data.get("last_error")),
            ami_action_id=_coerce_text(data.get("ami_action_id")) or None,
        )


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
            items = list(self._items)[-max(limit, 0) :]
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


class FileBackedInboxStore:
    def __init__(self, path: str, max_items: int = 1000) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._items: Deque[InboxMessage] = deque(maxlen=max_items)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")
            return
        try:
            for item in _safe_json_loads(self.path.read_text(encoding="utf-8")):
                self._items.append(InboxMessage.from_mapping(item))
        except Exception as exc:
            log.warning("Failed to load inbox store %s: %s", self.path, exc)

    def _persist_unlocked(self) -> None:
        items = [item.to_dict() for item in self._items]
        self.path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    def append(self, message: InboxMessage) -> InboxMessage:
        with self._lock:
            self._items.append(message)
            self._persist_unlocked()
        return message

    def list_messages(self, limit: int = 50) -> list[InboxMessage]:
        with self._lock:
            items = list(self._items)[-max(limit, 0) :]
        return list(reversed(items))

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items)
        counts = Counter(item.status for item in items)
        return {
            "total": len(items),
            "by_status": dict(sorted(counts.items())),
            "latest_timestamp": items[-1].created_at if items else None,
        }


class FileBackedQueueStore:
    def __init__(self, path: str, max_items: int = 1000) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._items: Deque[QueueItem] = deque(maxlen=max_items)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")
            return
        try:
            for item in _safe_json_loads(self.path.read_text(encoding="utf-8")):
                self._items.append(QueueItem.from_mapping(item))
        except Exception as exc:
            log.warning("Failed to load queue store %s: %s", self.path, exc)

    def _persist_unlocked(self) -> None:
        items = [item.to_dict() for item in self._items]
        self.path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    def append(self, item: QueueItem) -> QueueItem:
        with self._lock:
            self._items.append(item)
            self._persist_unlocked()
        return item

    def upsert(self, item: QueueItem) -> QueueItem:
        with self._lock:
            replaced = False
            for index, existing in enumerate(self._items):
                if existing.id == item.id:
                    self._items[index] = item
                    replaced = True
                    break
            if not replaced:
                self._items.append(item)
            self._persist_unlocked()
        return item

    def get(self, item_id: str) -> QueueItem | None:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    return item
        return None

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = deque((item for item in self._items if item.id != item_id), maxlen=self.max_items)
            removed = len(self._items) != before
            if removed:
                self._persist_unlocked()
        return removed

    def batch_delete(self, item_ids: list[str]) -> int:
        ids = {item_id for item_id in item_ids if item_id}
        if not ids:
            return 0
        with self._lock:
            before = len(self._items)
            self._items = deque((item for item in self._items if item.id not in ids), maxlen=self.max_items)
            removed = before - len(self._items)
            if removed:
                self._persist_unlocked()
        return removed

    def batch_update_status(self, item_ids: list[str], status: str) -> int:
        ids = {item_id for item_id in item_ids if item_id}
        if not ids:
            return 0
        updated = 0
        now = _isoformat(_utc_now())
        with self._lock:
            for index, existing in enumerate(self._items):
                if existing.id not in ids:
                    continue
                self._items[index] = QueueItem(
                    id=existing.id,
                    created_at=existing.created_at,
                    updated_at=now,
                    phone_number=existing.phone_number,
                    provider=existing.provider,
                    body=existing.body,
                    body_preview=existing.body_preview,
                    status=status,
                    attempts=existing.attempts,
                    max_attempts=existing.max_attempts,
                    retry_interval_seconds=existing.retry_interval_seconds,
                    next_attempt_at=existing.next_attempt_at,
                    last_error=existing.last_error,
                    ami_action_id=existing.ami_action_id,
                )
                updated += 1
            if updated:
                self._persist_unlocked()
        return updated

    def list_items(self, limit: int = 50) -> list[QueueItem]:
        with self._lock:
            items = list(self._items)[-max(limit, 0) :]
        return list(reversed(items))

    def query_items(
        self,
        *,
        search: str = "",
        status: str = "",
        provider: str = "",
        limit: int = 50,
    ) -> list[QueueItem]:
        search_term = search.strip().lower()
        status_term = status.strip().lower()
        provider_term = provider.strip().lower()
        with self._lock:
            items = list(self._items)
        filtered: list[QueueItem] = []
        for item in reversed(items):
            if search_term and search_term not in " ".join(
                [
                    item.id,
                    item.phone_number,
                    item.provider,
                    item.body,
                    item.body_preview,
                    item.status,
                    item.last_error,
                    item.ami_action_id or "",
                ]
            ).lower():
                continue
            if status_term and item.status.lower() != status_term:
                continue
            if provider_term and item.provider.lower() != provider_term:
                continue
            filtered.append(item)
            if limit > 0 and len(filtered) >= limit:
                break
        return filtered

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items)
        counts = Counter(item.status for item in items)
        return {
            "total": len(items),
            "by_status": dict(sorted(counts.items())),
            "latest_timestamp": items[-1].created_at if items else None,
        }


_report_collector: Optional[DeliveryReportCollector] = None
_inbox_store: FileBackedInboxStore | None = None
_queue_store: FileBackedQueueStore | None = None


def get_delivery_report_collector(settings: Settings) -> DeliveryReportCollector:
    global _report_collector
    if _report_collector is None:
        report_path = getattr(settings, "delivery_report_store_path", "") or ""
        if report_path:
            _report_collector = FileBackedDeliveryReportCollector(report_path)
        else:
            _report_collector = InMemoryDeliveryReportCollector()
    return _report_collector


def _default_inbox_path(settings: Settings) -> str:
    path = getattr(settings, "sms_inbox_store_path", "") or ""
    return path or str(Path("data") / "sms_inbox.json")


def _default_queue_path(settings: Settings) -> str:
    path = getattr(settings, "voice_queue_store_path", "") or ""
    return path or str(Path("data") / "voice_queue.json")


def get_inbox_store(settings: Settings) -> FileBackedInboxStore:
    global _inbox_store
    if _inbox_store is None:
        _inbox_store = FileBackedInboxStore(_default_inbox_path(settings))
    return _inbox_store


def get_queue_store(settings: Settings) -> FileBackedQueueStore:
    global _queue_store
    if _queue_store is None:
        _queue_store = FileBackedQueueStore(_default_queue_path(settings))
    return _queue_store


def record_inbox_message(
    settings: Settings,
    *,
    from_number: str,
    to_number: str,
    provider: str,
    body: str,
    source: str = "",
    status: str = "received",
    last_error: str = "",
) -> InboxMessage:
    now = _isoformat(_utc_now())
    message = InboxMessage(
        id=now,
        created_at=now,
        updated_at=now,
        from_number=from_number,
        to_number=to_number,
        provider=provider,
        body=body,
        body_preview=_clean_preview(body),
        source=source,
        status=status,
        last_error=last_error,
    )
    return get_inbox_store(settings).append(message)


def record_queue_item(
    settings: Settings,
    *,
    phone_number: str,
    provider: str,
    body: str,
    status: str = "queued",
    attempts: int = 0,
    max_attempts: int | None = None,
    retry_interval_seconds: int | None = None,
    next_attempt_at: str | None = None,
    last_error: str = "",
    ami_action_id: str | None = None,
    item_id: str | None = None,
) -> QueueItem:
    now = _isoformat(_utc_now())
    item = QueueItem(
        id=item_id or now,
        created_at=now,
        updated_at=now,
        phone_number=phone_number,
        provider=provider,
        body=body,
        body_preview=_clean_preview(body),
        status=status,
        attempts=attempts,
        max_attempts=max_attempts if max_attempts is not None else getattr(settings, "delivery_retry_count", 3) + 1,
        retry_interval_seconds=retry_interval_seconds if retry_interval_seconds is not None else getattr(settings, "delivery_retry_interval_seconds", 60),
        next_attempt_at=next_attempt_at,
        last_error=last_error,
        ami_action_id=ami_action_id,
    )
    return get_queue_store(settings).upsert(item)


def list_inbox_messages(settings: Settings, limit: int = 10) -> list[dict[str, Any]]:
    return [item.to_dict() for item in get_inbox_store(settings).list_messages(limit=limit)]


def list_queue_items(settings: Settings, limit: int = 10) -> list[dict[str, Any]]:
    return [item.to_dict() for item in get_queue_store(settings).list_items(limit=limit)]


def query_queue_items(
    settings: Settings,
    *,
    search: str = "",
    status: str = "",
    provider: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    return [
        item.to_dict()
        for item in get_queue_store(settings).query_items(
            search=search,
            status=status,
            provider=provider,
            limit=limit,
        )
    ]


def get_queue_item(settings: Settings, item_id: str) -> dict[str, Any] | None:
    item = get_queue_store(settings).get(item_id)
    return item.to_dict() if item else None


def delete_queue_item(settings: Settings, item_id: str) -> bool:
    return get_queue_store(settings).delete(item_id)


def batch_delete_queue_items(settings: Settings, item_ids: list[str]) -> int:
    return get_queue_store(settings).batch_delete(item_ids)


def batch_update_queue_item_status(settings: Settings, item_ids: list[str], status: str) -> int:
    return get_queue_store(settings).batch_update_status(item_ids, status)


def summarize_inbox(settings: Settings) -> dict[str, Any]:
    summary = get_inbox_store(settings).summary()
    return {
        "total": summary.get("total", 0),
        "detail": f"{summary.get('total', 0)} received messages stored",
        "latest_timestamp": summary.get("latest_timestamp"),
        "by_status": summary.get("by_status", {}),
        "unread_label": "Recent",
    }


def summarize_queue(settings: Settings) -> dict[str, Any]:
    summary = get_queue_store(settings).summary()
    by_status = summary.get("by_status", {})
    active_count = sum(by_status.get(key, 0) for key in ("queued", "processing", "retry_scheduled"))
    return {
        "total": summary.get("total", 0),
        "detail": f"{active_count} active jobs, {by_status.get('delivered', 0)} delivered, {by_status.get('failed', 0)} failed",
        "latest_timestamp": summary.get("latest_timestamp"),
        "by_status": by_status,
        "active_label": f"{active_count} active",
    }


def export_delivery_reports_csv(settings: Settings) -> bytes:
    reports = get_delivery_report_collector(settings).list_reports(limit=getattr(settings, "delivery_report_max_items", 1000))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "status", "provider", "phone_number", "destination", "message", "message_excerpt", "ami_action_id", "error"])
    for report in reports:
        writer.writerow(
            [
                report.get("timestamp", ""),
                report.get("status", ""),
                report.get("provider", report.get("source", "")),
                report.get("phone_number", ""),
                report.get("destination", ""),
                report.get("message", ""),
                report.get("message_excerpt", ""),
                report.get("ami_action_id", ""),
                report.get("error", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8")

def export_delivery_reports_xlsx(settings: Settings) -> bytes:
    reports = get_delivery_report_collector(settings).list_reports(limit=getattr(settings, "delivery_report_max_items", 1000))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Delivery Reports"
    sheet.append(["timestamp", "status", "provider", "phone_number", "destination", "message", "message_excerpt", "ami_action_id", "error"])
    for report in reports:
        sheet.append(
            [
                report.get("timestamp", ""),
                report.get("status", ""),
                report.get("provider", report.get("source", "")),
                report.get("phone_number", ""),
                report.get("destination", ""),
                report.get("message", ""),
                report.get("message_excerpt", ""),
                report.get("ami_action_id", ""),
                report.get("error", ""),
            ]
        )
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()

def export_inbox_messages_csv(settings: Settings) -> bytes:
    messages = list_inbox_messages(settings, limit=getattr(settings, "delivery_report_max_items", 1000))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["created_at", "updated_at", "from_number", "to_number", "provider", "status", "body", "body_preview", "source", "last_error"])
    for message in messages:
        writer.writerow(
            [
                message.get("created_at", ""),
                message.get("updated_at", ""),
                message.get("phone_number", ""),
                message.get("destination", ""),
                message.get("provider", ""),
                message.get("status", ""),
                message.get("body", ""),
                message.get("body_preview", ""),
                message.get("source", ""),
                message.get("last_error", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8")

def export_inbox_messages_xlsx(settings: Settings) -> bytes:
    messages = list_inbox_messages(settings, limit=getattr(settings, "delivery_report_max_items", 1000))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "SMS Inbox"
    sheet.append(["created_at", "updated_at", "from_number", "to_number", "provider", "status", "body", "body_preview", "source", "last_error"])
    for message in messages:
        sheet.append(
            [
                message.get("created_at", ""),
                message.get("updated_at", ""),
                message.get("phone_number", ""),
                message.get("destination", ""),
                message.get("provider", ""),
                message.get("status", ""),
                message.get("body", ""),
                message.get("body_preview", ""),
                message.get("source", ""),
                message.get("last_error", ""),
            ]
        )
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()

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