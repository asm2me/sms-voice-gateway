from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _strip_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_int(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    allow_none: bool = False,
) -> int | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field_name} is required")

    text = _strip_text(value)
    if text == "":
        if allow_none:
            return None
        raise ValueError(f"{field_name} is required")

    try:
        parsed = int(text)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be greater than or equal to {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} must be less than or equal to {maximum}")
    return parsed


def _coerce_float(value: Any, field_name: str, *, minimum: float | None = None) -> float:
    text = _strip_text(value)
    if text == "":
        raise ValueError(f"{field_name} is required")

    try:
        parsed = float(text)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be greater than or equal to {minimum}")
    return parsed


def _normalize_codec_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []

    alias_map = {
        "g729": "G729",
        "g.729": "G729",
        "g723": "G723",
        "g723.1": "G723",
        "g.723": "G723",
        "g.723.1": "G723",
    }

    if isinstance(value, str):
        raw_parts = re.split(r"[,;\s]+", value)
    else:
        raw_parts = list(value)

    seen: set[str] = set()
    codecs: list[str] = []
    for part in raw_parts:
        normalized = alias_map.get(_strip_text(part).lower(), _strip_text(part).upper())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        codecs.append(normalized)
    return codecs


class SIPAccount(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str = ""
    host: str = ""
    username: str = ""
    password: str = ""
    transport: Literal["udp", "tcp", "tls"] = "udp"
    port: int = Field(default=5060, ge=1, le=65535)
    domain: str = ""
    display_name: str = ""
    from_user: str = ""
    from_domain: str = ""
    enabled: bool = True
    default_for_outbound: bool = False
    register_enabled: bool = Field(default=True, alias="register", validation_alias="register", serialization_alias="register")
    outbound_proxy: str = ""
    concurrency_limit: int = Field(default=0, ge=0)
    preferred_codecs: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "id",
        "label",
        "host",
        "username",
        "password",
        "domain",
        "display_name",
        "from_user",
        "from_domain",
        "outbound_proxy",
        mode="before",
    )
    @classmethod
    def _strip_text_fields(cls, value: Any) -> str:
        return _strip_text(value)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value:
            raise ValueError("id is required")
        return value

    @field_validator("preferred_codecs", mode="before")
    @classmethod
    def _normalize_preferred_codecs(cls, value: Any) -> list[str]:
        return _normalize_codec_list(value)


class SMPPAccount(BaseModel):
    id: str
    label: str = ""
    username: str = ""
    password: str = ""
    enabled: bool = True
    default_for_inbound: bool = False
    default_sip_account_id: str = ""
    delivery_retry_count: int | None = None
    delivery_retry_interval_seconds: int | None = None
    static_default_message_enabled: bool = False
    static_default_message_template: str = ""
    uploaded_audio_path: str = ""
    uploaded_audio_original_name: str = ""
    static_message_part_audio: dict[str, dict[str, str]] = Field(default_factory=dict)
    static_message_digit_audio: dict[str, dict[str, str]] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "id",
        "label",
        "username",
        "password",
        "default_sip_account_id",
        "static_default_message_template",
        "uploaded_audio_path",
        "uploaded_audio_original_name",
        mode="before",
    )
    @classmethod
    def _strip_text_fields(cls, value: Any) -> str:
        return _strip_text(value)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value:
            raise ValueError("id is required")
        return value

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        if not value:
            raise ValueError("username is required")
        return value

    @field_validator("delivery_retry_count", "delivery_retry_interval_seconds", mode="before")
    @classmethod
    def _validate_optional_retry_values(cls, value: Any, info) -> int | None:
        return _coerce_int(value, info.field_name or "value", minimum=0, allow_none=True)


