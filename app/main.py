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
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .admin_reports import get_delivery_report_collector, record_delivery_report
from .cache import AudioCache, RateLimiter, get_redis
from .config import Settings, get_settings
from .config_store import load_settings_from_store, save_settings_to_store
from .sms_handler import IncomingSMS, SMSGateway

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


# ─────────────────────────────────────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("SMS Voice Gateway starting (TTS=%s, AMI=%s:%d)",
             settings.tts_provider, settings.ami_host, settings.ami_port)
    yield
    log.info("SMS Voice Gateway stopped")


app = FastAPI(
    title="SMS Voice Gateway",
    description="Converts incoming SMS to TTS and delivers via SIP/Asterisk call",
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─────────────────────────────────────────────────────────────────────────────
# Dependency helpers
# ─────────────────────────────────────────────────────────────────────────────

def dep_settings() -> Settings:
    return load_settings_from_store()


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


def _settings_sections(settings: Settings) -> dict[str, list[dict[str, str]]]:
    basic_fields = [
        ("tts_provider", "TTS Provider"),
        ("tts_language", "TTS Language"),
        ("tts_voice", "TTS Voice"),
        ("tts_speaking_rate", "Speaking Rate"),
        ("tts_audio_encoding", "Audio Encoding"),
        ("phone_regex", "Phone Regex"),
        ("strip_call_prefix", "Strip Call Prefix"),
        ("playback_repeats", "Playback Repeats"),
        ("playback_pause_ms", "Playback Pause (ms)"),
    ]
    advanced_fields = [
        ("webhook_secret", "Webhook Secret"),
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
        ("audio_cache_dir", "Audio Cache Directory"),
        ("asterisk_sounds_dir", "Asterisk Sounds Directory"),
        ("audio_cache_ttl", "Audio Cache TTL"),
        ("ami_host", "AMI Host"),
        ("ami_port", "AMI Port"),
        ("ami_username", "AMI Username"),
        ("ami_secret", "AMI Secret"),
        ("ami_connection_timeout", "AMI Connection Timeout"),
        ("ami_response_timeout", "AMI Response Timeout"),
        ("sip_channel_prefix", "SIP Channel Prefix"),
        ("outbound_caller_id", "Outbound Caller ID"),
        ("call_answer_timeout", "Call Answer Timeout"),
        ("asterisk_context", "Asterisk Context"),
        ("asterisk_exten", "Asterisk Exten"),
        ("asterisk_priority", "Asterisk Priority"),
        ("redis_url", "Redis URL"),
        ("redis_prefix", "Redis Prefix"),
        ("rate_limit_hourly", "Rate Limit Hourly"),
        ("rate_limit_daily", "Rate Limit Daily"),
        ("delivery_report_store_path", "Delivery Report Store Path"),
        ("delivery_report_max_items", "Delivery Report Max Items"),
        ("host", "Host"),
        ("port", "Port"),
        ("debug", "Debug"),
        ("admin_username", "Admin Username"),
        ("admin_password", "Admin Password"),
    ]

    def build_items(field_specs: list[tuple[str, str]]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for name, label in field_specs:
            value = getattr(settings, name)
            items.append(
                {
                    "name": name,
                    "label": label,
                    "value": "••••••" if _is_secret_field(name) and value else "" if value is None else str(value),
                    "raw_value": "" if value is None else str(value),
                    "type": type(value).__name__,
                    "is_secret": _is_secret_field(name),
                }
            )
        return items

    return {"basic": build_items(basic_fields), "advanced": build_items(advanced_fields)}


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
        {"label": "AMI Host", "display_value": settings.ami_host, "visibility": "public"},
        {"label": "AMI Port", "display_value": str(settings.ami_port), "visibility": "public"},
        {"label": "SIP Channel Prefix", "display_value": settings.sip_channel_prefix, "visibility": "public"},
        {"label": "Outbound Caller ID", "display_value": settings.outbound_caller_id, "visibility": "public"},
        {"label": "Redis URL", "display_value": settings.redis_url, "visibility": "public"},
        {"label": "Audio Cache Directory", "display_value": settings.audio_cache_dir, "visibility": "public"},
        {"label": "Asterisk Sounds Directory", "display_value": settings.asterisk_sounds_dir, "visibility": "public"},
        {"label": "Webhook Secret", "display_value": "configured" if settings.webhook_secret else "not configured", "visibility": "protected"},
    ]


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
    current = load_settings_from_store()
    data = current.model_dump()
    for key in keys:
        if key in form:
            raw = str(form.get(key, "")).strip()
            data[key] = raw if raw != "" else data.get(key)
    updated = Settings(**data)
    save_settings_to_store(updated)
    return updated


def _admin_context(
    request: Request,
    settings: Settings,
    *,
    active_section: str,
    success_message: str | None = None,
    health_context: dict | None = None,
) -> dict:
    settings_sections = _settings_sections(settings)
    report_summary, recent_reports = _report_context(settings)
    context = {
        "request": request,
        "active_section": active_section,
        "config_snapshot": {"source": "saved settings" if success_message else "runtime settings", "items": _config_items(settings)},
        "report_summary": report_summary,
        "recent_reports": recent_reports,
        "basic_settings": settings_sections["basic"],
        "advanced_settings": settings_sections["advanced"],
        "report_clear_supported": hasattr(get_delivery_report_collector(settings), "clear_old_reports"),
        "health": health_context or _build_health_context(settings),
    }
    if success_message:
        context["success_message"] = success_message
    return context


def _build_health_context(settings: Settings) -> dict:
    from .ami_service import AMIService

    now = time.time()
    checks = []
    overall_ok = True

    def add_check(key: str, label: str, status_value: str, details: str, *, restart_supported: bool = False, restart_disabled_reason: str = "") -> None:
        nonlocal overall_ok
        if status_value != "healthy":
            overall_ok = False
        checks.append(
            {
                "key": key,
                "label": label,
                "status": status_value,
                "details": details,
                "restart_supported": restart_supported,
                "restart_disabled_reason": restart_disabled_reason,
            }
        )

    add_check("api", "API / App", "healthy", "FastAPI process is serving requests.", restart_supported=False, restart_disabled_reason="This runtime does not support in-process app restarts.")
    try:
        redis_ok = get_redis(settings).ping()
        add_check("redis", "Redis", "healthy" if redis_ok else "degraded", "Redis ping succeeded." if redis_ok else "Redis ping failed.", restart_supported=False, restart_disabled_reason="Redis is managed externally and cannot be restarted safely from the app.")
    except Exception as exc:
        add_check("redis", "Redis", "degraded", f"Redis ping failed: {exc}", restart_supported=False, restart_disabled_reason="Redis is managed externally and cannot be restarted safely from the app.")

    try:
        ami_ok = AMIService(settings).ping()
        add_check("ami", "AMI", "healthy" if ami_ok else "degraded", "AMI ping succeeded." if ami_ok else "AMI ping failed.", restart_supported=False, restart_disabled_reason="AMI is managed by Asterisk and cannot be restarted safely from the app.")
    except Exception as exc:
        add_check("ami", "AMI", "degraded", f"AMI ping failed: {exc}", restart_supported=False, restart_disabled_reason="AMI is managed by Asterisk and cannot be restarted safely from the app.")

    try:
        Path(settings.delivery_report_store_path).parent.mkdir(parents=True, exist_ok=True)
        add_check("config_store", "Config Store", "healthy", "Settings store is writable and reachable.", restart_supported=False, restart_disabled_reason="Config storage is file-based and does not have a restart action.")
    except Exception as exc:
        add_check("config_store", "Config Store", "degraded", f"Config store access failed: {exc}", restart_supported=False, restart_disabled_reason="Config storage is file-based and does not have a restart action.")

    try:
        collector = get_delivery_report_collector(settings)
        collector.summary()
        add_check("report_store", "Report Store", "healthy", "Report storage is reachable.", restart_supported=False, restart_disabled_reason="Report storage is file-based and does not have a restart action.")
    except Exception as exc:
        add_check("report_store", "Report Store", "degraded", f"Report store access failed: {exc}", restart_supported=False, restart_disabled_reason="Report storage is file-based and does not have a restart action.")

    tts_ready = bool(settings.tts_provider)
    add_check("tts", "TTS Provider Readiness", "healthy" if tts_ready else "degraded", f"TTS provider configured as {settings.tts_provider}." if tts_ready else "No TTS provider configured.", restart_supported=False, restart_disabled_reason="TTS providers are configured at runtime and do not expose restart controls.")

    restart_actions = _restart_actions(settings)
    return {
        "generated_at": now,
        "overall_status": "healthy" if overall_ok else "degraded",
        "checks": checks,
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


# ─────────────────────────────────────────────────────────────────────────────
# Admin portal
# ─────────────────────────────────────────────────────────────────────────────

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

    return templates.TemplateResponse(request, "admin.html", _admin_context(request, settings, active_section=section))


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
            "redis_url",
            "redis_prefix",
            "rate_limit_hourly",
            "rate_limit_daily",
            "delivery_report_store_path",
            "delivery_report_max_items",
            "host",
            "port",
            "debug",
            "admin_username",
            "admin_password",
        ],
    )
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(request, settings, active_section="config", success_message="Advanced settings saved and applied immediately."),
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


