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


# ─────────────────────────────────────────────────────────────────────────────
# Google Cloud TTS
# ─────────────────────────────────────────────────────────────────────────────

class GoogleTTSBackend(TTSBackend):
    def __init__(self, settings: Settings):
        from google.cloud import texttospeech  # type: ignore
        import os
        if settings.google_credentials_json:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_credentials_json)
        self.client = texttospeech.TextToSpeechClient()
        self.voice = texttospeech.VoiceSelectionParams(
            language_code=settings.tts_language,
            name=settings.tts_voice,
        )
        self.audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            speaking_rate=settings.tts_speaking_rate,
        )
        self._tts_mod = texttospeech

    def synthesize(self, text: str) -> bytes:
        synthesis_input = self._tts_mod.SynthesisInput(text=text)
        response = self.client.synthesize_speech(
            input=synthesis_input,
            voice=self.voice,
            audio_config=self.audio_config,
        )
        return response.audio_content


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
        self.api_key = settings.elevenlabs_api_key
        self.voice_id = settings.elevenlabs_voice_id
        self.requests = requests

    def synthesize(self, text: str) -> bytes:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
            "output_format": "pcm_8000",
        }
        r = self.requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        # Wrap raw PCM in WAV
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(r.content)
        return out.getvalue()


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


def get_backend(settings: Settings) -> TTSBackend:
    global _backend_instance
    if _backend_instance is None:
        cls = _BACKENDS.get(settings.tts_provider)
        if cls is None:
            raise ValueError(f"Unknown TTS provider: {settings.tts_provider}")
        _backend_instance = cls(settings)
        log.info("TTS backend initialised: %s", settings.tts_provider)
    return _backend_instance


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

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
        raw_wav = backend.synthesize(text)

        # Normalise to Asterisk-compatible format
        normalised = _ensure_wav_format(raw_wav)

        # Build final WAV with repeats + silence gaps
        if self.settings.playback_repeats > 1:
            silence = _generate_silence(self.settings.playback_pause_ms)
            parts: list[bytes] = []
            for i in range(self.settings.playback_repeats):
                parts.append(normalised)
                if i < self.settings.playback_repeats - 1:
                    parts.append(silence)
            final_wav = _concat_wavs(parts)
        else:
            final_wav = normalised

        path = self.cache.store_audio(hkey, final_wav)
        return path, False

    def hash_for(self, text: str) -> str:
        return text_hash(text, self.settings.tts_voice, self.settings.tts_language)
