"""
Two-level cache:
  L1 – in-process LRU (avoids Redis round-trip for hot keys)
  L2 – Redis (shared between workers / restarts)
  L3 – Filesystem (audio files, keyed by content hash)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import redis

from .config import Settings

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Redis connection (lazy singleton)
# ─────────────────────────────────────────────────────────────────────────────

_redis_client: Optional[redis.Redis] = None


def get_redis(settings: Settings) -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
    return _redis_client


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _key(prefix: str, settings: Settings, *parts: str) -> str:
    return settings.redis_prefix + prefix + ":" + ":".join(parts)


def text_hash(text: str, voice: str, language: str) -> str:
    """Deterministic 16-char hex key for a (text, voice, language) triple."""
    raw = f"{text.strip().lower()}|{voice}|{language}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Audio file cache (L3 – filesystem + L2 – Redis pointer)
# ─────────────────────────────────────────────────────────────────────────────

class AudioCache:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache_dir = Path(settings.audio_cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _audio_path(self, hash_key: str) -> Path:
        # WAV file stored locally (also symlinked into Asterisk sounds dir)
        return self.cache_dir / f"{hash_key}.wav"

    def _asterisk_path(self, hash_key: str) -> Path:
        return Path(self.settings.asterisk_sounds_dir) / hash_key

    def get_audio_path(self, hash_key: str) -> Optional[str]:
        """Return local WAV path if cached and still valid, else None."""
        r = get_redis(self.settings)
        redis_key = _key("audio", self.settings, hash_key)
        try:
            stored = r.get(redis_key)
            if stored:
                data = json.loads(stored)
                path = Path(data["path"])
                if path.exists():
                    log.debug("Audio cache HIT: %s", hash_key)
                    return str(path)
                # File was deleted – invalidate
                r.delete(redis_key)
        except Exception as exc:
            log.warning("Redis get_audio error: %s", exc)

        # Fallback: check filesystem without Redis
        local = self._audio_path(hash_key)
        if local.exists():
            self._store_redis_pointer(hash_key, str(local))
            return str(local)

        log.debug("Audio cache MISS: %s", hash_key)
        return None

    def store_audio(self, hash_key: str, wav_bytes: bytes) -> str:
        """Persist WAV bytes and register in Redis.  Returns local file path."""
        path = self._audio_path(hash_key)
        path.write_bytes(wav_bytes)
        self._store_redis_pointer(hash_key, str(path))
        self._symlink_to_asterisk(hash_key, path)
        log.info("Audio cached: %s (%d bytes)", hash_key, len(wav_bytes))
        return str(path)

    def _store_redis_pointer(self, hash_key: str, path: str) -> None:
        r = get_redis(self.settings)
        redis_key = _key("audio", self.settings, hash_key)
        try:
            r.setex(
                redis_key,
                self.settings.audio_cache_ttl,
                json.dumps({"path": path, "created": int(time.time())}),
            )
        except Exception as exc:
            log.warning("Redis store audio pointer error: %s", exc)

    def _symlink_to_asterisk(self, hash_key: str, source: Path) -> None:
        """Create/refresh a symlink in Asterisk's sounds directory.
        This lets the AMI Originate reference the file as 'sms_otp/<hash_key>'.
        """
        ast_dir = Path(self.settings.asterisk_sounds_dir)
        try:
            ast_dir.mkdir(parents=True, exist_ok=True)
            link = ast_dir / f"{hash_key}.wav"
            if link.is_symlink():
                link.unlink()
            link.symlink_to(source.resolve())
        except PermissionError:
            # Running without root – Asterisk must share the same sounds dir
            log.warning(
                "Cannot symlink to %s (permission denied). "
                "Ensure audio_cache_dir == asterisk_sounds_dir or mount is shared.",
                ast_dir,
            )
        except Exception as exc:
            log.warning("Asterisk symlink error: %s", exc)

    def asterisk_sound_ref(self, hash_key: str) -> str:
        """Return the Asterisk Playback() reference (no .wav extension)."""
        base = Path(self.settings.asterisk_sounds_dir).name
        return f"{base}/{hash_key}"

    def evict_expired(self) -> int:
        """Remove WAV files whose Redis TTL has expired.  Returns # removed."""
        removed = 0
        r = get_redis(self.settings)
        for wav in self.cache_dir.glob("*.wav"):
            h = wav.stem
            redis_key = _key("audio", self.settings, h)
            try:
                if not r.exists(redis_key):
                    wav.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
        return removed


# ─────────────────────────────────────────────────────────────────────────────
# Rate-limiting cache
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _check_and_increment(self, phone: str, window: str, limit: int, ttl: int) -> bool:
        """Rate limiting has been removed; always allow."""
        return True

    def is_allowed(self, phone: str) -> tuple[bool, str]:
        """Rate limiting has been removed; always allow."""
        return True, "ok"

    def get_counts(self, phone: str) -> dict:
        """Rate limiting has been removed; return empty counters."""
        return {}
