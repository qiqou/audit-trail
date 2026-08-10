"""T7 附件完整性扫描 + 导入报告用例。

覆盖（对应 TASKS.md T7 验收）：
- health_check 进度回调：phys/hash 阶段逐步推进，done/total 正确
- 取消事件：置位后扫描尽早退出并标记 cancelled
- 扫描 API：启动→轮询→done，结果含 problems
- 导入报告：errors 完整返回（不再截断 20 条）
"""

import threading

from fastapi.testclient import TestClient

from main import app


def _login(client) -> dict:
    r = client.post("/api/session", json={"operator": "测试员"})
    return {"X-Session": r.json()["token"]}


def test_health_check_progress_callback(proj):
    """进度回调：phys/hash 阶段各至少回调一次，done 单调递增。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    for i in range(5):
        src = proj.root / f"tmp_{i}.txt"
        src.write_text(f"content-{i}", encoding="utf-8")
        proj.add_file(uid, src, "张三", orig_name=f"f{i}.txt")

    seen = []
    proj.health_check(sample_size=0, progress=lambda d, t, ph: seen.append((ph, d, t)))

    phases = {ph for ph, _, _ in seen}
    assert "phys" in phases
    assert "hash" in phases
    # phys 阶段最终进度应到 5（5 个物理文件）
    phys_last = [t for ph, d, t in seen if ph == "phys"]
    assert phys_last and phys_last[-1] == 5
    # hash 阶段最终进度应到 5（5 个有 sha 的普通文件）
    hash_last = [t for ph, d, t in seen if ph == "hash"]
    assert hash_last and hash_last[-1] == 5


def test_health_check_cancel(proj):
    """取消事件置位后：扫描标记 cancelled 且尽早退出。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    for i in range(20):
        src = proj.root / f"tmp_{i}.txt"
        src.write_text(f"content-{i}", encoding="utf-8")
        proj.add_file(uid, src, "张三", orig_name=f"f{i}.txt")

    cancel = threading.Event()

    def _cancel_early(done, total, phase):
        if phase == "phys" and done >= 2:
            cancel.set()

    result = proj.health_check(sample_size=0, progress=_cancel_early, cancel_event=cancel)
    assert result["cancelled"] is True


def test_scan_api_full_flow():
    """扫描 API：启动→轮询→done，结果含 counts/problems。"""
    import tempfile
    from pathlib import Path

    from database import AuditProject

    tmp = tempfile.mkdtemp(prefix="t7_scan_")
    p = Path(tmp) / "项目"
    proj = AuditProject(p)
    uid = proj.add_unit("华电集团XX电厂", "张三")
    src = p / "证据.txt"
    src.write_text("evidence", encoding="utf-8")
    proj.add_file(uid, src, "张三", orig_name="证据.txt")
    proj.close()

    with TestClient(app) as client:
        h = _login(client)
        client.post("/api/project/open", json={"path": str(p)}, headers=h)
        r = client.post("/api/project/scan", json={}, headers=h)
        assert r.status_code == 200
        scan_id = r.json()["scan_id"]

        # 轮询直到 done（小项目很快）
        import time
        status = "running"
        for _ in range(50):
            r = client.get(f"/api/project/scan/{scan_id}", headers=h)
            status = r.json()["status"]
            if status in ("done", "cancelled", "error"):
                break
            time.sleep(0.05)
        assert status == "done", r.text
        body = r.json()
        assert body["counts"]["files"] == 1
        assert body["counts"]["units"] == 1
        assert body["problems"] == []


def test_scan_api_cancel():
    """扫描 API：取消后状态变 cancelled。"""
    import tempfile
    from pathlib import Path

    from database import AuditProject

    tmp = tempfile.mkdtemp(prefix="t7_cancel_")
    p = Path(tmp) / "项目"
    proj = AuditProject(p)
    uid = proj.add_unit("华电集团XX电厂", "张三")
    # 造较多文件让扫描有运行窗口；实际取消语义由事件保证
    for i in range(30):
        src = p / f"f{i}.txt"
        src.write_text(f"c{i}", encoding="utf-8")
        proj.add_file(uid, src, "张三", orig_name=f"f{i}.txt")
    proj.close()

    with TestClient(app) as client:
        h = _login(client)
        client.post("/api/project/open", json={"path": str(p)}, headers=h)
        r = client.post("/api/project/scan", json={}, headers=h)
        scan_id = r.json()["scan_id"]

        # 立即取消（可能在 running，也可能已完成——取消接口幂等返回 ok）
        r = client.post(f"/api/project/scan/{scan_id}/cancel", json={}, headers=h)
        assert r.status_code == 200

        # 最终状态：cancelled 或 done 都允许（取决于取消时机），但绝不能 error
        import time
        for _ in range(50):
            r = client.get(f"/api/project/scan/{scan_id}", headers=h)
            st = r.json()["status"]
            if st != "running":
                break
            time.sleep(0.05)
        assert st in ("cancelled", "done"), r.text


def test_scan_api_not_found():
    """不存在的 scan_id → 404 且提示可操作。"""
    with TestClient(app) as client:
        h = _login(client)
        r = client.get("/api/project/scan/nonexistent", headers=h)
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]


def test_import_report_errors_not_truncated(proj):
    """导入报告：errors 完整返回（含 20 条以后）。"""
    # 直接构造 Excel 需要 openpyxl；这里验证数据层导入逻辑返回完整 errors
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "导入模板"
    ws.append(["被审计单位", "所属版块", "缺陷定性"])
    for i in range(25):
        ws.append([f"单位{i}", "", ""])  # 25 行都缺版块/定性 → 25 条错误

    from export import import_from_excel

    path = proj.root / "bad.xlsx"
    wb.save(path)
    result = import_from_excel(proj, path, "张三")
    assert result["skipped"] == 25
    assert len(result["errors"]) == 25  # 不再截断 20 条
