"""API 层回归用例（审查 F-03：会话隔离）。

用 TestClient 起真实 FastAPI 应用，验证：
- 使用人会话隔离：两个 token 可各自打开不同项目，互不干扰
- 部分更新（F-02）在 API 层同样生效
- 跨单位引用删除保护（F-01）在 API 层返回 400
"""

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import main
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, name):
    r = client.post("/api/session", json={"operator": name})
    assert r.status_code == 200
    return r.json()["token"]


def _headers(token):
    return {"X-Session": token}


def test_f03_sessions_isolated_projects(client, tmp_path):
    """两个会话各自打开不同项目，单位/底稿互不串扰（审查 F-03 修复）。"""
    t1 = _login(client, "张三")
    t2 = _login(client, "李四")

    p1 = tmp_path / "项目A"
    p2 = tmp_path / "项目B"

    # 会话1 创建/打开项目A，会话2 创建/打开项目B
    r = client.post("/api/project/create", json={"path": str(p1), "name": "项目A"}, headers=_headers(t1))
    assert r.status_code == 200
    r = client.post("/api/project/create", json={"path": str(p2), "name": "项目B"}, headers=_headers(t2))
    assert r.status_code == 200

    # 会话1 在项目A 建单位
    r = client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=_headers(t1))
    assert r.status_code == 200

    # 会话2 的项目B 不应看到项目A 的单位
    units2 = client.get("/api/units", headers=_headers(t2))
    assert units2.status_code == 200
    assert units2.json() == [], "会话2 不应看到会话1 项目的数据"

    # 会话1 的项目A 仍有单位
    units1 = client.get("/api/units", headers=_headers(t1))
    assert units1.json()[0]["name"] == "华电集团XX电厂"


def test_session_validation_and_empty_project_path(client):
    """前端可校验服务重启后的会话；空路径不能误打开源码工作目录。"""
    token = _login(client, "张三")
    headers = _headers(token)
    current = client.get("/api/session", headers=headers)
    assert current.status_code == 200
    assert current.json() == {"operator": "张三"}
    assert client.post("/api/project/open", json={"path": "  "}, headers=headers).status_code == 400
    assert client.post("/api/project/create", json={"path": "", "name": "错误项目"}, headers=headers).status_code == 400


def test_logout_releases_session_and_project(client, tmp_path):
    """切换使用人时服务端同步注销会话，旧 token 不可继续访问。"""
    token = _login(client, "张三")
    headers = _headers(token)
    assert client.post("/api/project/create", json={"path": str(tmp_path / "项目A")}, headers=headers).status_code == 200
    project = main._sessions[token].project
    assert project is not None

    response = client.delete("/api/session", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert token not in main._sessions
    assert client.get("/api/session", headers=headers).status_code == 400


def test_api_new_issue_cannot_bypass_status_machine(client, tmp_path):
    token = _login(client, "张三")
    headers = _headers(token)
    client.post("/api/project/create", json={"path": str(tmp_path / "项目A")}, headers=headers)
    client.post("/api/units", json={"name": "单位A"}, headers=headers)
    created = client.post("/api/units/1/issues", json={"status": "已归档"}, headers=headers)
    assert created.status_code == 200
    issue = client.get(f"/api/issues/{created.json()['id']}", headers=headers).json()
    assert issue["status"] == "草稿"


def test_f02_api_partial_update_keeps_fields(client, tmp_path):
    """API 层 PATCH 只提交部分字段时，其余字段保持原值。"""
    t = _login(client, "张三")
    p = tmp_path / "项目A"
    client.post("/api/project/create", json={"path": str(p), "name": "项目A"}, headers=_headers(t))
    client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=_headers(t))
    r = client.post("/api/units/1/issues", json={
        "department": "营销管理", "defect_type": "电费回收不及时",
        "defect_desc": "原始描述", "amount": "100万", "author": "张三",
    }, headers=_headers(t))
    iid = r.json()["id"]

    r = client.patch(f"/api/issues/{iid}", json={"amount": "120万"}, headers=_headers(t))
    assert r.status_code == 200
    got = client.get(f"/api/issues/{iid}", headers=_headers(t)).json()
    assert got["amount"] == "120万"
    assert got["department"] == "营销管理"
    assert got["defect_type"] == "电费回收不及时"
    assert got["defect_desc"] == "原始描述"
    assert got["author"] == "张三"


