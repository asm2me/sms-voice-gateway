from __future__ import annotations
from functools import lru_cache
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Server ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    webhook_secret: str = ""          # shared secret to validate SMS providers

    # ── TTS ───────────────────────────────────────────────────────────────────
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
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"

    # ── Audio cache ───────────────────────────────────────────────────────────
    audio_cache_dir: str = "./audio_cache"
    # Asterisk-accessible path (symlink or shared mount if different machine)
    asterisk_sounds_dir: str = "/var/lib/asterisk/sounds/sms_otp"
    # How long to keep cached audio (seconds)
    audio_cache_ttl: int = 7 * 24 * 3600   # 7 days

    # ── Asterisk AMI ──────────────────────────────────────────────────────────
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_username: str = "manager"
    ami_secret: str = "manager_secret"
    ami_connection_timeout: int = 10
    ami_response_timeout: int = 30

    # SIP trunk / channel template used in AMI Originate
    # Examples: "SIP/my_trunk", "PJSIP/my_trunk", "DAHDI/g1"
    sip_channel_prefix: str = "PJSIP/trunk"
    # CallerID presented on outbound calls
    outbound_caller_id: str = "OTP Service <0000>"
    # Seconds to wait for the remote to answer
    call_answer_timeout: int = 30
    # Asterisk dialplan context that handles the answered call
    asterisk_context: str = "sms-voice-otp"
    asterisk_exten: str = "s"
    asterisk_priority: str = "1"

    # ── Redis cache ───────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "sms_gw:"

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_hourly: int = 3    # max calls per number per hour
    rate_limit_daily: int = 10    # max calls per number per day

    # ── SMS parsing ───────────────────────────────────────────────────────────
    # Regex applied to SMS body to extract destination phone number.
    # First capturing group must be the number.
    phone_regex: str = r"(?:CALL|TO|call|to)?[:\s]*(\+?[\d\s\-\(\)]{7,20})"
    # Prefix to strip from OTP text before TTS (e.g. "CALL:+123 ")
    strip_call_prefix: bool = True

    # ── Repeat playback ───────────────────────────────────────────────────────
    playback_repeats: int = 3     # how many times to repeat the OTP audio
    playback_pause_ms: int = 1500  # silence between repeats (ms)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
