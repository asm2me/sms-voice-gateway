"""
FastAPI application – SMS webhook receiver.

Supported SMS provider webhooks:
  POST /sms/twilio      – Twilio SMS webhook (form-encoded)
  POST /sms/vonage      – Vonage / Nexmo SMS webhook (JSON)
  POST /sms/generic     – Generic JSON: {body, from, to, destination}

Health / debug endpoints:
  GET  /health          – service liveness + SIP + Redis ping
  GET  /cache/stats     – audio cache statistics
  POST /cache/evict     – evict expired audio files
  GET  /debug/call      – manually trigger a test call (guarded by webhook_secret)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
import threading
from typing import Annotated, Optional

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import ClientDisconnect
from pydantic import BaseModel

from .admin_reports import (
    batch_delete_inbox_messages,
    batch_delete_queue_items,
    batch_update_queue_item_status,
    delete_inbox_message,
    delete_queue_item,
    export_delivery_reports_csv,
    export_delivery_reports_xlsx,
    export_inbox_messages_csv,
    export_inbox_messages_xlsx,
    get_delivery_report_collector,
    get_queue_item,
    get_queue_store,
    paginate_items,
    query_inbox_messages,
    query_queue_items,
    record_delivery_report,
    record_queue_item,
)
from .cache import AudioCache, RateLimiter, get_redis
from .config import SIPAccount, SMPPAccount, Settings, SystemUser, get_system_user_permissions
from .config_store import (
    ADMIN_MANAGED_FIELDS,
    ensure_default_accounts,
    load_settings_from_store,
    save_settings_to_store,
)
from .pjsua2_service import SipAccountProfile, build_pjsua2_service
from .sms_handler import IncomingSMS, SMSGateway, _utc_now_iso
from .smpp_service import SMPPService
from .tts_service import TTSService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s – %(message)s",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
security = HTTPBasic()

_retry_worker_stop = Event()
_retry_worker_thread: Thread | None = None
_admin_test_send_jobs: dict[str, dict[str, object]] = {}
_admin_test_send_lock = threading.Lock()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_attempt_due(item) -> bool:
    next_attempt_at = getattr(item, "next_attempt_at", "") or ""
    if not str(next_attempt_at).strip():
        return True
    due_at = _parse_iso_datetime(str(next_attempt_at))
    if due_at is None:
        return True
    return due_at <= datetime.now(timezone.utc)


def _queue_item_status_finished(status_value: str) -> bool:
    return str(status_value or "").strip().lower() in {"delivered", "read", "failed", "cancelled", "canceled", "error", "answered", "completed"}


def _update_queue_item_attempt_metadata(
    item,
    *,
    attempts: int,
    status: str,
    last_error: str = "",
    next_attempt_at: str | None = None,
    sip_call_id: str = "",
    sip_account_id: str = "",
    ami_action_id: str = "",
    audio_path: str = "",
) -> None:
    item.attempts = max(0, int(attempts))
    item.status = status
    item.last_error = last_error or ""
    item.next_attempt_at = next_attempt_at
    if sip_call_id:
        item.sip_call_id = sip_call_id
    if sip_account_id:
        item.sip_account_id = sip_account_id
    if ami_action_id:
        item.ami_action_id = ami_action_id
    if audio_path:
        item.audio_path = audio_path
    item.updated_at = _utc_now_iso()


def _deliver_queue_item(settings: Settings, item) -> tuple[bool, str]:
    gateway = SMSGateway(settings)
    sms = IncomingSMS(
        body=item.body,
        destination=item.phone_number,
        provider=item.provider or "queued",
    )

    try:
        sip_account = next(
            (
                account
                for account in settings.sip_accounts
                if account.id == getattr(item, "sip_account_id", "") and account.enabled
            ),
            None,
        )
        if sip_account is None and item.provider == "smpp":
            sip_account = next(
                (
                    account
                    for account in settings.sip_accounts
                    if account.id == getattr(item, "sip_account_id", "")
                ),
                None,
            )
        if sip_account is not None:
            sms.smpp_username = next(
                (
                    account.username
                    for account in settings.smpp_accounts
                    if account.id == getattr(item, "provider", "") or account.username == getattr(item, "provider", "")
                ),
                "",
            )
        result = gateway.process(sms)
    except Exception as exc:
        return False, str(exc)

    item.sip_call_id = getattr(result, "sip_call_id", "") or getattr(item, "sip_call_id", "") or ""
    item.sip_account_id = getattr(result, "sip_account_id", "") or getattr(item, "sip_account_id", "") or ""
    item.audio_path = getattr(result, "audio_path", "") or getattr(item, "audio_path", "") or ""

    if result.success:
        item.status = "answered" if result.answered else ("completed" if result.read else "delivered")
        item.last_error = ""
        item.next_attempt_at = None
        item.updated_at = _utc_now_iso()
        return True, ""
    return False, result.error or str(result.details.get("pending_reason") or "Delivery failed")


def _retry_worker_loop() -> None:
    while not _retry_worker_stop.is_set():
        try:
            settings = load_settings_from_store()
            queue_store = get_queue_store(settings)
            now = datetime.now(timezone.utc)
            queue_items = queue_store.query_items(
                status="queued",
                limit=getattr(settings, "delivery_report_max_items", 1000),
            )
            retry_items = queue_store.query_items(
                status="retry_scheduled",
                limit=getattr(settings, "delivery_report_max_items", 1000),
            )
            answered_items = queue_store.query_items(
                status="answered",
                limit=getattr(settings, "delivery_report_max_items", 1000),
            )
            completed_items = queue_store.query_items(
                status="completed",
                limit=getattr(settings, "delivery_report_max_items", 1000),
            )
            due_items = []
            for item in queue_items + retry_items + answered_items + completed_items:
                if _queue_item_status_finished(getattr(item, "status", "")):
                    continue
                if getattr(item, "attempts", 0) >= max(1, int(getattr(item, "max_attempts", 1) or 1)):
                    continue
                if _next_attempt_due(item):
                    due_items.append(item)

            seen_ids: set[str] = set()
            for item in due_items:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                current_item = queue_store.get(item.id)
                if current_item is None:
                    continue
                if _que