def test_problem_category_presets_and_issue_field(client, tmp_path):
    """问题分类预设随项目保存，且作为可选底稿字段可单独更新。"""
    token = _login(client, "张三")
    headers = _headers(token)
    client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers)
    client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=headers)

    saved = client.post("/api/settings/categories", json={"categories": ["经营管理", "合规管理", "经营管理", " "]}, headers=headers)
    assert saved.status_code == 200
    assert saved.json() == ["经营管理", "合规管理"]
    assert client.get("/api/settings/categories", headers=headers).json() == ["经营管理", "合规管理"]

    saved_with_put = client.put("/api/settings/categories", json={"categories": ["合规管理", "内控管理"]}, headers=headers)
    assert saved_with_put.status_code == 200
    assert saved_with_put.json() == ["合规管理", "内控管理"]

    created = client.post("/api/units/1/issues", json={"department": "营销管理", "defect_type": "问题A", "category": "经营管理"}, headers=headers)
    issue_id = created.json()["id"]
    updated = client.patch(f"/api/issues/{issue_id}", json={"category": "合规管理"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["issue"]["category"] == "合规管理"
    assert updated.json()["issue"]["department"] == "营销管理"


def test_f01_api_delete_unit_blocked(client, tmp_path):
    """API 层删除被跨单位引用的单位返回 400，附件不丢。"""
    t = _login(client, "张三")
    p = tmp_path / "项目A"
    client.post("/api/project/create", json={"path": str(p), "name": "项目A"}, headers=_headers(t))
    client.post("/api/units", json={"name": "单位A"}, headers=_headers(t))
    client.post("/api/units", json={"name": "单位B"}, headers=_headers(t))
    r = client.post("/api/units/2/issues", json={
        "department": "营销管理", "defect_type": "问题B",
    }, headers=_headers(t))
    iid_b = r.json()["id"]
    # 上传附件到单位A
    r = client.post("/api/units/1/files",
                    files={"file": ("证据.pdf", b"%PDF shared", "application/pdf")},
                    headers=_headers(t))
    fid = r.json()["id"]
    # 单位B 底稿关联单位A 附件（跨单位）
    client.post(f"/api/issues/{iid_b}/files/{fid}/link", headers=_headers(t))

    r = client.delete("/api/units/1", headers=_headers(t))
    assert r.status_code == 400, "跨单位引用时应阻止删除"
    assert "引用" in r.json()["detail"]

    # 附件仍在
    files = client.get(f"/api/issues/{iid_b}/files", headers=_headers(t))
    assert len(files.json()) == 1


def test_v3_issue_tree_is_grouped_and_requires_project(client, tmp_path):
    """V3 双视图读取聚合问题树，避免对每个单位单独请求。"""
    token = _login(client, "张三")
    headers = _headers(token)
    assert client.get("/api/issues/tree", headers=headers).status_code == 400

    client.post("/api/project/create", json={"path": str(tmp_path / "项目"), "name": "项目"}, headers=headers)
    client.post("/api/units", json={"name": "单位A"}, headers=headers)
    client.post("/api/units", json={"name": "单位B"}, headers=headers)
    client.post("/api/units/2/issues", json={"defect_type": "问题B"}, headers=headers)

    data = client.get("/api/issues/tree", headers=headers).json()
    assert set(data) == {"2"}
    assert data["2"][0]["defect_type"] == "问题B"


def test_f06_api_batch_rename_and_move(client, tmp_path):
    """API 层批量重命名（冲突跳过）与移动到单位。"""
    t = _login(client, "张三")
    p = tmp_path / "项目A"
    client.post("/api/project/create", json={"path": str(p), "name": "项目A"}, headers=_headers(t))
    client.post("/api/units", json={"name": "单位A"}, headers=_headers(t))
    client.post("/api/units", json={"name": "单位B"}, headers=_headers(t))
    r1 = client.post("/api/units/1/files", files={"file": ("a.pdf", b"aaa", "application/pdf")},
                     headers=_headers(t))
    r2 = client.post("/api/units/1/files", files={"file": ("b.pdf", b"bbb", "application/pdf")},
                     headers=_headers(t))
    fid1, fid2 = r1.json()["id"], r2.json()["id"]

    # 批量重命名：fid2 改名后与 fid1 相同 → 冲突跳过
    r = client.post("/api/files/batch-rename", json={
        "renames": [{"id": fid1, "name": "证据A.pdf"}, {"id": fid2, "name": "证据A.pdf"}],
    }, headers=_headers(t))
    assert r.status_code == 200
    body = r.json()
    assert body["renamed"] == 1
    assert len(body["conflicts"]) == 1

    # 移动到单位B
    r = client.post(f"/api/files/{fid1}/move", json={"unit_id": 2}, headers=_headers(t))
    assert r.status_code == 200
    assert r.json()["unit_id"] == 2


def test_open_unit_attachment_directory_uses_stable_unit_id(client, tmp_path, monkeypatch):
    """V3 打开附件库由服务端按 unit_id 解析，不依赖可变的单位显示名称。"""
    token = _login(client, "张三")
    headers = _headers(token)
    project_root = Path(client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers).json()["path"])
    client.post("/api/units", json={"name": "可重命名/单位"}, headers=headers)

    opened: list[Path] = []
    monkeypatch.setattr(main, "open_path", lambda path: opened.append(Path(path)))
    response = client.post("/api/units/1/attachments/open", headers=headers)

    assert response.status_code == 200
    assert opened == [project_root / "附件库" / "unit_1"]
    logs = client.get("/api/logs", headers=headers).json()
    assert logs[0]["action"] == "打开附件目录"


def test_attachment_directory_endpoints_accept_get_post_and_open_folder_entity(client, tmp_path, monkeypatch):
    """打开附件库和“查看目录”均使用服务端受控路径，且兼容旧界面的 GET。"""
    token = _login(client, "张三")
    headers = _headers(token)
    project_root = Path(client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers).json()["path"])
    unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]

    source = tmp_path / "证据原件.txt"
    source.write_text("evidence", encoding="utf-8")
    project = main._sessions[token].project
    assert project is not None
    folder = project.add_folder(unit_id, [("子目录/证据原件.txt", str(source))], "合同资料", "张三")

    opened: list[Path] = []
    monkeypatch.setattr(main, "open_path", lambda path: opened.append(Path(path)))
    assert client.post(f"/api/units/{unit_id}/attachments/open", headers=headers).status_code == 200
    assert client.get(f"/api/units/{unit_id}/attachments/open", headers=headers).status_code == 200
    assert client.post(f"/api/files/{folder['id']}/directory/open", headers=headers).status_code == 200

    assert opened[:2] == [project_root / "附件库" / f"unit_{unit_id}"] * 2
    assert opened[2] == project_root / folder["rel_path"]


