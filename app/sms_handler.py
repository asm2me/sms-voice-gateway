"""
Core business logic: parse an incoming SMS → synthesise voice → place direct SIP UA call.

SMS body format (flexible):
  "CALL:+9661234567890 Your OTP is 123456"
  "TO:+9661234567890 Your verification code is 654321"
  "+9661234567890 Your code is 111222"
  "Your code is 333444"   ← phone comes from webhook metadata (To/From field)

The destination phone number is resolved in this order:
  1. Explicit prefix in body: CALL:<number> or TO:<number>
  2. First E.164-looking number found anywhere in the body
  3. Caller-provided `destination` parameter in the webhook payload
  4. SMS `From` field (the number that *sent* the SMS) — useful for 2-way flows
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .admin_reports import QueueItem, get_queue_store
from .ami_service import AMIService
from .cache import AudioCache, RateLimiter
from .config import SIPAccount, Settings
from .config_store import get_sip_account_for_smpp_username
from .pjsua2_service import SipCallRequest, build_pjsua2_service
from .tts_service import TTSService, _generate_silence

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class IncomingSMS:
    body: str
    from_number: str = ""
    to_number: str = ""
    destination: str = ""
    provider: str = "generic"
    smpp_username: str = ""


@dataclass
class GatewayResult:
    success: bool
    phone_number: str = ""
    text_spoken: str = ""
    audio_path: str = ""
    was_cached: bool = False
    ami_action_id: str = ""
    sip_call_id: str = ""
    sip_account_id: str = ""
    error: str = ""
    details: dict = field(default_factory=dict)


_PREFIX_RE = re.compile(
    r"(?:CALL|TO|DEST|PHONE|NUMBER)[:\s]+(\+?[\d\s\-\(\)\.]{7,20})",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(\+[\d]{7,15}|\b0[\d]{8,14}\b|\b[\d]{10,15}\b)")
_STRIP_RE = re.compile(
    r"^(?:CALL|TO|DEST|PHONE|NUMBER)[:\s]+\+?[\d\s\-\(\)\.]{7,20}\s*",
    re.IGNORECASE,
)


def extract_destination(sms: IncomingSMS) -> tuple[str, str]:
    body = sms.body.strip()

    m = _PREFIX_RE.search(body)
    if m:
        phone = _normalise_number(m.group(1))
        text = _STRIP_RE.sub("", body).strip()
        return phone, text or body

    m = _NUMBER_RE.search(body)
    if m:
        phone = _normalise_number(m.group(1))
        text = body.replace(m.group(1), "").strip(" ,:;-")
        return phone, text or body

    if sms.destination:
        return _normalise_number(sms.destination), body

    if sms.from_number:
        return _normalise_number(sms.from_number), body

    raise ValueError("Cannot determine destination phone number from SMS")


def _normalise_number(raw: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", raw.strip())
    if not cleaned:
        raise ValueError(f"Invalid phone number: {raw!r}")
    return cleaned


def _queue_retry(
    settings: Settings,
    *,
    phone_number: str,
    provider: str,
    body_preview: str,
    attempts: int,
    last_error: str,
    ami_action_id: str | None = None,
    sip_account_id: str | None = None,
) -> dict:
    store = get_queue_store(settings)
    now = _utc_now_iso()
    item = QueueItem(
        id=f"{provider}:{phone_number}:{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        created_at=now,
        updated_at=now,
        phone_number=phone_number,
        provider=provider,
        body_preview=body_preview[:160],
        status="retry_scheduled",
        attempts=attempts,
        max_attempts=settings.delivery_retry_count + 1,
        retry_interval_seconds=settings.delivery_retry_interval_seconds,
        next_attempt_at=_schedule_next_attempt(settings.delivery_retry_interval_seconds),
        last_error=last_error,
        ami_action_id=ami_action_id,
        sip_account_id=sip_account_id or "",
    )
    store.upsert(item)
    return item.to_dict()


def _schedule_next_attempt(interval_seconds: int) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + max(interval_seconds, 0),
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")


class SMSGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.audio_cache = AudioCache(settings)
        self.tts = TTSService(settings, self.audio_cache)
        self.sip_ua = build_pjsua2_service(settings)
        self.ami = AMIService(settings)
        self.rate_limiter = RateLimiter(settings)

    def _resolve_sip_account(self, sms: IncomingSMS) -> Optional[SIPAccount]:
        return get_sip_account_for_smpp_username(self.settings, sms.smpp_username)

    def process(self, sms: IncomingSMS) -> GatewayResult:
        try:
            phone, spoken_text = extract_destination(sms)
        except ValueError as exc:
            log.warning("SMS parse error: %s | body=%r", exc, sms.body[:80])
            return GatewayResult(success=False, error=str(exc))

        if not spoken_text:
            return GatewayResult(success=False, phone_number=phone, error="Empty text after stripping phone number")

        allowed, reason = self.rate_limiter.is_allowed(phone)
        if not allowed:
            log.warning("Rate limit for %s: %s", phone, reason)
            return GatewayResult(success=False, phone_number=phone, error=f"Rate limited: {reason}")

        try:
            audio_path, was_cached = self.tts.get_or_create_audio(spoken_text)
        except Exception as exc:
            log.exception("TTS failed for text=%r", spoken_text[:60])
            if sms.provider == "admin-test":
                fallback_hash = self.tts.hash_for(f"admin-test-fallback:{spoken_text}")
                audio_path = self.audio_cache.store_audio(fallback_hash, _generate_silence(1500))
                was_cached = False
                log.warning(
                    "Admin test-send falling back to generated silence audio because TTS failed: %s",
                    exc,
                )
            else:
                return GatewayResult(
                    success=False,
                    phone_number=phone,
                    text_spoken=spoken_text,
                    error=f"TTS error: {exc}",
                )

        hkey = self.tts.hash_for(spoken_text)
        max_attempts = max(1, int(self.settings.delivery_retry_count) + 1)
        retry_interval_seconds = max(0, int(self.settings.delivery_retry_interval_seconds))
        last_error = ""
        ami_action_id = ""
        sip_call_id = ""
        sip_account = self._resolve_sip_account(sms)
        sip_account_id = sip_account.id if sip_account else ""

        if sip_account is None:
            return GatewayResult(
                success=False,
                phone_number=phone,
                text_spoken=spoken_text,
                audio_path=audio_path,
                was_cached=was_cached,
                error="No enabled SIP account is assigned for this SMPP user",
                details={
                    "tts_cached": was_cached,
                    "hash": hkey,
                    "rate_counts": self.rate_limiter.get_counts(phone),
                    "smpp_username": sms.smpp_username or "",
                },
            )

        pjsua_status = self.sip_ua.status_detail()
        use_ami_fallback = not bool(pjsua_status.get("available"))
        fallback_reason = pjsua_status.get("import_error") or "PJSUA2 bindings are unavailable"

        for attempt in range(1, max_attempts + 1):
            if use_ami_fallback:
                asterisk_sound_ref = self.audio_cache.asterisk_sound_ref(Path(audio_path).stem)
                ami_result = self.ami.originate_playback(
                    phone,
                    asterisk_sound_ref,
                    extra_vars={
                        "OTP_TEXT": spoken_text[:80],
                        "SIP_ACCOUNT_ID": sip_account.id,
                        "SMPP_USERNAME": sms.smpp_username or "",
                    },
                )
                ami_action_id = ami_result.action_id or ami_action_id

                if ami_result.success:
                    return GatewayResult(
                        success=True,
                        phone_number=phone,
                        text_spoken=spoken_text,
                        audio_path=audio_path,
                        was_cached=was_cached,
                        ami_action_id=ami_action_id,
                        sip_account_id=sip_account_id,
                        details={
                            "transport": "ami-fallback",
                            "fallback_reason": fallback_reason,
                            "ami_result": ami_result.raw,
                            "tts_cached": was_cached,
                            "hash": hkey,
                            "rate_counts": self.rate_limiter.get_counts(phone),
                            "attempts": attempt,
                            "max_attempts": max_attempts,
                            "retry_interval_seconds": retry_interval_seconds,
                            "sip_account_id": sip_account_id,
                            "smpp_username": sms.smpp_username or "",
                        },
                    )

                last_error = ami_result.message or "AMI originate failed"
                log.warning(
                    "AMI fallback attempt %d/%d failed for %s via %s: %s",
                    attempt,
                    max_attempts,
                    phone,
                    sip_account_id,
                    last_error,
                )
            else:
                sip_result = self.sip_ua.place_outbound_call(
                    SipCallRequest(
                        destination_number=phone,
                        audio_path=audio_path,
                        account_id=sip_account.id,
                        display_name=sip_account.display_name or sip_account.label,
                        caller_id=sip_account.from_user or self.settings.outbound_caller_id,
                        timeout_seconds=self.settings.call_answer_timeout,
                        playback_repeats=self.settings.playback_repeats,
                        playback_pause_ms=self.settings.playback_pause_ms,
                        extra_vars={
                            "OTP_TEXT": spoken_text[:80],
                            "SIP_ACCOUNT_ID": sip_account.id,
                            "SMPP_USERNAME": sms.smpp_username or "",
                        },
                    ),
                    profile={
                        "id": sip_account.id,
                        "display_name": sip_account.display_name or sip_account.label,
                        "domain": sip_account.domain or sip_account.host,
                        "username": sip_account.username,
                        "password": sip_account.password,
                        "transport": (sip_account.transport or "udp").upper(),
                        "caller_id": sip_account.from_user or self.settings.outbound_caller_id,
                        "enabled": sip_account.enabled,
                        "proxy_uri": sip_account.outbound_proxy,
                    },
                )
                sip_call_id = sip_result.call_id or sip_call_id

                if sip_result.success:
                    return GatewayResult(
                        success=True,
                        phone_number=phone,
                        text_spoken=spoken_text,
                        audio_path=audio_path,
                        was_cached=was_cached,
                        sip_call_id=sip_call_id,
                        sip_account_id=sip_account_id,
                        details={
                            "transport": "pjsua2",
                            "sip_result": sip_result.details,
                            "tts_cached": was_cached,
                            "hash": hkey,
                            "rate_counts": self.rate_limiter.get_counts(phone),
                            "attempts": attempt,
                            "max_attempts": max_attempts,
                            "retry_interval_seconds": retry_interval_seconds,
                            "sip_account_id": sip_account_id,
                            "smpp_username": sms.smpp_username or "",
                        },
                    )

                last_error = sip_result.error or sip_result.message or "Direct SIP call failed"
                log.warning("Direct SIP attempt %d/%d failed for %s via %s: %s", attempt, max_attempts, phone, sip_account_id, last_error)

            if attempt < max_attempts:
                retry_snapshot = _queue_retry(
                    self.settings,
                    phone_number=phone,
                    provider=sms.provider,
                    body_preview=spoken_text,
                    attempts=attempt,
                    last_error=last_error,
                    ami_action_id=ami_action_id or None,
                    sip_account_id=sip_account_id,
                )
                log.info(
                    "Retry scheduled for %s in %ss (attempt=%d/%d, queue_id=%s)",
                    phone,
                    retry_interval_seconds,
                    attempt,
                    max_attempts,
                    retry_snapshot.get("id", ""),
                )

        return GatewayResult(
            success=False,
            phone_number=phone,
            text_spoken=spoken_text,
            audio_path=audio_path,
            was_cached=was_cached,
            sip_call_id=sip_call_id,
            sip_account_id=sip_account_id,
            error=last_error or "Direct SIP call failed",
            details={
                "sip_result": None,
                "tts_cached": was_cached,
                "hash": hkey,
                "rate_counts": self.rate_limiter.get_counts(phone),
                "attempts": max_attempts,
                "max_attempts": max_attempts,
                "retry_interval_seconds": retry_interval_seconds,
                "sip_account_id": sip_account_id,
                "smpp_username": sms.smpp_username or "",
            },
        )
