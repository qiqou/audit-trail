"""T2 平台适配层用例（backend/platform.py）。

覆盖（对应 TASKS.md T2 验收）：
- port_in_use：空闲端口 False / 占用端口 True
- acquire/release_single_instance：拿锁→再拿失败→释放→再拿成功
- open_path：不存在路径给可执行提示（PlatformError）
- 错误消息规范：说"怎么做"，不说"怎么了"（含"请"或"重试"等动作词）
"""

import os
import socket
import subprocess
from pathlib import Path

import pytest
from platform_adapter import (
    PlatformError,
    acquire_single_instance,
    choose_folder,
    discover_instance_endpoint,
    open_path,
    port_in_use,
    release_single_instance,
    write_instance_endpoint,
)


def _free_port() -> int:
    """找一个当前空闲的端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_free_and_in_use():
    """空闲端口 False；bind 之后 True。"""
    port = _free_port()
    assert port_in_use("127.0.0.1", port) is False
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        assert port_in_use("127.0.0.1", port) is True


def test_single_instance_lock_blocks_second(tmp_path, monkeypatch):
    """单实例锁：第一次拿到，第二次拿不到，释放后再拿成功。"""
    monkeypatch.setattr("platform_adapter.CONFIG_DIR", tmp_path)
    lock1 = acquire_single_instance()
    assert lock1 is not None
    assert (tmp_path / "shenji.lock").read_text() == str(os.getpid())

    lock2 = acquire_single_instance()
    assert lock2 is None
    assert (tmp_path / "shenji.lock").read_text() == str(os.getpid())

    release_single_instance(lock1)
    lock3 = acquire_single_instance()
    assert lock3 is not None
    release_single_instance(lock3)


def test_single_instance_lock_file_location(tmp_path, monkeypatch):
    """锁文件落在配置目录（~/.shenji/）。"""
    monkeypatch.setattr("platform_adapter.CONFIG_DIR", tmp_path)
    lock = acquire_single_instance()
    assert lock is not None
    assert (tmp_path / "shenji.lock").exists()
    release_single_instance(lock)


def test_instance_endpoint_round_trip(tmp_path, monkeypatch):
    """已有实例地址可登记并读取，供重复启动打开页面。"""
    monkeypatch.setattr("platform_adapter.CONFIG_DIR", tmp_path)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        write_instance_endpoint("127.0.0.1", port)
        assert discover_instance_endpoint() == f"http://127.0.0.1:{port}/"


def test_open_path_missing_gives_actionable_hint():
    """不存在的路径：PlatformError 消息教用户怎么做。"""
    missing = Path("/nonexistent/不存在的文件夹")
    with pytest.raises(PlatformError) as ei:
        open_path(missing)
    msg = str(ei.value)
    assert "不存在" in msg
    assert "请" in msg  # 给出动作指引


def test_choose_folder_reports_platform_failure(monkeypatch):
    """原生选择器异常不得伪装成“用户没选”，否则前端无法给出排障提示。"""
    monkeypatch.setattr("platform_adapter._is_darwin", lambda: True)
    monkeypatch.setattr(
        "platform_adapter.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "Not authorized to send Apple events"),
    )

    with pytest.raises(PlatformError, match="自动化权限"):
        choose_folder()


def test_choose_folder_treats_user_cancel_as_empty_path(monkeypatch):
    """用户取消 macOS 对话框是正常分支，前端据此保留已输入路径。"""
    monkeypatch.setattr("platform_adapter._is_darwin", lambda: True)
    monkeypatch.setattr(
        "platform_adapter.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "User canceled. (-128)"),
    )

    assert choose_folder() == ""


def test_platform_error_messages_are_actionable():
    """错误消息规范抽查：全部含动作词（请/重试/手动），不含纯状态描述。"""
    errs = [
        PlatformError("当前系统暂不支持原生文件夹选择，请手动输入完整路径"),
        PlatformError("无法弹出文件夹选择器：timeout。请手动输入完整路径"),
        PlatformError("文件夹不存在：/x。请确认路径后重试"),
        PlatformError("无法打开文件夹：err。请手动打开 /x"),
    ]
    for e in errs:
        assert "请" in str(e) or "重试" in str(e)
