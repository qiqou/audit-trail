"""将领域异常稳定映射为 HTTP 错误，避免路由自行猜测状态码。"""

from domain.errors import ConflictError
from fastapi import HTTPException


def key_or_value_error(exc: KeyError | ValueError) -> HTTPException:
    """缺失实体 404、基线冲突 409、其他业务校验 400；保留中文提示。"""
    if isinstance(exc, KeyError):
        status_code = 404
    elif isinstance(exc, ConflictError):
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=str(exc))
