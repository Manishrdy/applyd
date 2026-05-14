from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import Request, Response

MAX_LOG_FILE_BYTES = 10 * 1024 * 1024  # 10MB
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_INLINE_BODY_CHARS = 20_000
_TEXT_BODY_TYPES = (
    "application/json",
    "application/xml",
    "application/x-www-form-urlencoded",
    "text/plain",
    "text/html",
)


def _dashboard_root() -> Path:
    return Path(__file__).resolve().parents[1]


def log_dir() -> Path:
    p = _dashboard_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def configure_logging(log_level: str = "INFO") -> Path:
    """Configure root + uvicorn loggers with rotating file and console output."""
    dest = log_dir() / "dashboard.log"
    root = logging.getLogger()
    root.setLevel(log_level.upper())

    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    seen_file = False
    seen_console = False

    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler):
            seen_file = True
            handler.setFormatter(fmt)
        elif isinstance(handler, logging.StreamHandler):
            seen_console = True
            handler.setFormatter(fmt)

    if not seen_console:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    if not seen_file:
        file_handler = RotatingFileHandler(
            filename=dest,
            maxBytes=MAX_LOG_FILE_BYTES,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logging.getLogger(name).setLevel(log_level.upper())

    logging.getLogger(__name__).info("centralized logging configured at %s", dest)
    return dest


def _headers_dict(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items()}


def _clip(value: str, limit: int = _MAX_INLINE_BODY_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [truncated {len(value) - limit} chars]"


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return repr(value)


def _is_text_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return any(ct in lowered for ct in _TEXT_BODY_TYPES)


async def request_payload(request: Request) -> str:
    content_type = request.headers.get("content-type")
    body = await request.body()
    if not body:
        return ""
    if _is_text_content_type(content_type):
        return _clip(body.decode("utf-8", errors="replace"))
    return f"<{len(body)} bytes; content-type={content_type or 'unknown'}>"


def response_payload(response: Response, body: bytes | None = None) -> str:
    content_type = response.headers.get("content-type")
    payload = body if body is not None else getattr(response, "body", b"")
    if not payload:
        return ""
    if _is_text_content_type(content_type):
        return _clip(payload.decode("utf-8", errors="replace"))
    return f"<{len(payload)} bytes; content-type={content_type or 'unknown'}>"


def log_http_request(log: logging.Logger, request: Request, payload: str) -> None:
    log.info(
        "request method=%s path=%s query=%s client=%s headers=%s payload=%s",
        request.method,
        request.url.path,
        request.url.query,
        request.client.host if request.client else "unknown",
        _safe_json_dumps(_headers_dict(request)),
        payload or "<empty>",
    )


def log_http_response(
    log: logging.Logger,
    request: Request,
    response: Response,
    elapsed_ms: float,
    payload: str,
) -> None:
    msg = (
        "response method=%s path=%s status=%s elapsed_ms=%.2f headers=%s payload=%s"
    )
    args = (
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        _safe_json_dumps(dict(response.headers.items())),
        payload or "<empty>",
    )
    if response.status_code >= 500:
        log.error(msg, *args)
    elif response.status_code >= 400:
        log.warning(msg, *args)
    else:
        log.info(msg, *args)