class SystemUser(BaseModel):
    id: str
    username: str
    password: str = ""
    role: str = "Administrator"
    enabled: bool = True
    auth_source: str = "Admin Portal"
    permissions: list[str] = Field(default_factory=list)

    @field_validator("id", "username", "password", "role", "auth_source", mode="before")
    @classmethod
    def _strip_text_fields(cls, value: Any) -> str:
        return _strip_text(value)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value:
            raise ValueError("id is required")
        return value

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        if not value:
            raise ValueError("username is required")
        return value

    @field_validator("permissions", mode="before")
    @classmethod
    def _normalize_permissions(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []

        if isinstance(value, str):
            raw_values = re.split(r"[,;\n]+", value)
        else:
            raw_values = list(value)

        allowed = set(get_system_user_permissions())
        seen: set[str] = set()
        permissions: list[str] = []
        for item in raw_values:
            permission = _strip_text(item)
            if not permission or permission in seen:
                continue
            if allowed and permission not in allowed:
                raise ValueError(f"Invalid permission: {permission}")
            seen.add(permission)
            permissions.append(permission)
        return permissions


SYSTEM_USER_PERMISSION_GROUPS: list[dict[str, object]] = [
    {
        "group": "Overview",
        "permissions": [
            "Overview — Read",
        ],
    },
    {
        "group": "Health",
        "permissions": [
            "Health — Read",
            "Health — Restart",
        ],
    },
    {
        "group": "Configuration",
        "permissions": [
            "Configuration — Read",
            "Configuration — Write",
        ],
    },
    {
        "group": "Delivery Reports",
        "permissions": [
            "Delivery Reports — Read",
            "Delivery Reports — Write",
        ],
    },
    {
        "group": "Queue",
        "permissions": [
            "Queue — Read",
            "Queue — Create",
            "Queue — Update",
            "Queue — Delete",
            "Queue — Batch Update",
            "Queue — Batch Delete",
        ],
    },
    {
        "group": "Test Send",
        "permissions": [
            "Test Send — Execute",
        ],
    },
    {
        "group": "System Users",
        "permissions": [
            "System Users — Read",
            "System Users — Write",
        ],
    },
]


def get_system_user_permissions() -> list[str]:
    permissions: list[str] = []
    for group in SYSTEM_USER_PERMISSION_GROUPS:
        permissions.extend(group["permissions"])  # type: ignore[arg-type]
    return permissions


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Bootstrap / runtime ──────────────────────────────────────────────────
    # These remain env-driven so the app can start and the admin UI can load.
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    admin_username: str = "admin"
    admin_password: str = "change-me"
    delivery_report_store_path: Optional[str] = None

    # ── Admin-managed operational settings ───────────────────────────────────
    webhook_secret: str = ""          # shared secret to validate SMS providers

    # TTS
    tts_provider: Literal["google", "aws_polly", "openai", "elevenlabs"] = "google"
    tts_language: str = "en-US"
    tts_voice: str = "en-US-Neural2-F"   # default / primary voice
    tts_speaking_rate: float = 0.90       # slightly slower for OTP clarity
    tts_audio_encoding: str = "LINEAR16"  # PCM WAV
    tts_multilingual_mode: bool = True
    tts_secondary_language: str = "ar-EG"
    tts_secondary_voice: str = "ar-EG-Neural2-A"

    # Google Cloud TTS
    google_credentials_json: Optional[str] = None  # path to service-account JSON

    # AWS Polly
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: str = "us-east-1"
    aws_polly_voice_id: str = "Joanna"
    aws_polly_engine: Literal["standard", "neural"] = "neural"

    # OpenAI TTS
    openai_api_key: Optional[str] = None
    openai_tts_model: str = "tts-1-hd"
    openai_tts_voice: str = "nova"

    # ElevenLabs
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: str = "rUaPbzcZIu8df8iNL9WZ"

    # Audio cache
    audio_cache_dir: str = "./audio_cache"
    asterisk_sounds_dir: str = "/var/lib/asterisk/sounds/sms_otp"
    audio_cache_ttl: int = 7 * 24 * 3600   # 7 days

    # Asterisk AMI
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_username: str = "manager"
    ami_secret: str = "manager_secret"
    ami_connection_timeout: int = 10
    ami_response_timeout: int = 30

    # SIP trunk / channel template used in AMI Originate
    sip_channel_prefix: str = "PJSIP/trunk"
    outbound_caller_id: str = "OTP Service <0000>"
    call_answer_timeout: int = 30
    asterisk_context: str = "sms-voice-otp"
    asterisk_exten: str = "s"
    asterisk_priority: str = "1"

    # Multi-account SIP / SMPP configuration persisted in JSON
    sip_accounts: list[SIPAccount] = Field(default_factory=list)
    smpp_accounts: list[SMPPAccount] = Field(default_factory=list)
    smpp_sip_assignments: dict[str, str] = Field(default_factory=dict)
    system_users: list[SystemUser] = Field(default_factory=list)

    # Retry policy
    delivery_retry_count: int = 2
    delivery_retry_interval_seconds: int = 60

    # Redis cache
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "sms_gw:"

    # SMPP listener
    smpp_enabled: bool = False
    smpp_host: str = "0.0.0.0"
    smpp_port: int = 7070
    smpp_username: str = "smpp"
    smpp_password: str = "smpp_secret"

    # SMS parsing
    phone_regex: str = r"(?:CALL|TO|call|to)?[:\s]*(\+?[\d\s\-\(\)]{7,20})"
    strip_call_prefix: bool = True

    # Repeat playback
    playback_repeats: int = 3     # how many times to repeat the OTP audio
    playback_pause_ms: int = 1500  # silence between repeats (ms)

    # Call audio / media device
    use_null_sound_device: bool = True

    # Call recording
    enable_call_recording: bool = False

    # Reporting
    delivery_report_max_items: int = 1000

    @field_validator(
        "host",
        "admin_username",
        "admin_password",
        "delivery_report_store_path",
        "webhook_secret",
        "tts_language",
        "tts_voice",
        "tts_audio_encoding",
        "tts_secondary_language",
        "tts_secondary_voice",
        "google_credentials_json",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_region",
        "aws_polly_voice_id",
        "openai_api_key",
        "openai_tts_model",
        "openai_tts_voice",
        "elevenlabs_api_key",
        "elevenlabs_voice_id",
        "audio_cache_dir",
        "asterisk_sounds_dir",
        "ami_host",
        "ami_username",
        "ami_secret",
        "sip_channel_prefix",
        "outbound_caller_id",
        "asterisk_context",
        "asterisk_exten",
        "asterisk_priority",
        "redis_url",
        "redis_prefix",
        "smpp_host",
        "smpp_username",
        "smpp_password",
        "phone_regex",
        mode="before",
    )
    @classmethod
    def _strip_settings_text_fields(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("tts_speaking_rate", mode="before")
    @classmethod
    def _validate_tts_speaking_rate(cls, value: Any) -> float:
        return _coerce_float(value, "tts_speaking_rate", minimum=0.01)

    @field_validator(
        "port",
        "ami_port",
        "smpp_port",
        "audio_cache_ttl",
        "ami_connection_timeout",
        "ami_response_timeout",
        "call_answer_timeout",
        "playback_repeats",
        "playback_pause_ms",
        "delivery_retry_count",
        "delivery_retry_interval_seconds",
        "delivery_report_max_items",
        mode="before",
    )
    @classmethod
    def _validate_settings_int_fields(cls, value: Any, info) -> int:
        field_name = info.field_name or "value"
        bounds: dict[str, tuple[int | None, int | None]] = {
            "port": (1, 65535),
            "ami_port": (1, 65535),
            "smpp_port": (1, 65535),
            "audio_cache_ttl": (1, None),
            "ami_connection_timeout": (1, None),
            "ami_response_timeout": (1, None),
            "call_answer_timeout": (1, None),
            "playback_repeats": (1, None),
            "playback_pause_ms": (0, None),
            "delivery_retry_count": (0, None),
            "delivery_retry_interval_seconds": (0, None),
            "delivery_report_max_items": (1, None),
        }
        minimum, maximum = bounds.get(field_name, (None, None))
        parsed = _coerce_int(value, field_name, minimum=minimum, maximum=maximum)
        assert parsed is not None
        return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
