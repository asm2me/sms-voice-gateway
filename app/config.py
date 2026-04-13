from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SIPAccount(BaseModel):
    id: str
    label: str = ""
    host: str = ""
    username: str = ""
    password: str = ""
    transport: Literal["udp", "tcp", "tls"] = "udp"
    port: int = 5060
    domain: str = ""
    display_name: str = ""
    from_user: str = ""
    from_domain: str = ""
    enabled: bool = True
    default_for_outbound: bool = False
    register: bool = True
    outbound_proxy: str = ""
    concurrency_limit: int = 1
    extra: dict[str, Any] = Field(default_factory=dict)


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
    extra: dict[str, Any] = Field(default_factory=dict)


class SystemUser(BaseModel):
    id: str
    username: str
    password: str = ""
    role: str = "Administrator"
    enabled: bool = True
    auth_source: str = "Admin Portal"
    permissions: list[str] = Field(default_factory=list)


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
    tts_voice: str = "en-US-Neural2-F"   # Google voice name
    tts_speaking_rate: float = 0.90       # slightly slower for OTP clarity
    tts_audio_encoding: str = "LINEAR16"  # PCM WAV

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

    # Rate limiting
    rate_limit_hourly: int = 3    # max calls per number per hour
    rate_limit_daily: int = 10    # max calls per number per day

    # SMS parsing
    phone_regex: str = r"(?:CALL|TO|call|to)?[:\s]*(\+?[\d\s\-\(\)]{7,20})"
    strip_call_prefix: bool = True

    # Repeat playback
    playback_repeats: int = 3     # how many times to repeat the OTP audio
    playback_pause_ms: int = 1500  # silence between repeats (ms)

    # Reporting
    delivery_report_max_items: int = 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
