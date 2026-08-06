"""Small, dependency-free primitives shared by all CLI gateway backends."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Union


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def env_list(name: str, default: str = "") -> List[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def message_hashes(messages: Iterable[Dict[str, Any]]) -> List[str]:
    return [fingerprint(message) for message in messages]


def load_json_object(path: Union[str, Path]) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_object(path: Union[str, Path], value: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def content_text(content: Any) -> str:
    """Normalize the common OpenAI text/image content shapes to prompt text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False)
    parts: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
        elif part.get("type") in ("text", "input_text"):
            parts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append(f"[图片输入：{url}]")
        else:
            parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(item for item in parts if item)
