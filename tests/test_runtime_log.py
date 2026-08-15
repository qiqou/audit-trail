"""F-20：未预期异常必须可定位，但 API 和日志不能暴露用户本机路径。"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from runtime_log import install_unhandled_error_handler, log_unhandled_exception, runtime_log_path


def test_f20_unhandled_error_returns_safe_id_and_writes_redacted_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("SHENJI_RUNTIME_LOG_DIR", str(tmp_path / "runtime"))
    app = FastAPI()
    install_unhandled_error_handler(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError(f"cannot read {tmp_path / '敏感项目' / 'audit.db'}")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    payload = response.json()
    assert payload["error_id"].startswith("E")
    assert payload["error_id"] in payload["detail"] or "错误编号" in payload["detail"]
    assert str(tmp_path) not in payload["detail"]

    # 同一进程中主应用可能已初始化过 logger；直接调用仍应给出可供人工定位的编号。
    logged_id = log_unhandled_exception(RuntimeError(f"cannot read {tmp_path / '敏感项目'}"), route="/test")
    assert logged_id.startswith("E")
    path = runtime_log_path()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    record = next(item for item in records if item["error_id"] == logged_id)
    assert record["event"] == "unhandled_exception"
    assert "[path]" in record["detail"]
    assert str(tmp_path) not in record["detail"]
