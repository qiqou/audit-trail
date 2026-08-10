"""pytest 共享夹具。

- 保证 backend/ 可被导入（pytest 从 tests/ 目录运行时 sys.path 不含 backend）
- 每个测试一个独立临时项目夹，测试间互不影响
"""

import sys
from pathlib import Path

import pytest

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
