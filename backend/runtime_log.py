"""本地运行日志与未预期 HTTP 错误边界。

日志仅用于本机故障定位：以 JSON Lines 追加、滚动保留，并向界面返回不包含
路径、堆栈或数据库细节的错误编号。业务校验失败仍由各接口用 4xx 明确提示。
"""

import json
import logging
import os
import re
import traceback
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

LOGGER_NAME = "shenji.runtime"
LOG_FILE_NAME = "runtime.jsonl"
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
_PATH_PATTERN = re.compile(r"(?:(?:[A-Za-z]:)?(?:/|\\\\|~[/\\\\]))[^\s'\"，。；：()（）\[\]{}]+")


def runtime_log_path() -> Path:
    """返回可由部署环境覆盖的本机运行日志路径。"""
    base = os.environ.get("SHENJI_RUNTIME_LOG_DIR", "").strip()
    directory = Path(base).expanduser() if base else Path.home() / ".shenji" / "logs"
    return directory / LOG_FILE_NAME


def _safe_text(value: object, limit: int = 1200) -> str:
    """日志保留诊断信息，但移除用户路径并限制单字段长度。"""
    text = _PATH_PATTERN.sub("[path]", str(value or "").replace("\x00", ""))
    return text[:limit]


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": record.levelname,
            "event": getattr(record, "event", "runtime"),
            "error_id": getattr(record, "error_id", ""),
            "method": getattr(record, "method", ""),
            "route": getattr(record, "route", ""),
            "error_type": getattr(record, "error_type", ""),
            "message": _safe_text(record.getMessage()),
        }
        detail = getattr(record, "detail", "")
        if detail:
            payload["detail"] = _safe_text(detail, limit=6000)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    path = runtime_log_path()
    for handler in list(logger.handlers):
        if not getattr(handler, "_shenji_runtime_handler", False):
            continue
        if getattr(handler, "_shenji_runtime_path", "") == str(path):
            return logger
        logger.removeHandler(handler)
        handler.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        handler._shenji_runtime_handler = True  # type: ignore[attr-defined]
        handler._shenji_runtime_path = str(path)  # type: ignore[attr-defined]
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if os.name != "nt":
            path.chmod(0o600)
        return logger
    except OSError:
        # 无法写运行日志不应阻断离线底稿工作；仍保留 Python 默认日志链路。
        logger.addHandler(logging.NullHandler())
    return logger


def log_runtime_event(level: str, event: str, *, message: object = "", **context: object) -> None:
    """写入一条脱敏结构化运行记录。"""
    logger = _logger()
    method = getattr(logger, str(level).lower(), logger.info)
    method(
        _safe_text(message),
        extra={
            "event": event,
            "error_id": _safe_text(context.get("error_id", ""), 80),
            "method": _safe_text(context.get("method", ""), 16),
            "route": _safe_text(context.get("route", ""), 240),
            "error_type": _safe_text(context.get("error_type", ""), 160),
            "detail": _safe_text(context.get("detail", ""), 6000),
        },
    )


def log_unhandled_exception(error: Exception, *, method: str = "", route: str = "") -> str:
    """记录未预期异常，返回可提供给支持人员的短错误编号。"""
    error_id = f"E{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
    detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log_runtime_event(
        "error", "unhandled_exception", message="未预期服务异常", error_id=error_id,
        method=method, route=route, error_type=type(error).__name__, detail=detail,
    )
    return error_id


def install_unhandled_error_handler(app) -> None:
    """为 FastAPI 安装统一 500 错误边界；重复调用安全。"""
    if getattr(app.state, "shenji_unhandled_error_handler", False):
        return

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, error: Exception):
        route = getattr(request.scope.get("route"), "path", request.url.path)
        error_id = log_unhandled_exception(error, method=request.method, route=route)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务发生未预期错误，请记录错误编号后重试或联系支持。", "error_id": error_id},
        )

    app.state.shenji_unhandled_error_handler = True
