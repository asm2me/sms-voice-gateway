"""
FastAPI application – SMS webhook receiver.

Supported SMS provider webhooks:
  POST /sms/twilio      – Twilio SMS webhook (form-encoded)
  POST /sms/vonage      – Vonage / Nexmo SMS webhook (JSON)
  POST /sms/generic     – Generic JSON: {body, from, to, destination}

Health / debug endpoints:
  GET  /health          – service liveness + AMI ping + Redis ping
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
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .admin_reports import (
    export_delivery_reports_csv,
    export_delivery_reports_xlsx,
    export_inbox_messages_csv,
    export_inbox_messages_xlsx,
    get_delivery_report_collector,
    record_delivery_report,
)
from .cache import AudioCache, RateLimiter, get_redis
from .config import SIPAccount, SMPPAccount, Settings
from .config_store import (
    ADMIN_MANAGED_FIELDS,
    ensure_default_accounts,
    load_settings_from_store,
    save_settings_to_store,
)
from .pjsua2_service import build_pjsua2_service
from .sms_handler import IncomingSMS, SMSGateway
from .smpp_service import SMPPService

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


def _retry_worker_loop() -> None:
    while not _retry_worker_stop.is_set():
        try:
            settings = load_settings_from_store()
            collector = get_delivery_report_collector(settings)
            if hasattr(collector, "list_reports"):
                pass
        except Exception as exc:
            log.debug("Retry worker loop issue: %s", exc)
        _retry_worker_stop.wait(5)


def _start_retry_worker() -> None:
    global _retry_worker_thread
    if _retry_worker_thread and _retry_worker_thread.is_alive():
        return
    _retry_worker_stop.clear()
    _retry_worker_thread = Thread(target=_retry_worker_loop, name="retry-worker", daemon=True)
    _retry_worker_thread.start()
    log.info("Retry worker started")


def _stop_retry_worker() -> None:
    _retry_worker_stop.set()
    if _retry_worker_thread and _retry_worker_thread.is_alive():
        _retry_worker_thread.join(timeout=2)
    log.info("Retry worker stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = ensure_default_accounts(load_settings_from_store())
    smpp_service = SMPPService(settings)
    app.state.smpp_service = smpp_service
    log.info(
        "SMS Voice Gateway starting (TTS=%s, SIP accounts=%d, SMPP=%s:%d enabled=%s)",
        settings.tts_provider,
        len(settings.sip_accounts),
        settings.smpp_host,
        settings.smpp_port,
        settings.smpp_enabled,
    )
    try:
        smpp_service.start()
    except Exception:
        log.exception("Failed to start SMPP listener")
    _start_retry_worker()
    yield
    _stop_retry_worker()
    try:
        smpp_service.stop()
    except Exception:
        log.exception("Failed to stop SMPP listener")
    log.info("SMS Voice Gateway stopped")


app = FastAPI(
    title="SMS Voice Gateway",
    description="Converts incoming SMS to TTS and delivers via SIP/Asterisk call",
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def dep_settings() -> Settings:
    return ensure_default_accounts(load_settings_from_store())


def dep_gateway(settings: Annotated[Settings, Depends(dep_settings)]) -> SMSGateway:
    return SMSGateway(settings)


def dep_admin_credentials(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    settings: Settings = Depends(dep_settings),
) -> None:
    if not (
        credentials.username == settings.admin_username
        and credentials.password == settings.admin_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("secret", "password", "token", "key", "credential"))


def _build_setting_items(settings: Settings, field_specs: list[tuple[str, str]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for name, label in field_specs:
        value = getattr(settings, name)
        value_type = type(value).__name__
        items.append(
            {
                "name": name,
                "label": label,
                "value": "••••••" if _is_secret_field(name) and value else "" if value is None else str(value),
                "raw_value": "" if value is None else str(value),
                "type": value_type,
                "is_secret": _is_secret_field(name),
                "is_bool": value_type == "bool",
                "is_integer": value_type == "int",
                "is_number": value_type in {"float"},
            }
        )
    return items


def _report_context(settings: Settings) -> tuple[dict, list[dict]]:
    collector = get_delivery_report_collector(settings)
    summary = collector.summary()
    recent_reports = collector.list_reports(limit=10)
    status_counts = {item["status"]: item["count"] for item in summary.get("status_counts", [])}
    report_summary = {
        "total": summary.get("total", 0),
        "status_counts": [
            {"status": "success", "count": status_counts.get("success", 0)},
            {"status": "error", "count": status_counts.get("error", 0)},
            {"status": "pending", "count": status_counts.get("pending", 0)},
            {"status": "unknown", "count": status_counts.get("unknown", 0)},
        ],
    }
    return report_summary, recent_reports


def _config_items(settings: Settings) -> list[dict[str, str]]:
    return [
        {"label": "TTS Provider", "display_value": settings.tts_provider, "visibility": "public"},
        {"label": "TTS Language", "display_value": settings.tts_language, "visibility": "public"},
        {"label": "TTS Voice", "display_value": settings.tts_voice, "visibility": "public"},
        {"label": "Speaking Rate", "display_value": str(settings.tts_speaking_rate), "visibility": "public"},
        {"label": "Audio Encoding", "display_value": settings.tts_audio_encoding, "visibility": "public"},
        {"label": "SIP Accounts", "display_value": str(len(settings.sip_accounts)), "visibility": "public"},
        {"label": "Default SIP Account", "display_value": next((account.label or account.id for account in settings.sip_accounts if account.default_for_outbound), "—"), "visibility": "public"},
        {"label": "SMPP Accounts", "display_value": str(len(settings.smpp_accounts)), "visibility": "public"},
        {"label": "SMPP Host", "display_value": settings.smpp_host, "visibility": "public"},
        {"label": "SMPP Port", "display_value": str(settings.smpp_port), "visibility": "public"},
        {"label": "SMPP Enabled", "display_value": "enabled" if settings.smpp_enabled else "disabled", "visibility": "public"},
        {"label": "Outbound Caller ID", "display_value": settings.outbound_caller_id, "visibility": "public"},
        {"label": "Retry Count", "display_value": str(settings.delivery_retry_count), "visibility": "public"},
        {"label": "Retry Interval (s)", "display_value": str(settings.delivery_retry_interval_seconds), "visibility": "public"},
        {"label": "Redis URL", "display_value": settings.redis_url, "visibility": "public"},
        {"label": "Audio Cache Directory", "display_value": settings.audio_cache_dir, "visibility": "public"},
        {"label": "Webhook Secret", "display_value": "configured" if settings.webhook_secret else "not configured", "visibility": "protected"},
    ]


def _build_queue_context(settings: Settings) -> tuple[dict, list[dict], dict, list[dict]]:
    from .admin_reports import list_inbox_messages, list_queue_items, summarize_inbox, summarize_queue

    inbox_summary = summarize_inbox(settings)
    queue_summary = summarize_queue(settings)
    recent_inbox_messages = list_inbox_messages(settings, limit=10)
    recent_queue_items = list_queue_items(settings, limit=10)
    return inbox_summary, recent_inbox_messages, queue_summary, recent_queue_items

def _build_live_call_context(settings: Settings) -> dict:
    service = build_pjsua2_service(settings)
    status = service.status_detail()
    active_calls: list[dict] = []
    current_account_id = status.get("current_account_id", "")

    if current_account_id:
        active_calls.append(
            {
                "channel": f"sip:{current_account_id}",
                "caller_id_num": "",
                "caller_id_name": current_account_id,
                "connected_line_num": "",
                "connected_line_name": "",
                "state": "registered" if status.get("registered") else "idle",
                "context": "pjsua2",
                "extension": "",
                "application": "direct-sip-ua",
                "duration": "",
            }
        )

    return {
        "active_count": len(active_calls),
        "items": active_calls,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "engine": "pjsua2",
        "available": status.get("available", False),
        "registered": status.get("registered", False),
        "import_error": status.get("import_error", ""),
    }


def _record_gateway_result(provider: str, result, *, phone_number: str = "", message: str = "") -> None:
    try:
        record_delivery_report(
            dep_settings(),
            status="success" if result.success else "error",
            provider=provider,
            phone_number=result.phone_number or phone_number,
            message=message,
            error=result.error or None,
            ami_action_id=result.ami_action_id or None,
            audio_cached=result.was_cached if hasattr(result, "was_cached") else None,
            text_spoken=result.text_spoken or None,
            details=result.details or None,
        )
    except Exception as exc:
        log.debug("Unable to persist delivery report: %s", exc)


def _save_admin_config(form, keys: list[str]) -> Settings:
    current = ensure_default_accounts(load_settings_from_store())
    data = current.model_dump()
    field_types = {name: field.annotation for name, field in Settings.model_fields.items()}

    for key in keys:
        if key not in form:
            continue

        raw = str(form.get(key, ""))
        annotation = field_types.get(key)

        if annotation is bool:
            data[key] = raw.strip().lower() in {"1", "true", "yes", "on"}
        elif annotation is int:
            stripped = raw.strip()
            if stripped != "":
                data[key] = int(stripped)
        elif annotation is float:
            stripped = raw.strip()
            if stripped != "":
                data[key] = float(stripped)
        elif raw != "":
            data[key] = raw

    updated = Settings(**data)
    save_settings_to_store(updated)
    return updated


def _slugify_identifier(value: str, fallback: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "").strip())
    compact = "-".join(part for part in cleaned.split("-") if part)
    return compact[:50] or fallback


def _form_bool(form, key: str) -> bool:
    return str(form.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}


def _save_account_collections(
    *,
    sip_accounts: list[SIPAccount] | None = None,
    smpp_accounts: list[SMPPAccount] | None = None,
    smpp_sip_assignments: dict[str, str] | None = None,
) -> Settings:
    current = ensure_default_accounts(load_settings_from_store())
    update_data = {}
    if sip_accounts is not None:
        update_data["sip_accounts"] = sip_accounts
    if smpp_accounts is not None:
        update_data["smpp_accounts"] = smpp_accounts
    if smpp_sip_assignments is not None:
        update_data["smpp_sip_assignments"] = smpp_sip_assignments
    updated = ensure_default_accounts(current.model_copy(update=update_data))
    save_settings_to_store(updated)
    return updated


def _build_sip_account_from_form(form) -> SIPAccount:
    label = str(form.get("label", "")).strip()
    host = str(form.get("host", "")).strip()
    username = str(form.get("username", "")).strip()
    account_id = str(form.get("account_id", "")).strip() or _slugify_identifier(label or username or host, "sip-account")
    port_raw = str(form.get("port", "5060")).strip()
    return SIPAccount(
        id=account_id,
        label=label or account_id,
        host=host,
        username=username,
        password=str(form.get("password", "")).strip(),
        transport=str(form.get("transport", "udp")).strip() or "udp",
        port=int(port_raw or "5060"),
        domain=str(form.get("domain", "")).strip(),
        display_name=str(form.get("display_name", "")).strip(),
        from_user=str(form.get("from_user", "")).strip(),
        from_domain=str(form.get("from_domain", "")).strip(),
        enabled=_form_bool(form, "enabled"),
        default_for_outbound=_form_bool(form, "default_for_outbound"),
        register=_form_bool(form, "register"),
        outbound_proxy=str(form.get("outbound_proxy", "")).strip(),
    )


def _build_smpp_account_from_form(form) -> SMPPAccount:
    label = str(form.get("label", "")).strip()
    username = str(form.get("username", "")).strip()
    account_id = str(form.get("account_id", "")).strip() or _slugify_identifier(label or username, "smpp-account")
    return SMPPAccount(
        id=account_id,
        label=label or account_id,
        username=username,
        password=str(form.get("password", "")).strip(),
        enabled=_form_bool(form, "enabled"),
        default_for_inbound=_form_bool(form, "default_for_inbound"),
        default_sip_account_id=str(form.get("default_sip_account_id", "")).strip(),
    )


def _admin_context(
    request: Request,
    settings: Settings,
    *,
    active_section: str,
    success_message: str | None = None,
    health_context: dict | None = None,
) -> dict:
    report_summary, recent_reports = _report_context(settings)
    sms_inbox_summary, recent_inbox_messages, queue_summary, recent_queue_items = _build_queue_context(settings)
    live_calls = _build_live_call_context(settings)
    context = {
        "request": request,
        "active_section": active_section,
        "config_snapshot": {"source": "saved settings" if success_message else "runtime settings", "items": _config_items(settings)},
        "report_summary": report_summary,
        "recent_reports": recent_reports,
        "sms_inbox_summary": sms_inbox_summary,
        "recent_inbox_messages": recent_inbox_messages,
        "queue_summary": queue_summary,
        "recent_queue_items": recent_queue_items,
        "live_calls": live_calls,
        "basic_settings": _build_setting_items(
            settings,
            [
                ("tts_provider", "TTS Provider"),
                ("tts_language", "TTS Language"),
                ("tts_voice", "TTS Voice"),
                ("tts_speaking_rate", "Speaking Rate"),
                ("tts_audio_encoding", "Audio Encoding"),
                ("phone_regex", "Phone Regex"),
                ("strip_call_prefix", "Strip Call Prefix"),
                ("playback_repeats", "Playback Repeats"),
                ("playback_pause_ms", "Playback Pause (ms)"),
            ],
        ),
        "security_settings": _build_setting_items(
            settings,
            [
                ("webhook_secret", "Webhook Secret"),
                ("rate_limit_hourly", "Rate Limit Hourly"),
                ("rate_limit_daily", "Rate Limit Daily"),
            ],
        ),
        "provider_settings": _build_setting_items(
            settings,
            [
                ("google_credentials_json", "Google Credentials JSON"),
                ("aws_access_key_id", "AWS Access Key ID"),
                ("aws_secret_access_key", "AWS Secret Access Key"),
                ("aws_region", "AWS Region"),
                ("aws_polly_voice_id", "AWS Polly Voice ID"),
                ("aws_polly_engine", "AWS Polly Engine"),
                ("openai_api_key", "OpenAI API Key"),
                ("openai_tts_model", "OpenAI TTS Model"),
                ("openai_tts_voice", "OpenAI TTS Voice"),
                ("elevenlabs_api_key", "ElevenLabs API Key"),
                ("elevenlabs_voice_id", "ElevenLabs Voice ID"),
            ],
        ),
        "telephony_settings": _build_setting_items(
            settings,
            [
                ("outbound_caller_id", "Outbound Caller ID"),
                ("call_answer_timeout", "Call Answer Timeout"),
                ("smpp_enabled", "SMPP Enabled"),
                ("smpp_host", "SMPP Host"),
                ("smpp_port", "SMPP Port"),
                ("smpp_username", "Legacy SMPP Username"),
                ("smpp_password", "Legacy SMPP Password"),
            ],
        ),
        "storage_settings": _build_setting_items(
            settings,
            [
                ("audio_cache_dir", "Audio Cache Directory"),
                ("asterisk_sounds_dir", "Asterisk Sounds Directory"),
                ("audio_cache_ttl", "Audio Cache TTL"),
                ("delivery_retry_count", "Delivery Retry Count"),
                ("delivery_retry_interval_seconds", "Delivery Retry Interval Seconds"),
                ("redis_url", "Redis URL"),
                ("redis_prefix", "Redis Prefix"),
                ("delivery_report_store_path", "Delivery Report Store Path"),
                ("delivery_report_max_items", "Delivery Report Max Items"),
            ],
        ),
        "system_settings": _build_setting_items(
            settings,
            [
                ("host", "Host"),
                ("port", "Port"),
                ("debug", "Debug"),
                ("admin_username", "Admin Username"),
                ("admin_password", "Admin Password"),
            ],
        ),
        "config_groups": [
            {"key": "basic", "label": "Core Voice Routing", "description": "TTS, parsing, and playback behavior"},
            {"key": "security", "label": "Security and Access", "description": "Webhook verification and rate limiting"},
            {"key": "providers", "label": "Speech Provider Credentials", "description": "Cloud/provider credentials and engine options"},
            {"key": "telephony", "label": "Telephony and Gateway Routing", "description": "Direct SIP UA, SMPP listener, and dialing behavior"},
            {"key": "storage", "label": "Storage and Persistence", "description": "Cache, Redis, reports, and retry persistence"},
            {"key": "system", "label": "System Bootstrap", "description": "Minimal env/bootstrap values that start the admin"},
        ],
        "report_clear_supported": hasattr(get_delivery_report_collector(settings), "clear_old_reports"),
        "health": health_context or _build_health_context(settings),
        "sip_accounts": [account.model_dump() for account in settings.sip_accounts],
        "smpp_accounts": [account.model_dump() for account in settings.smpp_accounts],
        "smpp_sip_assignments": [
            {
                "smpp_username": smpp_username,
                "sip_account_id": sip_account_id,
                "smpp_label": next((account.label or account.id for account in settings.smpp_accounts if account.username == smpp_username), smpp_username),
                "sip_label": next((account.label or account.id for account in settings.sip_accounts if account.id == sip_account_id), sip_account_id),
            }
            for smpp_username, sip_account_id in (settings.smpp_sip_assignments or {}).items()
        ],
        "bootstrap_fields": ["host", "port", "debug", "admin_username", "admin_password"],
        "admin_managed_fields": sorted(ADMIN_MANAGED_FIELDS),
    }
    if success_message:
        context["success_message"] = success_message
    return context


def _build_health_context(settings: Settings) -> dict:
    checked_at = time.time()
    services: list[dict] = []
    dependencies: list[dict] = []
    healthy_count = 0
    total_count = 0

    def add_service(name: str, category: str, ok: bool, summary: str, details: list[str] | None = None, notes: list[str] | None = None) -> None:
        nonlocal healthy_count, total_count
        total_count += 1
        if ok:
            healthy_count += 1
        status_class = "success" if ok else "danger"
        status_label = "Healthy" if ok else "Degraded"
        services.append(
            {
                "name": name,
                "category": category,
                "status_class": status_class,
                "status_label": status_label,
                "summary": summary,
                "details": details or [],
                "notes": notes or [],
            }
        )
        dependencies.append(
            {
                "label": name,
                "status_class": status_class,
                "status_label": status_label,
                "detail": summary,
            }
        )

    add_service(
        "API / App",
        "Application",
        True,
        "FastAPI process is serving requests.",
        details=[
            f"Host: {settings.host}",
            f"Port: {settings.port}",
            f"Debug mode: {'enabled' if settings.debug else 'disabled'}",
        ],
    )

    try:
        redis_ok = bool(get_redis(settings).ping())
        add_service(
            "Redis",
            "Cache",
            redis_ok,
            "Redis ping succeeded." if redis_ok else "Redis ping failed.",
            details=[f"URL: {settings.redis_url}"],
        )
    except Exception as exc:
        add_service(
            "Redis",
            "Cache",
            False,
            f"Redis ping failed: {exc}",
            details=[f"URL: {settings.redis_url}"],
        )

    sip_status = build_pjsua2_service(settings).status_detail()
    sip_ok = bool(sip_status.get("available")) and bool(sip_status.get("registered"))
    sip_summary = (
        f"Direct SIP UA is registered with account {sip_status.get('current_account_id', 'unknown')}."
        if sip_ok
        else "Direct SIP UA is not registered."
    )
    sip_details = [
        f"Engine: {sip_status.get('engine', 'pjsua2')}",
        f"Available: {'yes' if sip_status.get('available') else 'no'}",
        f"Registered: {'yes' if sip_status.get('registered') else 'no'}",
        f"Current account: {sip_status.get('current_account_id') or '—'}",
    ]
    if sip_status.get("import_error"):
        sip_details.append(f"Import error: {sip_status.get('import_error')}")

    add_service(
        "Direct SIP UA",
        "Telephony",
        sip_ok,
        sip_summary,
        details=sip_details,
        notes=[
            "Direct outbound delivery now uses the PJSUA2 runtime instead of Asterisk Manager Interface originate.",
            "The PJSUA2 Python bindings must be installed in the deployment environment.",
            "A SIP account must be mapped to the authenticated SMPP username for outbound delivery to work.",
        ],
    )

    smpp_service = getattr(app.state, "smpp_service", None)
    smpp_enabled = bool(settings.smpp_enabled)
    smpp_listening = bool(smpp_service and getattr(smpp_service, "is_listening", False))
    smpp_last_error = getattr(smpp_service, "last_error", "") if smpp_service else ""
    smpp_ok = smpp_enabled and smpp_listening
    smpp_summary = (
        "SMPP listener is enabled and listening."
        if smpp_ok
        else "SMPP listener is enabled but not listening."
        if smpp_enabled
        else "SMPP listener is disabled."
    )
    smpp_details = [
        f"Target: {settings.smpp_host}:{settings.smpp_port}",
        f"Enabled: {'yes' if smpp_enabled else 'no'}",
        f"Listening: {'yes' if smpp_listening else 'no'}",
    ]
    if smpp_last_error:
        smpp_details.append(f"Last error: {smpp_last_error}")

    add_service(
        "SMPP",
        "Messaging",
        smpp_ok,
        smpp_summary,
        details=smpp_details,
        notes=[
            "SMPP health is only healthy when the listener is actually bound and accepting connections.",
            "If this gateway runs in Docker, expose the SMPP port from the container to the host.",
            "After enabling SMPP in the admin UI, restart the app/container if your process has not rebound the listener yet.",
        ],
    )

    config_store_path = BASE_DIR / "data" / "config.json"
    try:
        config_exists = config_store_path.exists()
        load_settings_from_store()
        add_service(
            "Config Store",
            "Persistence",
            True,
            "Settings store is readable.",
            details=[
                f"Path: {config_store_path}",
                f"Exists: {'yes' if config_exists else 'no'}",
            ],
        )
    except Exception as exc:
        add_service(
            "Config Store",
            "Persistence",
            False,
            f"Settings store access failed: {exc}",
            details=[f"Path: {config_store_path}"],
        )

    report_store_path = Path(settings.delivery_report_store_path) if settings.delivery_report_store_path else BASE_DIR / "data" / "reports.json"
    try:
        collector = get_delivery_report_collector(settings)
        summary = collector.summary()
        add_service(
            "Report Store",
            "Persistence",
            True,
            "Report storage is reachable.",
            details=[
                f"Path: {report_store_path}",
                f"Stored reports: {summary.get('total', 0)}",
            ],
        )
    except Exception as exc:
        add_service(
            "Report Store",
            "Persistence",
            False,
            f"Report store access failed: {exc}",
            details=[f"Path: {report_store_path}"],
        )

    tts_ready = bool(settings.tts_provider)
    add_service(
        "TTS Provider",
        "Speech",
        tts_ready,
        f"TTS provider configured as {settings.tts_provider}." if tts_ready else "No TTS provider configured.",
        details=[
            f"Provider: {settings.tts_provider}",
            f"Language: {settings.tts_language}",
            f"Voice: {settings.tts_voice}",
            f"Audio encoding: {settings.tts_audio_encoding}",
        ],
    )

    overall_ok = healthy_count == total_count
    restart_actions = _restart_actions(settings)
    return {
        "overall_class": "success" if overall_ok else "warning",
        "overall_label": "Healthy" if overall_ok else "Degraded",
        "checked_at_display": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(checked_at)),
        "summary": {
            "title": "Gateway health overview",
            "detail": f"{healthy_count} of {total_count} services are healthy.",
        },
        "runtime": {
            "uptime_display": "Current session",
            "last_restart_display": "Not tracked",
        },
        "summary_cards": [
            {"label": "Healthy Services", "value": str(healthy_count), "detail": "Passing checks", "class": "success"},
            {"label": "Total Services", "value": str(total_count), "detail": "Monitored components", "class": "unknown"},
            {"label": "Redis", "value": "Online" if any(d['label'] == 'Redis' and d['status_label'] == 'Healthy' for d in dependencies) else "Offline", "detail": settings.redis_url, "class": "success" if any(d['label'] == 'Redis' and d['status_label'] == 'Healthy' for d in dependencies) else "danger"},
            {"label": "Direct SIP UA", "value": "Registered" if any(d['label'] == 'Direct SIP UA' and d['status_label'] == 'Healthy' for d in dependencies) else "Not Registered", "detail": next((d["detail"] for d in dependencies if d["label"] == "Direct SIP UA"), "PJSUA2"), "class": "success" if any(d['label'] == 'Direct SIP UA' and d['status_label'] == 'Healthy' for d in dependencies) else "danger"},
            {"label": "SMPP", "value": "Listening" if smpp_ok else "Enabled / Not Listening" if smpp_enabled else "Disabled", "detail": f"{settings.smpp_host}:{settings.smpp_port}", "class": "success" if smpp_ok else "warning" if smpp_enabled else "danger"},
        ],
        "services": services,
        "dependencies": dependencies,
        "notes": [
            "Restart controls are safety-gated and may be disabled depending on the runtime environment.",
            "Direct SIP UA health depends on the PJSUA2 runtime, valid SIP credentials, and successful account registration.",
            "Configuration changes are loaded from persistent storage per request and apply automatically.",
            "SMPP support is a lightweight listener for inbound bind/connect verification and should be paired with a production SMPP stack if you need full message routing.",
        ],
        "actions": {
            "refresh": {"enabled": True, "reason": ""},
            "restart_app": next(({"enabled": action["supported"], "reason": action["disabled_reason"]} for action in restart_actions if action["key"] == "app"), {"enabled": False, "reason": "Unavailable"}),
            "restart_compose": next(({"enabled": action["supported"], "reason": action["disabled_reason"]} for action in restart_actions if action["key"] == "gateway"), {"enabled": False, "reason": "Unavailable"}),
        },
        "restart_actions": restart_actions,
    }


def _runtime_command_available(command: str) -> bool:
    from shutil import which

    return which(command) is not None


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or "container" in os.environ.get("RUNNING_IN_CONTAINER", "").lower()


def _restart_actions(settings: Settings) -> list[dict]:
    actions: list[dict] = []
    available = _runtime_command_available("docker") and _runtime_command_available("docker-compose")
    compose_path = BASE_DIR / "docker-compose.yml"
    if available and compose_path.exists():
        actions.append(
            {
                "key": "gateway",
                "label": "Restart Gateway Container",
                "description": "Uses docker-compose restart gateway from the project root.",
                "supported": True,
                "disabled_reason": "",
                "safety": "Requires docker-compose CLI access and local project checkout.",
            }
        )
        actions.append(
            {
                "key": "redis",
                "label": "Restart Redis Container",
                "description": "Uses docker-compose restart redis from the project root.",
                "supported": True,
                "disabled_reason": "",
                "safety": "Requires docker-compose CLI access and local project checkout.",
            }
        )
    else:
        reason = "docker-compose CLI is unavailable or docker-compose.yml was not found."
        actions.append(
            {
                "key": "gateway",
                "label": "Restart Gateway Container",
                "description": "Not available in this runtime.",
                "supported": False,
                "disabled_reason": reason,
                "safety": "Disabled for safety.",
            }
        )
        actions.append(
            {
                "key": "redis",
                "label": "Restart Redis Container",
                "description": "Not available in this runtime.",
                "supported": False,
                "disabled_reason": reason,
                "safety": "Disabled for safety.",
            }
        )

    actions.append(
        {
            "key": "app",
            "label": "Restart App Process",
            "description": "In-process restart is intentionally disabled.",
            "supported": False,
            "disabled_reason": "Restarting the running ASGI process from inside the request handler is not safe in this deployment.",
            "safety": "Disabled for safety.",
        }
    )
    return actions


def _restart_action_result(settings: Settings, action: str) -> dict:
    if action not in {"gateway", "redis", "app"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown restart action")

    if action == "app":
        return {
            "ok": False,
            "action": action,
            "message": "App restart is disabled in this runtime.",
            "disabled": True,
            "reason": "Restarting the running ASGI process from inside the request handler is not safe.",
        }

    if not (_runtime_command_available("docker") and _runtime_command_available("docker-compose") and (BASE_DIR / "docker-compose.yml").exists()):
        return {
            "ok": False,
            "action": action,
            "message": "docker-compose restart is unavailable in this runtime.",
            "disabled": True,
            "reason": "docker-compose CLI is unavailable or docker-compose.yml was not found.",
        }

    cmd = ["docker-compose", "restart", action]
    try:
        completed = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=90, check=True)
        return {
            "ok": True,
            "action": action,
            "message": f"Restart command completed for {action}.",
            "disabled": False,
            "stdout": completed.stdout[-1000:] if completed.stdout else "",
            "stderr": completed.stderr[-1000:] if completed.stderr else "",
        }
    except subprocess.CalledProcessError as exc:
        return {
            "ok": False,
            "action": action,
            "message": f"Restart command failed for {action}.",
            "disabled": False,
            "reason": (exc.stderr or exc.stdout or str(exc))[-1000:],
        }
    except Exception as exc:
        return {
            "ok": False,
            "action": action,
            "message": f"Restart command could not run for {action}.",
            "disabled": False,
            "reason": str(exc),
        }


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/config", response_class=HTMLResponse)
@app.get("/admin/reports", response_class=HTMLResponse)
@app.get("/admin/health", response_class=HTMLResponse)
async def admin_portal(
    request: Request,
    _: None = Depends(dep_admin_credentials),
    settings: Settings = Depends(dep_settings),
):
    section = "overview"
    if request.url.path.endswith("/config"):
        section = "config"
    elif request.url.path.endswith("/reports"):
        section = "reports"
    elif request.url.path.endswith("/health"):
        section = "health"

    context = _admin_context(request, settings, active_section=section)
    return templates.TemplateResponse(request, "admin.html", context)


@app.post("/admin/config/basic")
async def admin_update_basic_config(
    request: Request,
    _: None = Depends(dep_admin_credentials),
):
    form = await request.form()
    settings = _save_admin_config(
        form,
        [
            "tts_provider",
            "tts_language",
            "tts_voice",
            "tts_speaking_rate",
            "tts_audio_encoding",
            "phone_regex",
            "strip_call_prefix",
            "playback_repeats",
            "playback_pause_ms",
        ],
    )
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="config", success_message="Basic settings saved and applied immediately."),
    )


@app.post("/admin/config/advanced")
async def admin_update_advanced_config(
    request: Request,
    _: None = Depends(dep_admin_credentials),
):
    form = await request.form()
    settings = _save_admin_config(
        form,
        [
            "webhook_secret",
            "google_credentials_json",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_region",
            "aws_polly_voice_id",
            "aws_polly_engine",
            "openai_api_key",
            "openai_tts_model",
            "openai_tts_voice",
            "elevenlabs_api_key",
            "elevenlabs_voice_id",
            "audio_cache_dir",
            "asterisk_sounds_dir",
            "audio_cache_ttl",
            "ami_host",
            "ami_port",
            "ami_username",
            "ami_secret",
            "ami_connection_timeout",
            "ami_response_timeout",
            "sip_channel_prefix",
            "outbound_caller_id",
            "call_answer_timeout",
            "asterisk_context",
            "asterisk_exten",
            "asterisk_priority",
            "delivery_retry_count",
            "delivery_retry_interval_seconds",
            "redis_url",
            "redis_prefix",
            "smpp_enabled",
            "smpp_host",
            "smpp_port",
            "smpp_username",
            "smpp_password",
            "rate_limit_hourly",
            "rate_limit_daily",
            "delivery_report_store_path",
            "delivery_report_max_items",
        ],
    )

    smpp_message = ""
    smpp_service = getattr(app.state, "smpp_service", None)
    if smpp_service is not None:
        try:
            smpp_service.stop()
        except Exception as exc:
            log.warning("Failed stopping SMPP listener during config apply: %s", exc)

        try:
            new_smpp_service = SMPPService(settings)
            if settings.smpp_enabled:
                new_smpp_service.start()
                if new_smpp_service.is_listening:
                    smpp_message = f" SMPP listener is now listening on {settings.smpp_host}:{settings.smpp_port}."
                else:
                    detail = new_smpp_service.last_error or "listener did not enter listening state"
                    smpp_message = f" SMPP listener could not start: {detail}."
            else:
                smpp_message = " SMPP listener is disabled."
            app.state.smpp_service = new_smpp_service
        except Exception as exc:
            failed_service = SMPPService(settings)
            failed_service._last_error = str(exc)
            app.state.smpp_service = failed_service
            smpp_message = f" SMPP listener could not start: {exc}."

    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="config", success_message=f"Advanced settings saved and applied immediately.{smpp_message}"),
    )


@app.post("/admin/config/sip-accounts")
async def admin_add_sip_account(
    request: Request,
    _: None = Depends(dep_admin_credentials),
):
    form = await request.form()
    current = ensure_default_accounts(load_settings_from_store())
    new_account = _build_sip_account_from_form(form)
    sip_accounts = [account for account in current.sip_accounts if account.id != new_account.id]
    if new_account.default_for_outbound:
        sip_accounts = [account.model_copy(update={"default_for_outbound": False}) for account in sip_accounts]
    sip_accounts.append(new_account)
    settings = _save_account_collections(
        sip_accounts=sip_accounts,
        smpp_sip_assignments=dict(current.smpp_sip_assignments),
    )
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="config", success_message=f"SIP trunk '{new_account.label}' saved."),
    )


@app.post("/admin/config/smpp-accounts")
async def admin_add_smpp_account(
    request: Request,
    _: None = Depends(dep_admin_credentials),
):
    form = await request.form()
    current = ensure_default_accounts(load_settings_from_store())
    new_account = _build_smpp_account_from_form(form)
    smpp_accounts = [account for account in current.smpp_accounts if account.id != new_account.id]
    if new_account.default_for_inbound:
        smpp_accounts = [account.model_copy(update={"default_for_inbound": False}) for account in smpp_accounts]
    smpp_accounts.append(new_account)
    assignments = dict(current.smpp_sip_assignments)
    if new_account.username:
        assignments[new_account.username] = new_account.default_sip_account_id or assignments.get(new_account.username, "")
    settings = _save_account_collections(
        smpp_accounts=smpp_accounts,
        smpp_sip_assignments=assignments,
    )
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="config", success_message=f"SMPP user '{new_account.label}' saved."),
    )


@app.post("/admin/config/assignments")
async def admin_assign_smpp_to_sip(
    request: Request,
    _: None = Depends(dep_admin_credentials),
):
    form = await request.form()
    current = ensure_default_accounts(load_settings_from_store())
    smpp_username = str(form.get("smpp_username", "")).strip()
    sip_account_id = str(form.get("sip_account_id", "")).strip()
    assignments = dict(current.smpp_sip_assignments)
    if smpp_username and sip_account_id:
        assignments[smpp_username] = sip_account_id
    settings = _save_account_collections(smpp_sip_assignments=assignments)
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="config", success_message=f"Assigned SMPP user '{smpp_username}' to SIP trunk '{sip_account_id}'."),
    )


@app.get("/admin/reports/live")
async def admin_reports_live(
    _: None = Depends(dep_admin_credentials),
    settings: Settings = Depends(dep_settings),
):
    report_summary, recent_reports = _report_context(settings)
    sms_inbox_summary, recent_inbox_messages, queue_summary, recent_queue_items = _build_queue_context(settings)
    live_calls = _build_live_call_context(settings)
    return {
        "report_summary": report_summary,
        "recent_reports": recent_reports,
        "sms_inbox_summary": sms_inbox_summary,
        "recent_inbox_messages": recent_inbox_messages,
        "queue_summary": queue_summary,
        "recent_queue_items": recent_queue_items,
        "live_calls": live_calls,
        "updated_at": live_calls.get("updated_at", ""),
    }

@app.get("/admin/reports/export/{dataset}.{file_format}")
async def admin_export_reports(
    dataset: str,
    file_format: str,
    _: None = Depends(dep_admin_credentials),
    settings: Settings = Depends(dep_settings),
):
    dataset_key = dataset.strip().lower()
    format_key = file_format.strip().lower()

    exporters: dict[tuple[str, str], tuple[bytes, str, str]] = {
        ("reports", "csv"): (
            export_delivery_reports_csv(settings),
            "text/csv; charset=utf-8",
            "delivery-reports.csv",
        ),
        ("reports", "xls"): (
            export_delivery_reports_xlsx(settings),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "delivery-reports.xlsx",
        ),
        ("inbox", "csv"): (
            export_inbox_messages_csv(settings),
            "text/csv; charset=utf-8",
            "sms-inbox.csv",
        ),
        ("inbox", "xls"): (
            export_inbox_messages_xlsx(settings),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "sms-inbox.xlsx",
        ),
    }

    selected = exporters.get((dataset_key, format_key))
    if not selected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unsupported export type")

    content, media_type, filename = selected
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/reports/clear")
async def admin_clear_old_reports(
    request: Request,
    _: None = Depends(dep_admin_credentials),
    settings: Settings = Depends(dep_settings),
):
    collector = get_delivery_report_collector(settings)
    if hasattr(collector, "clear_old_reports"):
        collector.clear_old_reports()
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="reports", success_message="Old reports cleared."),
    )


@app.post("/admin/health/restart")
async def admin_restart_service(
    request: Request,
    action: str = "",
    _: None = Depends(dep_admin_credentials),
    settings: Settings = Depends(dep_settings),
):
    result = _restart_action_result(settings, action.strip())
    health_context = _build_health_context(settings)
    health_context["restart_result"] = result
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(
            request,
            settings,
            active_section="health",
            success_message=result["message"],
            health_context=health_context,
        ),
    )


@app.post("/sms/twilio", response_class=PlainTextResponse)
async def twilio_webhook(
    request: Request,
    Body: Annotated[str, Form()] = "",
    From: Annotated[str, Form()] = "",
    To: Annotated[str, Form()] = "",
    settings: Settings = Depends(dep_settings),
    gateway: SMSGateway = Depends(dep_gateway),
):
    _verify_twilio_signature(request, settings)
    sms = IncomingSMS(body=Body, from_number=From, to_number=To, provider="twilio")
    log.info("Twilio SMS from=%s body=%r", From, Body[:80])
    result = gateway.process(sms)
    _log_result(result)
    _record_gateway_result("twilio", result, phone_number=From or To, message=Body[:120])
    return '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def _verify_twilio_signature(request: Request, settings: Settings) -> None:
    if not settings.webhook_secret:
        return
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing Twilio signature")


class VonagePayload(BaseModel):
    text: str
    msisdn: str = ""
    to: str = ""
    messageId: str = ""


@app.post("/sms/vonage")
async def vonage_webhook(
    payload: VonagePayload,
    gateway: SMSGateway = Depends(dep_gateway),
    settings: Settings = Depends(dep_settings),
):
    sms = IncomingSMS(
        body=payload.text,
        from_number=payload.msisdn,
        to_number=payload.to,
        provider="vonage",
    )
    log.info("Vonage SMS from=%s body=%r", payload.msisdn, payload.text[:80])
    result = gateway.process(sms)
    _log_result(result)
    _record_gateway_result("vonage", result, phone_number=payload.msisdn or payload.to, message=payload.text[:120])
    return {"status": "ok" if result.success else "error", "detail": result.error or None}


class GenericSMSPayload(BaseModel):
    body: str
    from_number: str = ""
    to_number: str = ""
    destination: str = ""
    secret: str = ""


@app.post("/sms/generic")
async def generic_webhook(
    payload: GenericSMSPayload,
    settings: Settings = Depends(dep_settings),
    gateway: SMSGateway = Depends(dep_gateway),
):
    if settings.webhook_secret and not hmac.compare_digest(
        payload.secret, settings.webhook_secret
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret")

    sms = IncomingSMS(
        body=payload.body,
        from_number=payload.from_number,
        to_number=payload.to_number,
        destination=payload.destination,
        provider="generic",
    )
    log.info("Generic SMS body=%r dest=%s", payload.body[:80], payload.destination)
    result = gateway.process(sms)
    _log_result(result)
    _record_gateway_result("generic", result, phone_number=payload.destination or payload.from_number or payload.to_number, message=payload.body[:120])

    if not result.success:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, result.error)

    return {
        "success": True,
        "phone_number": result.phone_number,
        "text_spoken": result.text_spoken,
        "audio_cached": result.was_cached,
        "ami_action_id": result.ami_action_id,
        "details": result.details,
    }


@app.get("/health")
async def health(settings: Settings = Depends(dep_settings)):
    from .ami_service import AMIService
    redis_ok = False

    try:
        r = get_redis(settings)
        redis_ok = r.ping()
    except Exception as e:
        log.debug("Redis health check failed: %s", e)

    sip_ok = bool(build_pjsua2_service(settings).status_detail().get("registered"))
    healthy = sip_ok and redis_ok
    return JSONResponse(
        content={
            "status": "healthy" if healthy else "degraded",
        "sip": build_pjsua2_service(settings).status_detail(),
        "redis": redis_ok,
        "tts_provider": settings.tts_provider,
        "smpp_enabled": settings.smpp_enabled,
        "smpp_port": settings.smpp_port,
        },
        status_code=200 if healthy else 503,
    )


@app.get("/cache/stats")
async def cache_stats(settings: Settings = Depends(dep_settings)):
    cache_dir = Path(settings.audio_cache_dir)
    files = list(cache_dir.glob("*.wav"))
    total_bytes = sum(f.stat().st_size for f in files)
    return {
        "audio_files": len(files),
        "total_size_mb": round(total_bytes / 1024 / 1024, 2),
        "cache_dir": str(cache_dir.resolve()),
        "asterisk_sounds_dir": settings.asterisk_sounds_dir,
    }


@app.post("/cache/evict")
async def cache_evict(
    secret: str = "",
    settings: Settings = Depends(dep_settings),
):
    if settings.webhook_secret and not hmac.compare_digest(secret, settings.webhook_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret")
    removed = AudioCache(settings).evict_expired()
    return {"evicted_files": removed}


class DebugCallRequest(BaseModel):
    phone: str
    text: str
    secret: str = ""


@app.post("/debug/call")
async def debug_call(
    req: DebugCallRequest,
    settings: Settings = Depends(dep_settings),
    gateway: SMSGateway = Depends(dep_gateway),
):
    if settings.webhook_secret and not hmac.compare_digest(req.secret, settings.webhook_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret")

    sms = IncomingSMS(body=req.text, destination=req.phone, provider="debug")
    result = gateway.process(sms)
    _record_gateway_result("debug", result, phone_number=req.phone, message=req.text[:120])
    return {
        "success": result.success,
        "phone_number": result.phone_number,
        "text_spoken": result.text_spoken,
        "audio_cached": result.was_cached,
        "sip_call_id": result.sip_call_id,
        "sip_account_id": result.sip_account_id,
        "error": result.error,
        "details": result.details,
    }


def _log_result(result) -> None:
    if result.success:
        log.info("Call queued → %s (cached=%s sip_call_id=%s sip_account_id=%s)",
                 result.phone_number, result.was_cached, result.sip_call_id, result.sip_account_id)
    else:
        log.error("Gateway failed for %s: %s", result.phone_number, result.error)
