"""
Core business logic: parse an incoming SMS → synthesise voice → place an outbound voice call.

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
from .cache import AudioCache
from .config import SIPAccount, Settings, SMPPAccount
from .config_store import get_sip_account_for_smpp_username
from .message_parts import (
    render_static_default_message,
    resolve_static_message_parts,
)
from .pjsua2_service import SipCallRequest, build_pjsua2_service
from .tts_service import TTSService, _concat_wavs, _ensure_wav_format, _generate_silence

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_uploaded_audio_abs_path(raw_path: str) -> Optional[Path]:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (_REPO_ROOT / candidate)
    try:
        candidate = candidate.resolve()
    except Exception:
        return None
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def _audio_fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{path.name}:{int(stat.st_mtime_ns)}:{stat.st_size}"
    except Exception:
        return f"{path.name}:0:0"

log = logging.getLogger(__name__)

_SMS_GATEWAY_PJSUA_SCOPE = "sms-gateway"


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
    recording_path: str = ""
    was_cached: bool = False
    ami_action_id: str = ""
    sip_call_id: str = ""
    sip_account_id: str = ""
    error: str = ""
    delivered: bool = False
    read: bool = False
    answered: bool = False
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


def _derive_pending_reason(*, stage: str, detail: str = "") -> str:
    normalized_stage = (stage or "").strip().lower()
    normalized_detail = (detail or "").strip()
    if normalized_stage == "sip_trunk":
        base = "Pending SIP trunk"
    elif normalized_stage == "voice_tts":
        base = "Pending VOICE TTS"
    elif normalized_stage == "destination":
        base = "Pending valid destination"
    else:
        base = "Pending processing"
    return f"{base}: {normalized_detail}" if normalized_detail else base


def _queue_retry(
    settings: Settings,
    *,
    phone_number: str,
    provider: str,
    body: str,
    body_preview: str,
    attempts: int,
    max_attempts: int,
    retry_interval_seconds: int,
    last_error: str,
    sip_account_id: str | None = None,
    smpp_username: str = "",
    audio_path: str = "",
) -> dict:
    store = get_queue_store(settings)
    now = _utc_now_iso()
    item = QueueItem(
        id=f"{provider}:{phone_number}:{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        created_at=now,
        updated_at=now,
        phone_number=phone_number,
        provider=provider,
        body=body,
        body_preview=body_preview[:160],
        status="retry_scheduled",
        attempts=attempts,
        max_attempts=max_attempts,
        retry_interval_seconds=retry_interval_seconds,
        next_attempt_at=_schedule_next_attempt(retry_interval_seconds),
        last_error=last_error,
        sip_account_id=sip_account_id or "",
        smpp_username=smpp_username,
        audio_path=audio_path,
    )
    store.upsert(item)
    return item.to_dict()


def _schedule_next_attempt(interval_seconds: int) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + max(interval_seconds, 0),
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")


class SMSGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        sip_scope: str = _SMS_GATEWAY_PJSUA_SCOPE,
        isolated_sip: bool = False,
    ):
        self.settings = settings
        self.audio_cache = AudioCache(settings)
        self.tts = TTSService(settings, self.audio_cache)
        self.sip_ua = build_pjsua2_service(
            settings,
            scope=sip_scope,
            isolated=isolated_sip,
        )
    def _resolve_sip_account(self, sms: IncomingSMS) -> Optional[SIPAccount]:
        return get_sip_account_for_smpp_username(self.settings, sms.smpp_username)

    def _resolve_smpp_account(self, sms: IncomingSMS):
        if not sms.smpp_username:
            return None
        return next(
            (account for account in self.settings.smpp_accounts if account.username == sms.smpp_username),
            None,
        )

    def _resolve_static_template_audio(
        self,
        *,
        inbound_text: str,
        rendered_text: str,
        template: str,
        smpp_account: Optional[SMPPAccount] = None,
    ) -> tuple[str, bool]:
        if smpp_account is not None:
            uploaded_account_path = _resolve_uploaded_audio_abs_path(
                str(smpp_account.uploaded_audio_path or "")
            )
            if uploaded_account_path is not None:
                log.info(
                    "Using account-wide uploaded audio for SMPP user %s: %s",
                    smpp_account.username or smpp_account.id,
                    uploaded_account_path,
                )
                return str(uploaded_account_path), True

        part_audio_map = dict((smpp_account.static_message_part_audio or {}) if smpp_account else {})
        digit_audio_map = dict((smpp_account.static_message_digit_audio or {}) if smpp_account else {})

        resolved_parts = resolve_static_message_parts(template, inbound_text)
        spoken_resolved_parts: list[dict] = []
        for part in resolved_parts:
            if part.get("kind") == "parameter":
                if str(part.get("resolved_value", "")).strip():
                    spoken_resolved_parts.append(part)
            elif part.get("spoken") and str(part.get("spoken_value", "")).strip():
                spoken_resolved_parts.append(part)

        if not spoken_resolved_parts:
            return self.tts.get_or_create_audio(rendered_text)

        if not part_audio_map and not digit_audio_map:
            if len(spoken_resolved_parts) <= 1:
                return self.tts.get_or_create_audio(rendered_text)
            merged_hash = self.tts.hash_for(rendered_text)
            cached = self.audio_cache.get_audio_path(merged_hash)
            if cached:
                return cached, True
            wavs: list[bytes] = []
            for part in spoken_resolved_parts:
                segment_text = (
                    str(part.get("resolved_value", ""))
                    if part.get("kind") == "parameter"
                    else str(part.get("spoken_value", ""))
                )
                segment_audio_path, _ = self.tts.get_or_create_audio(segment_text)
                wavs.append(Path(segment_audio_path).read_bytes())
            merged = _ensure_wav_format(_concat_wavs([_ensure_wav_format(w) for w in wavs]))
            return self.audio_cache.store_audio(merged_hash, merged), False

        wav_parts: list[bytes] = []
        used_uploaded_files: list[Path] = []
        used_tts_segments: list[str] = []

        for part in spoken_resolved_parts:
            ordinal = str(part.get("ordinal", "")).strip()
            kind = part.get("kind")
            resolved_value = str(part.get("resolved_value", ""))
            spoken_value = str(part.get("spoken_value", ""))

            if kind == "parameter":
                digit_wavs: list[bytes] = []
                digit_files: list[Path] = []
                all_digits_have_audio = bool(resolved_value)
                for character in resolved_value:
                    info = digit_audio_map.get(character) if character.isdigit() else None
                    if not isinstance(info, dict):
                        all_digits_have_audio = False
                        break
                    digit_path = _resolve_uploaded_audio_abs_path(str(info.get("path", "")))
                    if digit_path is None:
                        all_digits_have_audio = False
                        break
                    digit_wavs.append(digit_path.read_bytes())
                    digit_files.append(digit_path)

                if all_digits_have_audio and digit_wavs:
                    wav_parts.extend(digit_wavs)
                    used_uploaded_files.extend(digit_files)
                    log.info(
                        "Static template parameter ord=%s value=%r resolved via digit audio (%d files)",
                        ordinal, resolved_value, len(digit_files),
                    )
                else:
                    if resolved_value.strip():
                        seg_path, _ = self.tts.get_or_create_audio(resolved_value)
                        wav_parts.append(Path(seg_path).read_bytes())
                        used_tts_segments.append(resolved_value)
                        log.info(
                            "Static template parameter ord=%s value=%r resolved via TTS (digit audio incomplete)",
                            ordinal, resolved_value,
                        )
                continue

            part_info = part_audio_map.get(ordinal)
            uploaded_part_path = None
            if isinstance(part_info, dict):
                uploaded_part_path = _resolve_uploaded_audio_abs_path(str(part_info.get("path", "")))
            if uploaded_part_path is not None:
                wav_parts.append(uploaded_part_path.read_bytes())
                used_uploaded_files.append(uploaded_part_path)
                log.info(
                    "Static template part ord=%s resolved via uploaded audio: %s",
                    ordinal, uploaded_part_path,
                )
            else:
                if spoken_value.strip():
                    seg_path, _ = self.tts.get_or_create_audio(spoken_value)
                    wav_parts.append(Path(seg_path).read_bytes())
                    used_tts_segments.append(spoken_value)
                    log.info(
                        "Static template part ord=%s resolved via TTS (no upload): %r",
                        ordinal, spoken_value,
                    )

        if not wav_parts:
            return self.tts.get_or_create_audio(rendered_text)

        fingerprint = "|".join(_audio_fingerprint(p) for p in used_uploaded_files)
        cache_text = f"{rendered_text}␟{fingerprint}␟" + "␟".join(used_tts_segments)
        merged_hash = self.tts.hash_for(cache_text)
        cached = self.audio_cache.get_audio_path(merged_hash)
        if cached:
            return cached, True

        if len(wav_parts) == 1 and used_uploaded_files and not used_tts_segments:
            single = _ensure_wav_format(wav_parts[0])
            return self.audio_cache.store_audio(merged_hash, single), False

        merged = _ensure_wav_format(_concat_wavs([_ensure_wav_format(w) for w in wav_parts]))
        return self.audio_cache.store_audio(merged_hash, merged), False

    def process(self, sms: IncomingSMS, *, queue_retries: bool = True) -> GatewayResult:
        try:
            phone, spoken_text = extract_destination(sms)
        except ValueError as exc:
            log.warning("SMS parse error: %s | body=%r", exc, sms.body[:80])
            return GatewayResult(
                success=False,
                error=str(exc),
                details={"pending_reason": _derive_pending_reason(stage="destination", detail=str(exc))},
            )

        smpp_account = self._resolve_smpp_account(sms)
        rendered_from_static_template = False
        if (
            smpp_account
            and smpp_account.static_default_message_enabled
            and smpp_account.static_default_message_template
        ):
            spoken_text = render_static_default_message(
                smpp_account.static_default_message_template,
                spoken_text,
            )
            rendered_from_static_template = True

        if not spoken_text:
            return GatewayResult(
                success=False,
                phone_number=phone,
                error="Empty text after stripping phone number",
                details={"pending_reason": _derive_pending_reason(stage="voice_tts", detail="message body is empty after number parsing")},
            )

        try:
            if rendered_from_static_template and smpp_account is not None:
                audio_path, was_cached = self._resolve_static_template_audio(
                    inbound_text=extract_destination(sms)[1],
                    rendered_text=spoken_text,
                    template=smpp_account.static_default_message_template,
                    smpp_account=smpp_account,
                )
            else:
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
                    details={"pending_reason": _derive_pending_reason(stage="voice_tts", detail=str(exc))},
                )

        hkey = self.tts.hash_for(spoken_text)
        retry_count = (
            smpp_account.delivery_retry_count
            if smpp_account and smpp_account.delivery_retry_count is not None
            else self.settings.delivery_retry_count
        )
        retry_interval_value = (
            smpp_account.delivery_retry_interval_seconds
            if smpp_account and smpp_account.delivery_retry_interval_seconds is not None
            else self.settings.delivery_retry_interval_seconds
        )
        max_attempts = 1 if sms.provider == "admin-test" else max(1, int(retry_count) + 1)
        retry_interval_seconds = 0 if sms.provider == "admin-test" else max(0, int(retry_interval_value))
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
                    "pending_reason": _derive_pending_reason(stage="sip_trunk", detail="no enabled SIP account is assigned for this SMPP user"),
                    "tts_cached": was_cached,
                    "hash": hkey,
                        "smpp_username": sms.smpp_username or "",
                        "static_template_applied": rendered_from_static_template,
                },
            )

        for attempt in range(1, max_attempts + 1):
            sip_result = self.sip_ua.place_outbound_call(
                SipCallRequest(
                    destination_number=phone,
                    audio_path=audio_path,
                    account_id=sip_account.id,
                    display_name=sip_account.display_name or sip_account.label,
                    caller_id=sip_account.from_user or self.settings.outbound_caller_id,
                    timeout_seconds=self.settings.call_answer_timeout,
                    playback_repeats=max(1, int(getattr(self.settings, "playback_repeats", 1) or 1)),
                    playback_pause_ms=max(0, int(getattr(self.settings, "playback_pause_ms", 0) or 0)),
                    enable_recording=bool(getattr(self.settings, "enable_call_recording", False)),
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
                    "host": sip_account.host,
                    "port": sip_account.port,
                    "username": sip_account.username,
                    "password": sip_account.password,
                    "transport": (sip_account.transport or "udp").upper(),
                    "caller_id": sip_account.from_user or self.settings.outbound_caller_id,
                    "enabled": sip_account.enabled,
                    "proxy_uri": sip_account.outbound_proxy,
                    "concurrency_limit": sip_account.concurrency_limit,
                    "extra": {
                        "host": sip_account.host,
                        "port": sip_account.port,
                        "from_domain": sip_account.from_domain,
                        "register": sip_account.register_enabled,
                    },
                },
            )
            sip_call_id = sip_result.call_id or sip_call_id
            recording_path = sip_result.recording_path or ""

            if sip_result.success:
                return GatewayResult(
                    success=True,
                    phone_number=phone,
                    text_spoken=spoken_text,
                    audio_path=audio_path,
                    was_cached=was_cached,
                    sip_call_id=sip_call_id,
                    sip_account_id=sip_account_id,
                    recording_path=recording_path,
                    delivered=bool(sip_result.delivered),
                    read=bool(sip_result.read),
                    answered=bool(sip_result.answered),
                    details={
                        "transport": "sip-ua",
                        "sip_result": sip_result.details,
                        "tts_cached": was_cached,
                        "hash": hkey,
                        "attempts": attempt,
                        "max_attempts": max_attempts,
                        "retry_interval_seconds": retry_interval_seconds,
                        "sip_account_id": sip_account_id,
                        "smpp_username": sms.smpp_username or "",
                        "static_template_applied": rendered_from_static_template,
                        "delivery_state": "DELIVRD" if sip_result.delivered else "UNDELIV",
                        "read_state": "READ" if sip_result.read else "UNREAD",
                        "answered": bool(sip_result.answered),
                        "playback_seconds": float(sip_result.playback_seconds or 0.0),
                        "audio_duration_seconds": float(sip_result.audio_duration_seconds or 0.0),
                        "recording_path": recording_path,
                    },
                )

            last_error = sip_result.error or sip_result.message or "Outbound voice call failed"
            pending_reason = _derive_pending_reason(stage="sip_trunk", detail=last_error)
            log.warning("Outbound voice attempt %d/%d failed for %s via %s: %s", attempt, max_attempts, phone, sip_account_id, last_error)

            if "concurrency limit reached" in last_error.lower():
                return GatewayResult(
                    success=False,
                    phone_number=phone,
                    text_spoken=spoken_text,
                    audio_path=audio_path,
                    was_cached=was_cached,
                    sip_call_id=sip_call_id,
                    sip_account_id=sip_account_id,
                    recording_path=recording_path,
                    error=last_error,
                    details={
                        "pending_reason": pending_reason,
                        "sip_result": sip_result.details,
                        "tts_cached": was_cached,
                        "hash": hkey,
                        "attempts": attempt,
                        "max_attempts": max_attempts,
                        "retry_interval_seconds": retry_interval_seconds,
                        "sip_account_id": sip_account_id,
                        "smpp_username": sms.smpp_username or "",
                        "smpp_retry_count": retry_count,
                        "static_template_applied": rendered_from_static_template,
                        "recording_path": recording_path,
                    },
                )

            if "was not answered" in last_error.lower():
                return GatewayResult(
                    success=False,
                    phone_number=phone,
                    text_spoken=spoken_text,
                    audio_path=audio_path,
                    was_cached=was_cached,
                    sip_call_id=sip_call_id,
                    sip_account_id=sip_account_id,
                    recording_path=recording_path,
                    error=last_error,
                    details={
                        "pending_reason": pending_reason,
                        "sip_result": sip_result.details,
                        "tts_cached": was_cached,
                        "hash": hkey,
                        "attempts": attempt,
                        "max_attempts": max_attempts,
                        "retry_interval_seconds": retry_interval_seconds,
                        "sip_account_id": sip_account_id,
                        "smpp_username": sms.smpp_username or "",
                        "static_template_applied": rendered_from_static_template,
                        "state": "missed",
                        "recording_path": recording_path,
                    },
                )

            if attempt < max_attempts and queue_retries:
                retry_snapshot = _queue_retry(
                    self.settings,
                    phone_number=phone,
                    provider=sms.provider,
                    body=sms.body,
                    body_preview=spoken_text,
                    attempts=attempt,
                    max_attempts=max_attempts,
                    retry_interval_seconds=retry_interval_seconds,
                    last_error=pending_reason,
                    sip_account_id=sip_account_id,
                    smpp_username=sms.smpp_username or "",
                    audio_path=audio_path,
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
            recording_path=recording_path,
            error=last_error or "Outbound voice call failed",
            details={
                "pending_reason": _derive_pending_reason(stage="sip_trunk", detail=last_error or "outbound voice call failed"),
                "sip_result": None,
                "tts_cached": was_cached,
                "hash": hkey,
                "attempts": max_attempts,
                "max_attempts": max_attempts,
                "retry_interval_seconds": retry_interval_seconds,
                "sip_account_id": sip_account_id,
                "smpp_username": sms.smpp_username or "",
                "smpp_retry_count": retry_count,
                "static_template_applied": rendered_from_static_template,
                "recording_path": recording_path,
            },
        )
