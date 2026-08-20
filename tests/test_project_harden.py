"""目录伪装（防误删改）：新建项目自动加 .auditproj 后缀 + 隐藏属性。

- 后端 create_project 对目录名追加 PROJECT_EXT（已带不重复加）
- platform_adapter.harden_project 设置隐藏属性（macOS chflags / Windows attrib）
- 隐藏不影响程序按路径读写（回归：创建后能正常建单位）
"""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import PROJECT_EXT, app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, name="防误删测试员"):
    r = client.post("/api/session", json={"operator": name})
    assert r.status_code == 200
    return r.json()["token"]


def _h(token):
    return {"X-Session": token}


def _is_hidden(path: Path) -> bool:
    """检查目录隐藏标志（macOS ls -ldO 的 flags 列 / Windows attrib 输出）。"""
    if sys.platform == "darwin":
        r = subprocess.run(["ls", "-ldO", str(path)], capture_output=True, text=True, check=False)
        return "hidden" in r.stdout
    if sys.platform == "win32":
        r = subprocess.run(["attrib", str(path)], capture_output=True, text=True, check=False)
        return "H" in r.stdout
    return False


def test_create_project_appends_ext_and_hides(client, tmp_path):
    """新建项目：目录名自动加 .auditproj，且目录被隐藏。"""
    t = _login(client)
    raw = tmp_path / "2026专项审计"
    r = client.post("/api/project/create", json={"path": str(raw), "name": "2026专项审计"}, headers=_h(t))
    assert r.status_code == 200
    created = Path(r.json()["path"])
    assert created.name == "2026专项审计" + PROJECT_EXT, f"目录名应追加 {PROJECT_EXT}：{created}"
    assert created.is_dir()
    # 主防线：隐藏属性已设置（macOS/Windows 断言各自平台标志；Linux 无隐藏概念跳过）
    if sys.platform in ("darwin", "win32"):
        assert _is_hidden(created), f"项目目录应已隐藏：{created}"
    if sys.platform != "win32":
        assert created.stat().st_mode & 0o777 == 0o700
        assert (created / "audit.db").stat().st_mode & 0o777 == 0o600
    # 隐藏不影响程序读写：直接建一个单位验证会话内项目引用完好
    r2 = client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=_h(t))
    assert r2.status_code == 200


def test_create_project_does_not_double_ext(client, tmp_path):
    """传入已带 .auditproj 后缀的路径不重复追加。"""
    t = _login(client)
    raw = tmp_path / "已有后缀项目.auditproj"
    r = client.post("/api/project/create", json={"path": str(raw), "name": "项目"}, headers=_h(t))
    assert r.status_code == 200
    created = Path(r.json()["path"])
    assert created.name == "已有后缀项目.auditproj"
    assert created.is_dir()


def test_create_project_renames_existing_empty_dir(client, tmp_path):
    """用户先建的空目录被就地改名，不留无用空文件夹。"""
    t = _login(client)
    empty = tmp_path / "空目录项目"
    empty.mkdir()
    r = client.post("/api/project/create", json={"path": str(empty), "name": "项目"}, headers=_h(t))
    assert r.status_code == 200
    created = Path(r.json()["path"])
    assert created.name == "空目录项目" + PROJECT_EXT
    assert created.is_dir()
    assert not empty.exists(), "原空目录应被改名，不残留"


def test_create_project_removes_empty_dir_when_same_ext_exists(client, tmp_path):
    """同目录已有同名 .auditproj（旧服务残留）时，用户新建的空文件夹被删除不留残留。"""
    t = _login(client)
    empty = tmp_path / "同名项目"
    empty.mkdir()
    existing = tmp_path / "同名项目.auditproj"
    existing.mkdir()
    r = client.post("/api/project/create", json={"path": str(empty), "name": "同名项目"}, headers=_h(t))
    assert r.status_code == 200
    assert Path(r.json()["path"]) == existing
    assert not empty.exists(), "空文件夹应被清理，不留残留"
    assert existing.is_dir(), "已有伪装项目目录保留"


def test_create_project_keeps_nonexistent_parent_clean(client, tmp_path):
    """输入不存在的路径：只创建 .auditproj 目录，原始路径不产生。"""
    t = _login(client)
    raw = tmp_path / "新项目"
    assert not raw.exists()
    r = client.post("/api/project/create", json={"path": str(raw), "name": "新项目"}, headers=_h(t))
    assert r.status_code == 200
    assert Path(r.json()["path"]).name == "新项目" + PROJECT_EXT
    assert not raw.exists()


def test_delete_project_removes_dir_and_closes_session(client, tmp_path):
    """删除项目：目录消失，当前会话项目被关闭。"""
    t = _login(client)
    raw = tmp_path / "待删除项目"
    r = client.post("/api/project/create", json={"path": str(raw), "name": "待删除项目"}, headers=_h(t))
    created = Path(r.json()["path"])
    assert created.is_dir()

    d = client.post("/api/project/delete", json={"path": str(created)}, headers=_h(t))
    assert d.status_code == 200
    assert not created.exists(), "项目目录应被删除"
    # 会话项目已关闭：current 返回 400（未打开项目）
    assert client.get("/api/project/current", headers=_h(t)).status_code == 400


def test_delete_project_rejects_non_auditproj_dir(client, tmp_path):
    """只允许删除 .auditproj 伪装项目，防误删普通文件夹。"""
    t = _login(client)
    plain = tmp_path / "普通文件夹"
    plain.mkdir()
    r = client.post("/api/project/delete", json={"path": str(plain)}, headers=_h(t))
    assert r.status_code == 400
    assert plain.is_dir(), "普通文件夹不应被删除"
    # 不存在的 .auditproj 项目 → 404
    r2 = client.post("/api/project/delete", json={"path": str(tmp_path / "不存在.auditproj")}, headers=_h(t))
    assert r2.status_code == 404


