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
    return settings.model_dump()


def load_settings_from_store(path: Path | str = CONFIG_STORE_PATH) -> Settings:
    data = load_persistent_config(path)
    if not data:
        return Settings()
    return Settings(**data)


def save_settings_to_store(settings: Settings, path: Path | str = CONFIG_STORE_PATH) -> Path:
    return save_persistent_config(build_settings_data(settings), path)