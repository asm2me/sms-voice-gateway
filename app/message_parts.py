from __future__ import annotations

import re
import unicodedata
from typing import Any

_STATIC_TEMPLATE_PARAMETER_RE = re.compile(r"(?:\{\%\s*(\d+)\s*\}|%(\d+))")


def _is_text_character(character: str) -> bool:
    if not character:
        return False
    category = unicodedata.category(character)
    return category[0] in {"L", "M"} or character == "_"


def _is_symbol_character(character: str) -> bool:
    if not character:
        return False
    category = unicodedata.category(character)
    return category[0] in {"P", "S"}


def split_inbound_message_parameters(inbound_message: str) -> list[str]:
    return str(inbound_message or "").split()


def split_static_message_template(template: str) -> list[dict[str, Any]]:
    raw_template = str(template or "")
    parts: list[dict[str, Any]] = []
    cursor = 0
    ordinal = 1

    while cursor < len(raw_template):
        match = _STATIC_TEMPLATE_PARAMETER_RE.match(raw_template, cursor)
        if match:
            token = match.group(0)
            parameter_token = match.group(1) or match.group(2) or ""
            parameter_index = int(parameter_token) if parameter_token.isdigit() else None
            parts.append(
                {
                    "ordinal": ordinal,
                    "kind": "parameter",
                    "value": token,
                    "display_value": f"%{parameter_index}" if parameter_index else token,
                    "spoken": False,
                    "parameter_index": parameter_index,
                }
            )
            ordinal += 1
            cursor = match.end()
            continue

        current_character = raw_template[cursor]

        if current_character.isdigit():
            start = cursor
            cursor += 1
            while cursor < len(raw_template) and raw_template[cursor].isdigit():
                cursor += 1

            value = raw_template[start:cursor]
            parts.append(
                {
                    "ordinal": ordinal,
                    "kind": "number",
                    "value": value,
                    "display_value": value,
                    "spoken": bool(value.strip()),
                    "parameter_index": None,
                }
            )
            ordinal += 1
            continue

        if _is_symbol_character(current_character):
            start = cursor
            cursor += 1
            while cursor < len(raw_template):
                next_character = raw_template[cursor]
                if next_character.isdigit():
                    break
                if _STATIC_TEMPLATE_PARAMETER_RE.match(raw_template, cursor):
                    break
                if _is_text_character(next_character):
                    break
                if not _is_symbol_character(next_character):
                    break
                cursor += 1

            value = raw_template[start:cursor]
            if value.strip():
                parts.append(
                    {
                        "ordinal": ordinal,
                        "kind": "symbol",
                        "value": value,
                        "display_value": value,
                        "spoken": True,
                        "parameter_index": None,
                    }
                )
                ordinal += 1
            continue

        start = cursor
        cursor += 1
        while cursor < len(raw_template):
            next_character = raw_template[cursor]
            if next_character.isdigit():
                break
            if _STATIC_TEMPLATE_PARAMETER_RE.match(raw_template, cursor):
                break
            if _is_symbol_character(next_character):
                break
            cursor += 1

        value = raw_template[start:cursor]
        if value.strip():
            parts.append(
                {
                    "ordinal": ordinal,
                    "kind": "text",
                    "value": value,
                    "display_value": value,
                    "spoken": True,
                    "parameter_index": None,
                }
            )
            ordinal += 1

    if not parts and raw_template.strip():
        parts.append(
            {
                "ordinal": 1,
                "kind": "text",
                "value": raw_template.strip(),
                "display_value": raw_template.strip(),
                "spoken": True,
                "parameter_index": None,
            }
        )

    return parts


def resolve_static_message_parts(template: str, inbound_message: str) -> list[dict[str, Any]]:
    inbound_parts = split_inbound_message_parameters(inbound_message)
    resolved_parts: list[dict[str, Any]] = []

    for part in split_static_message_template(template):
        parameter_index = part.get("parameter_index")
        resolved_value = part.get("value", "")
        if part.get("kind") == "parameter" and parameter_index:
            resolved_value = (
                inbound_parts[parameter_index - 1]
                if 0 < parameter_index <= len(inbound_parts)
                else ""
            )

        resolved_parts.append(
            {
                **part,
                "resolved_value": resolved_value,
                "spoken_value": resolved_value if part.get("spoken") else "",
                "missing": bool(part.get("kind") == "parameter" and not resolved_value),
            }
        )

    return resolved_parts


def render_static_default_message(template: str, inbound_message: str) -> str:
    resolved_parts = resolve_static_message_parts(template, inbound_message)
    rendered_message = "".join(str(part.get("resolved_value", "")) for part in resolved_parts).strip()
    return rendered_message or str(inbound_message or "").strip()


def extract_spoken_segments(template: str, inbound_message: str) -> list[str]:
    resolved_parts = resolve_static_message_parts(template, inbound_message)
    segments: list[str] = []
    for part in resolved_parts:
        spoken_value = str(part.get("spoken_value", ""))
        if spoken_value.strip():
            segments.append(spoken_value)
        elif part.get("kind") == "parameter":
            parameter_value = str(part.get("resolved_value", ""))
            if parameter_value.strip():
                segments.append(parameter_value)
    return segments


def describe_static_message_template(template: str) -> dict[str, Any]:
    parts = split_static_message_template(template)
    return {
        "template": str(template or ""),
        "parts": parts,
        "has_parameters": any(part.get("kind") == "parameter" for part in parts),
        "spoken_parts": [part for part in parts if part.get("spoken")],
        "parameter_parts": [part for part in parts if part.get("kind") == "parameter"],
    }
