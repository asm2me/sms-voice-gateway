"""
Text-to-Speech service with pluggable backends.

All backends produce 16-bit PCM WAV at 8 kHz mono (Asterisk-native format).
If the backend returns a different sample rate the audio is resampled with
audioop (stdlib) so no heavy DSP library is required.
"""
from __future__ import annotations

import audioop
import io
import logging
import re
import struct
import wave
from abc import ABC, abstractmethod
from typing import Optional

from .cache import AudioCache, text_hash
from .config import Settings

log = logging.getLogger(__name__)

TARGET_RATE = 8000   # Hz – Asterisk default
TARGET_CHANNELS = 1  # mono
TARGET_SAMPWIDTH = 2  # 16-bit


# ─────────────────────────────────────────────────────────────────────────────
# WAV utilities
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_wav_format(wav_bytes: bytes) -> bytes:
    """Normalise any PCM WAV to 8 kHz / 16-bit / mono."""
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        frames = w.readframes(w.getnframes())

    # Convert to 16-bit
    if sampwidth == 1:        # 8-bit unsigned → 16-bit signed
        frames = audioop.bias(frames, 1, -128)
        frames = audioop.lin2lin(frames, 1, 2)
        sampwidth = 2
    elif sampwidth == 4:      # 32-bit → 16-bit
        frames = audioop.lin2lin(frames, 4, 2)
        sampwidth = 2

    # Convert to mono
    if n_channels == 2:
        frames = audioop.tomono(frames, sampwidth, 0.5, 0.5)
        n_channels = 1

    # Resample to TARGET_RATE
    if framerate != TARGET_RATE:
        frames, _ = audioop.ratecv(frames, sampwidth, n_channels, framerate, TARGET_RATE, None)
        framerate = TARGET_RATE

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(frames)
    return out.getvalue()


def _generate_silence(ms: int) -> bytes:
    """Return WAV bytes of silence for the given duration."""
    n_frames = int(TARGET_RATE * ms / 1000)
    silence = b"\x00" * n_frames * TARGET_SAMPWIDTH
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(TARGET_CHANNELS)
        w.setsampwidth(TARGET_SAMPWIDTH)
        w.setframerate(TARGET_RATE)
        w.writeframes(silence)
    return out.getvalue()


def _concat_wavs(wav_list: list[bytes]) -> bytes:
    """Concatenate multiple same-format WAV files into one."""
    all_frames = b""
    for wav in wav_list:
        buf = io.BytesIO(wav)
        with wave.open(buf, "rb") as w:
            all_frames += w.readframes(w.getnframes())
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(TARGET_CHANNELS)
        w.setsampwidth(TARGET_SAMPWIDTH)
        w.setframerate(TARGET_RATE)
        w.writeframes(all_frames)
    return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Backend ABC
# ─────────────────────────────────────────────────────────────────────────────

class TTSBackend(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """Return raw WAV bytes (any format – will be normalised)."""

    def synthesize_segments(self, segments: list[tuple[str, str, str]]) -> bytes:
        wavs = [self.synthesize(segment_text) for segment_text, _language, _voice in segments if segment_text.strip()]
        if not wavs:
            return self.synthesize("")
        if len(wavs) == 1:
            return wavs[0]
        return _concat_wavs([_ensure_wav_format(wav) for wav in wavs])


# ─────────────────────────────────────────────────────────────────────────────
# Google Cloud TTS
# ─────────────────────────────────────────────────────────────────────────────

def _google_language_from_voice_name(voice_name: str) -> str:
    voice = (voice_name or "").strip()
    if not voice:
        return ""
    parts = voice.split("-")
    if len(parts) >= 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
        return f"{parts[0]}-{parts[1]}".lower()
    return ""


class GoogleTTSBackend(TTSBackend):
    def __init__(self, settings: Settings):
        from google.cloud import texttospeech  # type: ignore
        import os
        if settings.google_credentials_json:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_credentials_json)
        self.client = texttospeech.TextToSpeechClient()
        self.settings = settings
        self._tts_mod = texttospeech
        self.audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            speaking_rate=settings.tts_speaking_rate,
        )
        self.voice = self._build_voice_params(settings.tts_language, settings.tts_voice)

    def _build_voice_params(self, language: str, voice_name: str):
        requested_language = (language or "").strip()
        configured_voice_name = (voice_name or "").strip()
        voice_language = _google_language_from_voice_name(configured_voice_name)
        effective_language = requested_language or voice_language
        if voice_language and requested_language and requested_language.lower() != voice_language:
            log.warning(
                "Google TTS language/voice mismatch detected; using voice language '%s' for voice '%s' instead of requested language '%s'",
                voice_language,
                configured_voice_name,
                requested_language,
            )
            effective_language = voice_language
        return self._tts_mod.VoiceSelectionParams(
            language_code=effective_language,
            name=configured_voice_name,
        )

    def _synthesize_with_voice(self, text: str, language: str, voice_name: str) -> bytes:
        synthesis_input = self._tts_mod.SynthesisInput(text=text)
        voice_params = self._build_voice_params(language, voice_name)
        try:
            response = self.client.synthesize_speech(
                input=synthesis_input,
                voice=voice_params,
                audio_config=self.audio_config,
            )
            return response.audio_content
        except Exception as exc:
            message = str(exc)
            if voice_name and "does not exist" in message:
                fallback_language = _google_language_from_voice_name(voice_name) or (language or "").strip()
                log.warning(
                    "Google TTS voice '%s' was not found; retrying with language '%s' and provider default voice",
                    voice_name,
                    fallback_language,
                )
                fallback_voice = self._tts_mod.VoiceSelectionParams(
                    language_code=fallback_language,
                )
                response = self.client.synthesize_speech(
                    input=synthesis_input,
                    voice=fallback_voice,
                    audio_config=self.audio_config,
                )
                return response.audio_content
            raise

    def synthesize(self, text: str) -> bytes:
        return self._synthesize_with_voice(text, self.settings.tts_language, self.settings.tts_voice)

    def synthesize_segments(self, segments: list[tuple[str, str, str]]) -> bytes:
        wavs = [
            _ensure_wav_format(self._synthesize_with_voice(segment_text, language, voice_name))
            for segment_text, language, voice_name in segments
            if segment_text.strip()
        ]
        if not wavs:
            return self.synthesize("")
        if len(wavs) == 1:
            return wavs[0]
        return _concat_wavs(wavs)


