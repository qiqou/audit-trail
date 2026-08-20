"""运行能力契约：前端按明确能力开关展示，不依赖报错文字。"""

from database import SCHEMA_VERSION
from fastapi.testclient import TestClient

from main import app


def test_runtime_meta_exposes_supported_v13_capabilities():
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        response = client.get("/api/meta", headers={"X-Session": token})
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": SCHEMA_VERSION,
        "capabilities": {
            "draft_recovery": True,
            "review_notes": True,
            "unit_ordering": True,
            "issue_ordering": True,
            "rich_text_editor": False,
            "project_material_requests": False,
        },
    }
