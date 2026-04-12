from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_STORE_PATH = DATA_DIR / "config.json"

BOOTSTRAP_ONLY_FIELDS = {
    "host",
    "port",
    "debug",
    "admin_username",
    "admin_password",
}

ADMIN_MANAGED_FIELDS = set(Settings.model_fields) - BOOTSTRAP_ONLY_FIELDS


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_persistent_config(path: Path | str = CONFIG_STORE_PATH) -> dict[str, Any]:
    store_path = Path(path)
    if not store_path.exists():
        return {}

    try:
        with store_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        log.warning("Config store JSON is invalid at %s: %s", store_path, exc)
        return {}
    except OSError as exc:
        log.warning("Unable to read config store at %s: %s", store_path, exc)
        return {}

    if not isinstance(data, dict):
        log.warning("Config store at %s did not contain a JSON object", store_path)
        return {}

    return data


def save_persistent_config(
    values: dict[str, Any],
    path: Path | str = CONFIG_STORE_PATH,
) -> Path:
    store_path = Path(path)
    _ensure_parent_dir(store_path)

    with store_path.open("w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")

    return store_path


def build_settings_data(settings: Settings) -> dict[str, Any]:
    data = settings.model_dump()
    for field in BOOTSTRAP_ONLY_FIELDS:
        data.pop(field, None)
    return data


def load_settings_from_store(path: Path | str = CONFIG_STORE_PATH) -> Settings:
    env_settings = Settings()
    data = load_persistent_config(path)
    if not data:
        return env_settings

    merged = env_settings.model_dump()
    for key, value in data.items():
        if key in BOOTSTRAP_ONLY_FIELDS:
            continue
        merged[key] = value

    return Settings(**merged)


def save_settings_to_store(settings: Settings, path: Path | str = CONFIG_STORE_PATH) -> Path:
    return save_persistent_config(build_settings_data(settings), path)
