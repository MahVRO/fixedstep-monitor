from __future__ import annotations

import re
import math
from dataclasses import dataclass
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:login|server|password|passwd|credential|token|secret|api_?key|"
    r"account_?(?:id|number)|machine|hostname|computer_?name|terminal_?path|"
    r"file_?path|directory|private_?key|ticket|deal_?id|order_?id|position_?id|magic|comment)(?:$|_)",
    re.IGNORECASE,
)
_WINDOWS_PATH = re.compile(r"(?:^|[\s'\"])(?:[A-Za-z]:[\\/]|\\\\)")
_POSIX_PRIVATE_PATH = re.compile(r"(?:^|[\s'\"])/(?:home|Users|var|etc|opt|srv|tmp)/")
_IP_ENDPOINT = re.compile(r"(?:^|\s)(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:$|\s)")
_SECRET_VALUE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"\bgh[opusr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,})"
)


@dataclass(frozen=True)
class Sanitized:
    value: Any
    omissions: tuple[dict[str, str], ...]


def _unsafe_string(value: str) -> str | None:
    stripped = value.strip()
    if _WINDOWS_PATH.search(stripped) or _POSIX_PRIVATE_PATH.search(stripped):
        return "machine path excluded"
    if _IP_ENDPOINT.search(stripped):
        return "private endpoint excluded"
    if _SECRET_VALUE.search(stripped):
        return "credential-like value excluded"
    return None


def sanitize_public_data(value: Any) -> Sanitized:
    """Recursively omit fields unsafe for a public artifact.

    Sanitization is intentionally lossy: unsafe values are omitted instead of redacted,
    so a redaction marker can never be mistaken for observed trading data.
    """

    omissions: list[dict[str, str]] = []
    missing = object()

    def visit(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                child_path = f"{path}.{key}" if path else key
                if _SENSITIVE_KEY.search(key):
                    omissions.append({"field": child_path, "reason": "sensitive field excluded"})
                    continue
                cleaned = visit(child, child_path)
                if cleaned is not missing:
                    result[key] = cleaned
            return result
        if isinstance(item, (list, tuple)):
            result_list: list[Any] = []
            for index, child in enumerate(item):
                cleaned = visit(child, f"{path}[{index}]")
                if cleaned is not missing:
                    result_list.append(cleaned)
            return result_list
        if isinstance(item, str):
            reason = _unsafe_string(item)
            if reason:
                omissions.append({"field": path or "$", "reason": reason})
                return missing
        if isinstance(item, float) and not math.isfinite(item):
            omissions.append({"field": path or "$", "reason": "non-finite numeric value excluded"})
            return missing
        return item

    cleaned = visit(value, "")
    if cleaned is missing:
        cleaned = None
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for omission in omissions:
        marker = (omission["field"], omission["reason"])
        if marker not in seen:
            seen.add(marker)
            unique.append(omission)
    return Sanitized(cleaned, tuple(unique))