def test_open_file_endpoint_opens_attachment_with_default_program(client, tmp_path, monkeypatch):
    """“打开”接口用系统默认程序打开附件真实路径，并写审计日志。"""
    token = _login(client, "张三")
    headers = _headers(token)
    client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers)
    unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]
    uploaded = client.post(
        f"/api/units/{unit_id}/files",
        files={"file": ("证据.pdf", b"%PDF shared", "application/pdf")},
        headers=headers,
    ).json()
    file_id = uploaded["id"]

    opened: list[Path] = []
    monkeypatch.setattr(main, "open_path", lambda path: opened.append(Path(path)))
    response = client.post(f"/api/files/{file_id}/open", headers=headers)

    assert response.status_code == 200
    proj = main._sessions[token].project
    rec = proj.get_file(file_id)
    assert opened == [proj.attachment_path(rec["rel_path"])]
    logs = client.get("/api/logs", headers=headers).json()
    assert logs[0]["action"] == "打开附件"

    missing = client.post("/api/files/999999/open", headers=headers)
    assert missing.status_code == 404


def test_recent_projects_api(client, tmp_path):
    """最近项目：打开/创建自动记录、重命名更新、按使用人隔离、可移除。"""
    token = _login(client, "张三")
    headers = _headers(token)

    # 初始为空
    assert client.get("/api/recent", headers=headers).json()["items"] == []

    # 创建项目A → 记录 1 条
    p1 = tmp_path / "项目A"
    client.post("/api/project/create", json={"path": str(p1), "name": "项目A"}, headers=headers)
    items = client.get("/api/recent", headers=headers).json()["items"]
    assert len(items) == 1 and items[0]["name"] == "项目A"

    # 打开项目B → 2 条，最新在前
    p2 = tmp_path / "项目B"
    client.post("/api/project/create", json={"path": str(p2), "name": "项目B"}, headers=headers)
    items = client.get("/api/recent", headers=headers).json()["items"]
    assert [it["name"] for it in items] == ["项目B", "项目A"]

    # 重命名 → 最近记录名称同步更新
    renamed = client.post("/api/project/rename", json={"name": "项目B改"}, headers=headers)
    assert renamed.status_code == 200
    items = client.get("/api/recent", headers=headers).json()["items"]
    assert items[0]["name"] == "项目B改"

    # 其他使用人看不到（按使用人隔离）
    token2 = _login(client, "李四")
    assert client.get("/api/recent", headers=_headers(token2)).json()["items"] == []

    # 移除单条记录
    removed = client.delete(f"/api/recent?path={quote(str(p2) + '.auditproj')}", headers=headers)
    assert removed.status_code == 200
    items = client.get("/api/recent", headers=headers).json()["items"]
    assert [it["name"] for it in items] == ["项目A"]

    # 删除项目 → 记录自动移除
    deleted = client.post("/api/project/delete",
                          json={"path": str(p1) + ".auditproj"}, headers=headers)
    assert deleted.status_code == 200
    assert client.get("/api/recent", headers=headers).json()["items"] == []


