from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def lm_studio_chat_request_options(
    cfg: Any,
    model_id: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Return LM Studio chat request fields that should go on the JSON body."""
    options: dict[str, Any] = {}
    if temperature is None:
        temperature = getattr(cfg, "LM_STUDIO_TEMPERATURE", None)
    if temperature is not None:
        options["temperature"] = float(temperature)

    if _qwen_thinking_should_be_disabled(cfg, model_id):
        options["chat_template_kwargs"] = {
            "enable_thinking": False,
            "preserve_thinking": False,
        }
    return options


def lm_studio_openai_chat_kwargs(
    cfg: Any,
    model_id: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Return kwargs for OpenAI-compatible LM Studio chat completions."""
    options = lm_studio_chat_request_options(cfg, model_id=model_id, temperature=temperature)
    kwargs: dict[str, Any] = {}
    if "temperature" in options:
        kwargs["temperature"] = options.pop("temperature")
    if options:
        kwargs["extra_body"] = options
    return kwargs


def _qwen_thinking_should_be_disabled(cfg: Any, model_id: str | None) -> bool:
    if bool(getattr(cfg, "LM_STUDIO_QWEN_THINKING_ENABLED", True)):
        return False
    model_text = str(model_id or getattr(cfg, "LM_STUDIO_MODEL", "") or "").casefold()
    return "qwen3" in model_text or "qwen3.6" in model_text


def _parse_json_object(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?", "", raw or "", flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        payload = json.loads(match.group(0))
        if isinstance(payload, dict):
            return payload
    raise ValueError(f"Qwen response was not JSON: {cleaned[:200]}")


def _path_identity(path: str | Path) -> dict:
    candidate = Path(path)
    try:
        resolved = candidate.resolve()
        stat = resolved.stat()
        return {
            "path": str(resolved).casefold(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        return {
            "path": str(candidate).casefold(),
            "size": None,
            "mtime_ns": None,
        }


def _format_rupiah_compact(amount_text: str) -> str:
    digits = re.sub(r"\D", "", str(amount_text))
    if not digits:
        return ""

    amount = int(digits)
    if amount >= 1000:
        if amount % 1000 == 0:
            return f"{amount // 1000}rb"
        compact = f"{amount / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}rb"
    return str(amount)
