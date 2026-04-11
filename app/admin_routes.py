from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .admin_reports import get_delivery_report_collector
from .config import Settings, get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def dep_settings() -> Settings:
    return get_settings()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("secret", "password", "token", "key", "credential"))


def _serialize_setting_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@router.get("/config")
async def get_config_snapshot(settings: Settings = Depends(dep_settings)):
    fields: list[dict[str, Any]] = []
    for name in settings.model_fields:
        value = getattr(settings, name)
        is_secret = _is_secret_field(name)
        fields.append(
            {
                "name": name,
                "value": None if is_secret else _serialize_setting_value(value),
                "display_value": "••••••" if is_secret and value else _serialize_setting_value(value),
                "is_secret": is_secret,
                "is_set": value not in (None, ""),
                "type": type(value).__name__,
            }
        )

    return {
        "generated_at": _utc_now_iso(),
        "status": "ok",
        "settings": fields,
    }


@router.get("/reports")
async def get_delivery_reports(
    settings: Settings = Depends(dep_settings),
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    collector = get_delivery_report_collector(settings)
    reports = collector.list_reports(limit=limit)
    summary = collector.summary()
    return {
        "generated_at": _utc_now_iso(),
        "status": "ok",
        "summary": summary,
        "reports": reports,
    }


@router.get("/reports/{report_id}")
async def get_delivery_report(report_id: str, settings: Settings = Depends(dep_settings)):
    collector = get_delivery_report_collector(settings)
    reports = collector.list_reports(limit=500)
    for report in reports:
        if report.get("ami_action_id") == report_id or report.get("timestamp") == report_id:
            return {"generated_at": _utc_now_iso(), "status": "ok", "report": report}
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")