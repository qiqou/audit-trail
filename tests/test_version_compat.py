"""T12 版本兼容检查 + 回滚策略用例。

覆盖（对应 TASKS.md T12 验收）：
- v1.1 项目（无 schema_version）打开 → 兼容并写入当前版本号
- schema 版本 > 当前 → 拒绝打开且提示"升级"（400）
- schema 版本 = 当前 → 正常打开
- 通过 API 打开不兼容项目 → 400 + 可执行提示
"""

import sqlite3

import pytest
from database import SCHEMA_VERSION, AuditProject
from fastapi.testclient import TestClient

from main import app


def _set_schema_version(root, ver):
    """直接往项目的 audit.db 写入 schema_version（模拟新旧库）。"""
    conn = sqlite3.connect(str(root / "audit.db"))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(ver),))
    conn.commit()
    conn.close()


def _login(client):
    r = client.post("/api/session", json={"operator": "测试员"})
    return {"X-Session": r.json()["token"]}


def test_legacy_project_no_version_opens(proj):
    """v1.1 项目（meta 无 schema_version）：兼容打开并写入当前版本。"""
    # 手动删掉版本键（模拟旧库）
    conn = sqlite3.connect(str(proj.db_path))
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.commit()
    conn.close()

    p2 = AuditProject(proj.root)  # 重新打开
    assert p2.get_meta("schema_version", "") == str(SCHEMA_VERSION)
    p2.close()


def test_current_version_opens(proj):
    """当前版本：正常打开。"""
    _set_schema_version(proj.root, SCHEMA_VERSION)
    p2 = AuditProject(proj.root)
    assert p2.root == proj.root
    p2.close()


def test_newer_version_rejected(proj):
    """更新版本创建的项目：拒绝打开 + 提示升级。"""
    _set_schema_version(proj.root, SCHEMA_VERSION + 1)
    with pytest.raises(ValueError, match="升级审迹"):
        AuditProject(proj.root)


def test_api_open_newer_version_400(proj):
    """API 打开不兼容项目 → 400 + 可执行提示（教用户怎么做）。"""
    _set_schema_version(proj.root, SCHEMA_VERSION + 1)
    with TestClient(app) as client:
        h = _login(client)
        r = client.post("/api/project/open", json={"path": str(proj.root)}, headers=h)
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "升级" in detail
        assert "备份" in detail


def test_api_open_legacy_ok(proj):
    """API 打开旧库（无版本号）→ 200 且写入版本号。"""
    conn = sqlite3.connect(str(proj.db_path))
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with TestClient(app) as client:
        h = _login(client)
        r = client.post("/api/project/open", json={"path": str(proj.root)}, headers=h)
        assert r.status_code == 200
        conn2 = sqlite3.connect(str(proj.db_path))
        ver = conn2.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        conn2.close()
        assert ver == str(SCHEMA_VERSION)


def test_docs_exist():
    """T12 文档齐备：用户说明 + 回滚策略。"""
    root = pytest.importorskip("pathlib").Path(__file__).resolve().parent.parent
    assert (root / "用户说明.md").exists()
    assert (root / "回滚策略_T12.md").exists()
    guide = (root / "用户说明.md").read_text(encoding="utf-8")
    for keyword in ("安装", "升级", "备份", "回滚", "常见问题"):
        assert keyword in guide
    rollback = (root / "回滚策略_T12.md").read_text(encoding="utf-8")
    for keyword in ("备份", "版本兼容", "回退", "旧版"):
        assert keyword in rollback
