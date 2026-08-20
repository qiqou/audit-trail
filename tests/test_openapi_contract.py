"""OpenAPI 合同门禁：防止前端生成类型时静默覆盖重复接口。"""

from main import app


def test_openapi_operation_ids_are_unique():
    """同一 operationId 不能被多个 HTTP 方法复用。"""
    app.openapi_schema = None
    schema = app.openapi()
    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]

    assert len(operation_ids) == len(set(operation_ids))