def test_folder_upload_dedup_reuses_existing(client, tmp_path):
    """文件夹上传：内容一致（目录结构+文件内容）时复用已有实体，不重复存储。"""
    token = _login(client, "张三")
    headers = _headers(token)
    client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers)
    unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]

    def upload_folder(payload: bytes):
        return client.post(
            f"/api/units/{unit_id}/folder-upload",
            data={"folder_name": "合同资料"},
            files=[("files", ("子目录/证据.txt", payload, "text/plain"))],
            headers=headers,
        )

    r1 = upload_folder(b"same content")
    assert r1.status_code == 200
    first = r1.json()
    assert first["mime"] == "folder" and first["sha256"] != ""

    # 相同内容 → duplicated 复用
    r2 = upload_folder(b"same content")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["duplicated"] is True and body2["file"]["id"] == first["id"]

    # 内容不同 → 新建实体
    r3 = upload_folder(b"different content")
    assert r3.status_code == 200
    assert "duplicated" not in r3.json()
    assert r3.json()["id"] != first["id"]

    # 目录结构不同（同内容不同路径）→ 不判重
    r4 = client.post(
        f"/api/units/{unit_id}/folder-upload",
        data={"folder_name": "合同资料"},
        files=[("files", ("其他目录/证据.txt", b"same content", "text/plain"))],
        headers=headers,
    )
    assert "duplicated" not in r4.json()


