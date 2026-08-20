"""可由 HTTP 层稳定映射的业务错误。"""


class ConflictError(ValueError):
    """用户操作基线已失效，必须重新读取后由用户决定如何处理。"""
