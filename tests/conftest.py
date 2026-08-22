"""pytest 共享夹具。

- 保证 backend/ 可被导入（pytest 从 tests/ 目录运行时 sys.path 不含 backend）
- 每个测试一个独立临时项目夹，测试间互不影响
"""

import sys
from pathlib import Path

import pytest

# 项目根加入 sys.path：让 test_contract_snapshot 等导入 scripts/* 的测试
# 不依赖 `python -m pytest`（console script 方式不会把 cwd 加入 sys.path）。
# 注意：backend 必须排在 root 之前——root 下也有 main.py（启动器），
# 而测试期望 `import main` 解析到 backend/main.py。
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def proj(tmp_path):
    """临时项目夹具：每个测试拿到全新 AuditProject（tmp_path 自动清理）。"""
    from database import AuditProject

    p = AuditProject(tmp_path / "测试项目")
    yield p
    p.close()


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    """隔离 ~/.shenji 配置目录：最近项目记录等运行时状态写入临时目录，不污染本机。"""
    import platform_adapter

    monkeypatch.setattr(platform_adapter, "CONFIG_DIR", tmp_path / ".shenji")
    yield
