"""将领域异常稳定映射为 HTTP 错误，避免路由自行猜测状态码。"""

from fastapi import HTTPException


def key_or_value_error(exc: KeyError | ValueError) -> HTTPException:
    """缺失实体为 404，业务校验错误为 400；保留原始中文提示。"""
    return HTTPException(status_code=404 if isinstance(exc, KeyError) else 400, detail=str(exc))