# ─────────────────────────────────────────────────────────────────────────────
# AWS Polly
# ─────────────────────────────────────────────────────────────────────────────

class AWSPollyBackend(TTSBackend):
    def __init__(self, settings: Settings):
        import boto3  # type: ignore
        kwargs: dict = {"region_name": settings.aws_region}
        if settings.aws_access_key_id:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
        self.client = boto3.client("polly", **kwargs)
        self.voice_id = settings.aws_polly_voice_id
        self.engine = settings.aws_polly_engine

    def synthesize(self, text: str) -> bytes:
        response = self.client.synthesize_speech(
            Text=text,
            OutputFormat="pcm",       # raw PCM – we wrap in WAV below
            SampleRate="8000",
            VoiceId=self.voice_id,
            Engine=self.engine,
        )
        pcm_bytes = response["AudioStream"].read()
        # Wrap raw PCM in WAV container
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(pcm_bytes)
        return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI TTS
# ─────────────────────────────────────────────────────────────────────────────

class OpenAITTSBackend(TTSBackend):
    def __init__(self, settings: Settings):
        from openai import OpenAI  # type: ignore
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_tts_model
        self.voice = settings.openai_tts_voice

    def synthesize(self, text: str) -> bytes:
        response = self.client.audio.speech.create(
            model=self.model,
            voice=self.voice,
            input=text,
            response_format="wav",
        )
        return response.content


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs TTS
# ─────────────────────────────────────────────────────────────────────────────

class ElevenLabsTTSBackend(TTSBackend):
    def __init__(self, settings: Settings):
        import requests  # type: ignore

        self.api_key = (settings.elevenlabs_api_key or "").strip()
        self.voice_id = (settings.elevenlabs_voice_id or "").strip()
        self.requests = requests

    def _voice_exists(self) -> tuple[bool, str]:
        url = f"https://api.elevenlabs.io/v1/voices/{self.voice_id}"
        response = self.requests.get(
            url,
            headers={"xi-api-key": self.api_key},
            timeout=20,
        )
        if response.status_code == 404:
            return False, f"ElevenLabs voice id '{self.voice_id}' was not found for this account."
        if response.status_code == 401:
            return False, "ElevenLabs API key was rejected while validating the voice id."
        if not response.ok:
            detail = response.text.strip()
            if len(detail) > 300:
                detail = detail[:300] + "..."
            return False, f"ElevenLabs voice validation failed with HTTP {response.status_code}: {detail or response.reason}"
        return True, "ok"

    def synthesize(self, text: str) -> bytes:
        if not self.api_key:
            raise ValueError("ElevenLabs API key is not configured")
        if not self.voice_id:
            raise ValueError("ElevenLabs voice id is not configured")

        voice_ok, voice_detail = self._voice_exists()
        if not voice_ok:
            raise ValueError(voice_detail)

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
        }
        r = self.requests.post(
            url,
            params={"output_format": "mp3_44100_128"},
            json=payload,
            headers=headers,
            timeout=30,
        )
        if r.status_code == 404:
            raise ValueError(
                f"ElevenLabs text-to-speech endpoint returned 404 for voice id '{self.voice_id}'. "
                "The configured voice may not exist in this workspace/account."
            )
        if r.status_code == 402:
            detail = r.text.strip()
            if len(detail) > 300:
                detail = detail[:300] + "..."
            raise ValueError(
                "ElevenLabs rejected synthesis with HTTP 402 Payment Required. "
                "The voice id is valid, but this API key/account does not currently have access to generate audio "
                "(for example due to billing, quota, or plan limits). "
                f"{detail or ''}".strip()
            )
        r.raise_for_status()
        return r.content