def test_global_search_finds_units_issues_files(client, tmp_path):
    """全局搜索：单位/底稿/附件按关键字模糊匹配。"""
    token = _login(client, "张三")
    headers = _headers(token)
    client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers)
    unit_id = client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=headers).json()["id"]
    client.post(f"/api/units/{unit_id}/issues", json={"department": "营销管理", "defect_type": "合同签订不规范", "amount": "12345"}, headers=headers)
    client.post(
        f"/api/units/{unit_id}/files",
        headers=headers,
        files={"file": ("付款凭证.pdf", b"%PDF-fake-content", "application/pdf")},
    )

    r = client.get("/api/search", params={"q": "电厂"}, headers=headers).json()
    assert [u["name"] for u in r["units"]] == ["华电集团XX电厂"]

    r = client.get("/api/search", params={"q": "合同"}, headers=headers).json()
    assert any(i["defect_type"] == "合同签订不规范" for i in r["issues"])

    r = client.get("/api/search", params={"q": "付款凭证"}, headers=headers).json()
    assert [f["orig_name"] for f in r["files"]] == ["付款凭证.pdf"]

    # 空关键字 → 三空
    r = client.get("/api/search", params={"q": "  "}, headers=headers).json()
    assert r == {"units": [], "issues": [], "files": []}


def test_summary_includes_issue_list(client, tmp_path):
    """项目汇总：返回问题明细列表（含单位名、金额、状态、附件数）。"""
    token = _login(client, "张三")
    headers = _headers(token)
    client.post("/api/project/create", json={"path": str(tmp_path / "项目A"), "name": "项目A"}, headers=headers)
    unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]
    client.post(f"/api/units/{unit_id}/issues", json={"department": "营销管理", "defect_type": "问题A", "amount": "100"}, headers=headers)
    client.post(f"/api/units/{unit_id}/issues", json={"department": "财务管理", "defect_type": "问题B"}, headers=headers)

    data = client.get("/api/project/summary", headers=headers).json()
    assert data["total"] == 2
    assert len(data["issues"]) == 2
    first = data["issues"][0]
    assert first["unit_name"] == "单位A" and first["defect_type"] == "问题A"
    assert first["amount"] == "100" and first["status"] == "草稿" and "file_count" in first


def test_system_quit_returns_ok(client, tmp_path, monkeypatch):
    """退出端点：返回 ok 且不抛错（_do_quit 由 Timer 延迟执行，测试拦下）。"""
    # 注意：测试用 from main import app（backend/main.py 以顶层名导入的副本），
    # monkeypatch 必须打 main 模块而非 backend.main（两者是两个实例，打错会触发真 os._exit）
    import main as main_module
    token = _login(client, "张三")
    headers = _headers(token)
    monkeypatch.setattr(main_module, "_do_quit", lambda: None)
    r = client.post("/api/system/quit", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    import time
    time.sleep(1.2)  # 等 Timer 触发（_do_quit 已被替换为空操作）


def test_backup_download_url_works(client, tmp_path):
    """备份后 download_url 可下载（备份在上级目录，不走输出目录端点）。"""
    t = _login(client, "张三")
    p = tmp_path / "项目A"
    client.post("/api/project/create", json={"path": str(p), "name": "项目A"}, headers=_headers(t))
    client.post("/api/units", json={"name": "单位A"}, headers=_headers(t))

    r = client.post("/api/backup/create", headers=_headers(t))
    assert r.status_code == 200
    body = r.json()
    assert "download_url" in body and "backup/download" in body["download_url"]
    assert (tmp_path / body["filename"]).exists(), "备份文件在项目上级目录"

    # 下载端点可拿到文件（与 create 返回文件名一致）
    r2 = client.get(body["download_url"], headers=_headers(t))
    assert r2.status_code == 200
    assert r2.content[:2] == b"PK", "auditbak 是 zip 内容"

    # 路径穿越防护：上级目录之外的文件拒绝
    evil = tmp_path / "evil.txt"
    evil.write_text("x")
    r3 = client.get("/api/backup/download/..%2F..%2Fevil.txt", headers=_headers(t))
    assert r3.status_code in (400, 404)
    r4 = client.get("/api/backup/download/不存在.auditbak", headers=_headers(t))
    assert r4.status_code == 404