def test_open_project_accepts_extended_dir(client, tmp_path):
    """打开带 .auditproj 后缀的项目目录（打开不校验扩展名）。"""
    t = _login(client)
    raw = tmp_path / "回访项目"
    r = client.post("/api/project/create", json={"path": str(raw), "name": "回访项目"}, headers=_h(t))
    assert r.status_code == 200
    created = Path(r.json()["path"])
    # 关闭会话后重新打开（新会话，路径用伪装后的目录）
    assert client.delete("/api/session", headers=_h(t)).status_code == 200
    t2 = _login(client, "复核员")
    r2 = client.post("/api/project/open", json={"path": str(created)}, headers=_h(t2))
    assert r2.status_code == 200
    assert Path(r2.json()["path"]) == created


def test_open_project_auto_appends_ext(client, tmp_path):
    """用户手动输入不带 .auditproj 后缀的路径也能打开伪装项目。"""
    t = _login(client)
    raw = tmp_path / "手输路径项目"
    r = client.post("/api/project/create", json={"path": str(raw), "name": "手输路径项目"}, headers=_h(t))
    assert r.status_code == 200
    created = Path(r.json()["path"])
    assert client.delete("/api/session", headers=_h(t)).status_code == 200
    t2 = _login(client, "复核员")
    # 不带后缀打开 → 自动补 .auditproj
    r2 = client.post("/api/project/open", json={"path": str(tmp_path / "手输路径项目")}, headers=_h(t2))
    assert r2.status_code == 200
    assert Path(r2.json()["path"]) == created
    # 不存在的路径仍 404
    r3 = client.post("/api/project/open", json={"path": str(tmp_path / "完全不存在")}, headers=_h(t2))
    assert r3.status_code == 404


def test_harden_project_missing_dir_returns_false(tmp_path):
    """不存在的目录：harden_project 返回 False 不抛异常。"""
    from platform_adapter import harden_project

    assert harden_project(tmp_path / "不存在") is False


def test_harden_project_keeps_read_write(tmp_path):
    """隐藏后程序仍可读写（chflags/attrib 只影响文件管理器显示）。"""
    from platform_adapter import harden_project

    d = tmp_path / "读写验证.auditproj"
    d.mkdir()
    assert harden_project(d) is True
    probe = d / "probe.txt"
    probe.write_text("数据", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "数据"
    probe.unlink()


# ───────────────────────── V3.2 恢复项目目录伪装 ─────────────────────────

def _make_backup(client, t, tmp_path, name="备份源项目"):
    """造一个项目 + 一条单位 + 备份，返回备份文件路径（备份落在项目上级目录）。"""
    raw = tmp_path / name
    r = client.post("/api/project/create", json={"path": str(raw), "name": name}, headers=_h(t))
    assert r.status_code == 200
    r2 = client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=_h(t))
    assert r2.status_code == 200
    b = client.post("/api/backup/create", headers=_h(t))
    assert b.status_code == 200
    return tmp_path / b.json()["filename"]


def _restore(client, t, bak, target):
    with open(bak, "rb") as fh:
        return client.post("/api/backup/restore",
                           files={"file": ("backup.auditbak", fh.read(), "application/zip")},
                           data={"target_dir": str(target)}, headers=_h(t))


def test_restore_backup_appends_ext_and_hides(client, tmp_path):
    """恢复备份：目标自动加 .auditproj 后缀并隐藏（与新建项目一致）。"""
    t = _login(client)
    bak = _make_backup(client, t, tmp_path)
    target = tmp_path / "恢复项目"
    r = _restore(client, t, bak, target)
    assert r.status_code == 200, r.text
    restored = Path(r.json()["path"])
    assert restored.name == "恢复项目" + PROJECT_EXT
    assert restored.is_dir()
    assert not target.exists(), "原路径不应残留（只产生伪装目录）"
    if sys.platform in ("darwin", "win32"):
        assert _is_hidden(restored), "恢复的项目目录应已隐藏"
    # 打开接口按不带后缀路径自动补后缀
    t2 = _login(client, "复核员")
    r2 = client.post("/api/project/open", json={"path": str(target)}, headers=_h(t2))
    assert r2.status_code == 200
    assert Path(r2.json()["path"]) == restored


def test_restore_backup_consumes_existing_empty_dir(client, tmp_path):
    """恢复目标选择已有空文件夹：空壳被消费，项目落在 .auditproj，不留残留。"""
    t = _login(client)
    bak = _make_backup(client, t, tmp_path)
    empty = tmp_path / "空恢复目标"
    empty.mkdir()
    r = _restore(client, t, bak, empty)
    assert r.status_code == 200, r.text
    restored = Path(r.json()["path"])
    assert restored.name == "空恢复目标" + PROJECT_EXT
    assert restored.is_dir()
    assert not empty.exists(), "用户先建的空文件夹应被消费，不留残留"


def test_restore_backup_rejects_existing_same_ext(client, tmp_path):
    """目标已存在同名伪装目录：拒绝恢复（防覆盖已有项目）。"""
    t = _login(client)
    bak = _make_backup(client, t, tmp_path)
    empty = tmp_path / "目标项目"
    empty.mkdir()
    existing = tmp_path / "目标项目.auditproj"
    existing.mkdir()
    r = _restore(client, t, bak, empty)
    assert r.status_code == 400
    assert "同名项目目录" in r.json()["detail"]
    assert existing.is_dir(), "已有伪装目录不应被覆盖"
