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
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .cache import AudioCache, RateLimiter, get_redis
from .config import Settings, get_settings
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
    return get_settings()


def dep_gateway(settings: Annotated[Settings, Depends(dep_settings)]) -> SMSGateway:
    return SMSGateway(settings)


# ─────────────────────────────────────────────────────────────────────────────
# Admin portal
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/config", response_class=HTMLResponse)
@app.get("/admin/reports", response_class=HTMLResponse)
async def admin_portal(request: Request, settings: Settings = Depends(dep_settings)):
    section = "overview"
    if request.url.path.endswith("/config"):
        section = "config"
    elif request.url.path.endswith("/reports"):
        section = "reports"

    config_items = [
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

    report_summary = {
        "total": 0,
        "status_counts": [
            {"status": "success", "count": 0},
            {"status": "error", "count": 0},
            {"status": "pending", "count": 0},
            {"status": "unknown", "count": 0},
        ],
    }
    recent_reports = []

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "active_section": section,
            "config_snapshot": {"source": "runtime settings", "items": config_items},
            "report_summary": report_summary,
            "recent_reports": recent_reports,
        },
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
    # Twilio expects an empty TwiML response (or a <Response/> body)
    return '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def _verify_twilio_signature(request: Request, settings: Settings) -> None:
    if not settings.webhook_secret:
        return
    # Twilio signs requests with X-Twilio-Signature (HMAC-SHA1 of URL+params)
    # For full production use, install twilio library and use RequestValidator.
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing Twilio signature")


# ─────────────────────────────────────────────────────────────────────────────
# Vonage / Nexmo webhook
# ─────────────────────────────────────────────────────────────────────────────

class VonagePayload(BaseModel):
    text: str
    msisdn: str = ""        # sender
    to: str = ""            # receiver
    messageId: str = ""


@app.post("/sms/vonage")
async def vonage_webhook(
    payload: VonagePayload,
    gateway: SMSGateway = Depends(dep_gateway),
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
    return {"status": "ok" if result.success else "error", "detail": result.error or None}


# ─────────────────────────────────────────────────────────────────────────────
# Generic webhook
# ─────────────────────────────────────────────────────────────────────────────

class GenericSMSPayload(BaseModel):
    body: str
    from_number: str = ""
    to_number: str = ""
    destination: str = ""   # explicit override for the call destination
    secret: str = ""        # optional per-request secret


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
    from pathlib import Path
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