"""OpenAPI 合同门禁：防止接口或生成类型发生未审查漂移。"""

import json
from pathlib import Path

from main import app

ROOT_DIR = Path(__file__).resolve().parent.parent


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


def test_openapi_matches_committed_contract_snapshot():
    app.openapi_schema = None
    expected = json.loads((ROOT_DIR / "contracts/v1.3/openapi.json").read_text(encoding="utf-8"))

    assert app.openapi() == expected
