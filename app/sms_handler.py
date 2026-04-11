"""
Core business logic: parse an incoming SMS → synthesise voice → originate call.

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
from typing import Optional

from .ami_service import AMIService
from .cache import AudioCache, RateLimiter
from .config import Settings
from .tts_service import TTSService

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IncomingSMS:
    body: str
    from_number: str = ""        # E.164 sender (populated by SMS provider webhook)
    to_number: str = ""          # SMS receiver (your gateway number)
    destination: str = ""        # explicit override from webhook payload
    provider: str = "generic"    # twilio | vonage | generic


@dataclass
class GatewayResult:
    success: bool
    phone_number: str = ""
    text_spoken: str = ""
    audio_path: str = ""
    was_cached: bool = False
    ami_action_id: str = ""
    error: str = ""
    details: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Phone-number extraction
# ─────────────────────────────────────────────────────────────────────────────

# Explicit prefix patterns: CALL:+123, TO:+123, DEST:+123
_PREFIX_RE = re.compile(
    r"(?:CALL|TO|DEST|PHONE|NUMBER)[:\s]+(\+?[\d\s\-\(\)\.]{7,20})",
    re.IGNORECASE,
)
# General E.164-ish number anywhere in text
_NUMBER_RE = re.compile(r"(\+[\d]{7,15}|\b0[\d]{8,14}\b|\b[\d]{10,15}\b)")

# Strip the "CALL:+123 " prefix from the spoken text
_STRIP_RE = re.compile(
    r"^(?:CALL|TO|DEST|PHONE|NUMBER)[:\s]+\+?[\d\s\-\(\)\.]{7,20}\s*",
    re.IGNORECASE,
)


def extract_destination(sms: IncomingSMS) -> tuple[str, str]:
    """
    Returns (phone_number, cleaned_text_to_speak).
    Raises ValueError if no phone number can be determined.
    """
    body = sms.body.strip()

    # 1. Explicit prefix
    m = _PREFIX_RE.search(body)
    if m:
        phone = _normalise_number(m.group(1))
        text = _STRIP_RE.sub("", body).strip()
        return phone, text or body

    # 2. First E.164 / long number in body
    m = _NUMBER_RE.search(body)
    if m:
        phone = _normalise_number(m.group(1))
        # Remove the number from spoken text
        text = body.replace(m.group(1), "").strip(" ,:;-")
        return phone, text or body

    # 3. Explicit override from webhook
    if sms.destination:
        return _normalise_number(sms.destination), body

    # 4. Fallback to SMS sender
    if sms.from_number:
        return _normalise_number(sms.from_number), body

    raise ValueError("Cannot determine destination phone number from SMS")


def _normalise_number(raw: str) -> str:
    """Strip non-digit chars except leading +."""
    cleaned = re.sub(r"[^\d+]", "", raw.strip())
    if not cleaned:
        raise ValueError(f"Invalid phone number: {raw!r}")
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Gateway handler
# ─────────────────────────────────────────────────────────────────────────────

class SMSGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.audio_cache = AudioCache(settings)
        self.tts = TTSService(settings, self.audio_cache)
        self.ami = AMIService(settings)
        self.rate_limiter = RateLimiter(settings)

    def process(self, sms: IncomingSMS) -> GatewayResult:
        # 1. Parse destination number and the text to speak
        try:
            phone, spoken_text = extract_destination(sms)
        except ValueError as exc:
            log.warning("SMS parse error: %s | body=%r", exc, sms.body[:80])
            return GatewayResult(success=False, error=str(exc))

        if not spoken_text:
            return GatewayResult(
                success=False,
                phone_number=phone,
                error="Empty text after stripping phone number",
            )

        # 2. Rate limiting
        allowed, reason = self.rate_limiter.is_allowed(phone)
        if not allowed:
            log.warning("Rate limit for %s: %s", phone, reason)
            return GatewayResult(
                success=False,
                phone_number=phone,
                error=f"Rate limited: {reason}",
            )

        # 3. TTS synthesis (with cache)
        try:
            audio_path, was_cached = self.tts.get_or_create_audio(spoken_text)
        except Exception as exc:
            log.exception("TTS failed for text=%r", spoken_text[:60])
            return GatewayResult(
                success=False,
                phone_number=phone,
                text_spoken=spoken_text,
                error=f"TTS error: {exc}",
            )

        # 4. Build Asterisk sound reference
        hkey = self.tts.hash_for(spoken_text)
        asterisk_ref = self.audio_cache.asterisk_sound_ref(hkey)

        # 5. Originate SIP call via AMI
        ami_result = self.ami.originate_playback(
            phone_number=phone,
            asterisk_sound_ref=asterisk_ref,
            extra_vars={"OTP_TEXT": spoken_text[:80]},
        )

        return GatewayResult(
            success=ami_result.success,
            phone_number=phone,
            text_spoken=spoken_text,
            audio_path=audio_path,
            was_cached=was_cached,
            ami_action_id=ami_result.action_id,
            error="" if ami_result.success else ami_result.message,
            details={
                "ami_response": ami_result.response,
                "tts_cached": was_cached,
                "hash": hkey,
                "rate_counts": self.rate_limiter.get_counts(phone),
            },
        )