# ─────────────────────────────────────────────────────────────────────────────
# Twilio webhook
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Vonage / Nexmo webhook
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Generic webhook
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Health endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(settings: Settings = Depends(dep_settings)):
    from .ami_service import AMIService
    ami_ok = False
    redis_ok = False

    try:
        ami_ok = AMIService(settings).ping()
    except Exception as e:
        log.debug("AMI health check failed: %s", e)

    try:
        r = get_redis(settings)
        redis_ok = r.ping()
    except Exception as e:
        log.debug("Redis health check failed: %s", e)

    healthy = ami_ok and redis_ok
    return JSONResponse(
        content={
            "status": "healthy" if healthy else "degraded",
            "ami": ami_ok,
            "redis": redis_ok,
            "tts_provider": settings.tts_provider,
            "sip_channel_prefix": settings.sip_channel_prefix,
        },
        status_code=200 if healthy else 503,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cache management endpoints
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Debug / manual trigger
# ─────────────────────────────────────────────────────────────────────────────

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
        "ami_action_id": result.ami_action_id,
        "error": result.error,
        "details": result.details,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def _log_result(result) -> None:
    if result.success:
        log.info("Call queued → %s (cached=%s action=%s)",
                 result.phone_number, result.was_cached, result.ami_action_id)
    else:
        log.error("Gateway failed for %s: %s", result.phone_number, result.error)