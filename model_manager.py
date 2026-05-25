#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from utils import lm_studio_chat_request_options

log = logging.getLogger("proya.model_manager")


def load_model(model_id: str, cfg=None, timeout: float = 120.0) -> bool:
    """Ask LM Studio to load a model. Errors are logged and reported as False."""
    if not _management_enabled(cfg):
        log.info("LM Studio model management disabled; skipping load for %s", model_id)
        return False
    if is_model_loaded(model_id, cfg):
        log.info("LM Studio model already loaded: %s", model_id)
        return True
    ok = _post_model_action("load", model_id, cfg, timeout=timeout)
    if ok:
        log.info("LM Studio load requested: %s", model_id)
    return ok


def unload_model(model_id: str, cfg=None, timeout: float = 120.0) -> bool:
    """Ask LM Studio to unload a model. Errors are logged and reported as False."""
    if not _management_enabled(cfg):
        log.info("LM Studio model management disabled; skipping unload for %s", model_id)
        return False
    if not is_model_loaded(model_id, cfg):
        log.info("LM Studio model already unloaded: %s", model_id)
        return True
    ok = _post_model_action("unload", model_id, cfg, timeout=timeout)
    if ok:
        log.info("LM Studio unload requested: %s", model_id)
    return ok


def get_loaded_models(cfg=None) -> list[dict[str, Any]]:
    """Return models currently reported as loaded by LM Studio."""
    if not _management_enabled(cfg):
        log.info("LM Studio model management disabled; skipping loaded-model check")
        return []
    try:
        payload = _request_json("GET", f"{_api_root(cfg)}/api/v0/models", cfg=cfg, timeout=10.0)
    except Exception as exc:
        log.warning("Could not query LM Studio loaded models: %s", exc)
        return []

    models = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        return []
    loaded = []
    for model in models:
        if not isinstance(model, dict):
            continue
        state = str(model.get("state") or model.get("status") or "").casefold()
        if state in {"loaded", "loading"} or model.get("loaded") is True:
            loaded.append(model)
    return loaded


def wait_until_ready(model_id: str, timeout: float = 120.0, cfg=None) -> bool:
    """Poll until the model is loaded and can answer a tiny chat request."""
    if not _management_enabled(cfg):
        log.info("LM Studio model management disabled; skipping readiness wait for %s", model_id)
        return False

    deadline = time.time() + max(1.0, float(timeout or 120.0))
    last_error = ""
    while time.time() < deadline:
        if not is_model_loaded(model_id, cfg):
            time.sleep(2.0)
            continue
        try:
            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            payload.update(lm_studio_chat_request_options(cfg, model_id=model_id))
            _request_json(
                "POST",
                f"{_openai_base_url_for_model(model_id, cfg).rstrip('/')}/chat/completions",
                cfg=cfg,
                payload=payload,
                timeout=15.0,
            )
            log.info("LM Studio model ready: %s", model_id)
            return True
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2.0)

    log.warning("Timed out waiting for LM Studio model %s to become ready: %s", model_id, last_error)
    return False


def is_model_loaded(model_id: str, cfg=None) -> bool:
    return any(_model_id_matches(model_id, model) for model in get_loaded_models(cfg))


def loaded_model_ids(cfg=None) -> list[str]:
    ids = []
    for model in get_loaded_models(cfg):
        model_id = str(model.get("id") or model.get("model") or "").strip()
        if model_id:
            ids.append(model_id)
    return ids


def _post_model_action(action: str, model_id: str, cfg, timeout: float) -> bool:
    root = _api_root(cfg)
    if action == "unload":
        requests = [
            (f"{root}/api/v0/models/{action}", {"model": model_id}),
            (f"{root}/api/v1/models/{action}", {"instance_id": _loaded_instance_id(model_id, cfg) or model_id}),
        ]
    else:
        requests = [
            (f"{root}/api/v1/models/{action}", {"model": model_id, "echo_load_config": True}),
            (f"{root}/api/v0/models/{action}", {"model": model_id}),
        ]
    last_error = ""
    success = False
    for index, (url, payload) in enumerate(requests):
        try:
            _request_json("POST", url, cfg=cfg, payload=payload, timeout=timeout)
            if action == "load":
                return True
            success = True
        except HTTPError as exc:
            body = _read_http_error(exc)
            last_error = f"{exc.code} {body}".strip()
            if exc.code in {404, 405} and index == 0:
                continue
            if exc.code in {400, 409} and "already" in body.casefold():
                return True
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = str(exc)
            break
    if success:
        return True
    log.warning("LM Studio %s failed for %s: %s", action, model_id, last_error)
    return False


def _loaded_instance_id(model_id: str, cfg) -> str:
    for model in get_loaded_models(cfg):
        if _model_id_matches(model_id, model):
            return str(model.get("instance_id") or model.get("id") or model.get("model") or "").strip()
    return str(model_id or "").strip()


def _request_json(
    method: str,
    url: str,
    cfg=None,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    data = None
    headers = {
        "Authorization": f"Bearer {_api_key(cfg)}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method.upper(), headers=headers)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        return json.loads(raw)


def _management_enabled(cfg) -> bool:
    if cfg is None:
        try:
            import config as cfg  # type: ignore
        except Exception:
            return False
    return bool(getattr(cfg, "LM_STUDIO_MODEL_MANAGEMENT_ENABLED", True))


def _api_root(cfg) -> str:
    base_url = str(getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1")).rstrip("/")
    for suffix in ("/v1", "/v0"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)]
    return base_url


def _openai_base_url_for_model(model_id: str, cfg) -> str:
    vision_id = str(getattr(cfg, "SCORER_VISION_MODEL_ID", getattr(cfg, "SCORER_VISION_MODEL", "")))
    if _text_id_matches(str(model_id), vision_id):
        return str(getattr(cfg, "SCORER_VISION_BASE_URL", getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1")))
    return str(getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1"))


def _api_key(cfg) -> str:
    return str(getattr(cfg, "LM_STUDIO_API_KEY", "lm-studio"))


def _model_id_matches(target: str, model: dict[str, Any]) -> bool:
    values = [
        model.get("id"),
        model.get("model"),
        model.get("path"),
        model.get("instance_id"),
    ]
    return any(_text_id_matches(target, str(value or "")) for value in values)


def _text_id_matches(target: str, candidate: str) -> bool:
    left = _normalize_model_id(target)
    right = _normalize_model_id(candidate)
    if not left or not right:
        return False
    return left == right or left.split("/")[-1] == right.split("/")[-1]


def _normalize_model_id(value: str) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r":\d+$", "", text)


def _read_http_error(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