# ─────────────────────────────────────────────────────────────────────────────
# Backend factory
# ─────────────────────────────────────────────────────────────────────────────

_BACKENDS = {
    "google": GoogleTTSBackend,
    "aws_polly": AWSPollyBackend,
    "openai": OpenAITTSBackend,
    "elevenlabs": ElevenLabsTTSBackend,
}

_backend_instance: Optional[TTSBackend] = None
_backend_provider: str | None = None


def get_backend(settings: Settings) -> TTSBackend:
    global _backend_instance, _backend_provider
    requested_provider = (settings.tts_provider or "").strip()

    if _backend_instance is None or _backend_provider != requested_provider:
        cls = _BACKENDS.get(requested_provider)
        if cls is None:
            raise ValueError(f"Unknown TTS provider: {requested_provider}")
        _backend_instance = cls(settings)
        _backend_provider = requested_provider
        log.info("TTS backend initialised: %s", requested_provider)
    return _backend_instance


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _contains_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text or ""))


def _contains_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _split_multilingual_segments(text: str, settings: Settings) -> list[tuple[str, str, str]]:
    primary_language = (settings.tts_language or "en-US").strip()
    primary_voice = (settings.tts_voice or "").strip()
    secondary_language = (settings.tts_secondary_language or primary_language).strip()
    secondary_voice = (settings.tts_secondary_voice or primary_voice).strip()

    if not text.strip():
        return [(text, primary_language, primary_voice)]

    if not getattr(settings, "tts_multilingual_mode", False):
        return [(text, primary_language, primary_voice)]

    if not (_contains_arabic(text) and _contains_latin(text)):
        return [(text, primary_language, primary_voice)]

    token_pattern = re.compile(r"(\s+|[A-Za-z0-9@#:_./+\-]+|[\u0600-\u06FF0-9]+|[^\w\s])", re.UNICODE)
    tokens = [token for token in token_pattern.findall(text) if token]
    segments: list[tuple[str, str, str]] = []

    current_text = ""
    current_language = ""
    current_voice = ""

    def classify(token: str) -> tuple[str, str]:
        if _contains_arabic(token):
            return secondary_language, secondary_voice
        if _contains_latin(token):
            return primary_language, primary_voice
        return current_language or primary_language, current_voice or primary_voice

    def flush() -> None:
        nonlocal current_text, current_language, current_voice
        if current_text:
            segments.append((current_text, current_language or primary_language, current_voice or primary_voice))
            current_text = ""
            current_language = ""
            current_voice = ""

    for token in tokens:
        token_language, token_voice = classify(token)
        if not current_text:
            current_text = token
            current_language = token_language
            current_voice = token_voice
            continue
        if token.isspace() or token_language == current_language:
            current_text += token
            continue
        flush()
        current_text = token
        current_language = token_language
        current_voice = token_voice

    flush()
    return segments or [(text, primary_language, primary_voice)]


class TTSService:
    def __init__(self, settings: Settings, audio_cache: AudioCache):
        self.settings = settings
        self.cache = audio_cache

    def get_or_create_audio(self, text: str) -> tuple[str, bool]:
        """
        Returns (local_wav_path, was_cached).
        Generates and caches TTS audio if not already cached.
        The returned WAV is 8 kHz / 16-bit / mono (Asterisk-ready).
        """
        hkey = text_hash(text, self.settings.tts_voice, self.settings.tts_language)

        cached = self.cache.get_audio_path(hkey)
        if cached:
            return cached, True

        log.info("TTS synthesis for hash=%s text=%r", hkey, text[:60])
        backend = get_backend(self.settings)
        segments = _split_multilingual_segments(text, self.settings)
        raw_wav = (
            backend.synthesize_segments(segments)
            if len(segments) > 1
            else backend.synthesize(text)
        )

        # Normalise to Asterisk-compatible format
        normalised = _ensure_wav_format(raw_wav)

        path = self.cache.store_audio(hkey, normalised)
        return path, False

    def hash_for(self, text: str) -> str:
        return text_hash(text, self.settings.tts_voice, self.settings.tts_language)
