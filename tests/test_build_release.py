"""T10 双端构建 + 发布产物清单用例。

覆盖（对应 TASKS.md T10 验收）：
- spec 文件平台分支语法可解析（macOS BUNDLE / Windows 无 BUNDLE）
- version.py 单一版本来源
- release_manifest.py：单文件 sha256 / 目录树确定性哈希 / manifest.txt 生成
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# ── version.py 单一来源 ──

def test_version_single_source():
    """version.py 可导入且提供打包所需的三个常量。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from version import APP_NAME, APP_VERSION, BUNDLE_ID

    assert APP_NAME == "审迹"
    assert APP_VERSION == "1.2.0"
    assert BUNDLE_ID.startswith("com.")
    root = Path(__file__).resolve().parent.parent
    package = json.loads((root / "frontend-v3" / "package.json").read_text(encoding="utf-8"))
    frontend_version = (root / "frontend-v3" / "src" / "version.ts").read_text(encoding="utf-8")
    assert package["version"] == APP_VERSION
    assert re.search(rf'APP_VERSION\s*=\s*"{re.escape(APP_VERSION)}"', frontend_version)


# ── spec 参数化：两种平台分支都可解析 ──

def test_spec_has_platform_branches():
    """spec 同时包含 macOS（BUNDLE）与 Windows（无 BUNDLE）分支。

    不实际运行 PyInstaller（慢），只断言 spec 源码结构正确：
    - 有 sys.platform 判断
    - macOS 分支有 BUNDLE
    - 有 version.py 注入（版本单一来源）
    """
    spec = (Path(__file__).resolve().parent.parent / "审迹.spec").read_text(encoding="utf-8")
    assert "sys.platform" in spec
    assert "IS_MAC" in spec
    assert "IS_WIN" in spec
    assert "BUNDLE(" in spec
    assert "CFBundleShortVersionString" in spec
    assert "version.py" in spec
    # Windows 分支不生成 BUNDLE（else 分支走 COLLECT onedir，双端一致）
    assert "exe.binaries = a.binaries" not in spec
    assert "COLLECT(" in spec
    # 双端 onedir：EXE 一律 exclude_binaries=True（二进制由 COLLECT 收集）
    assert "exclude_binaries=True" in spec
    assert "frontend-v3/dist" in spec


def test_spec_python_syntax_valid():
    """spec 是 Python 代码，语法必须有效（py_compile 检查）。"""
    import py_compile

    spec_path = Path(__file__).resolve().parent.parent / "审迹.spec"
    py_compile.compile(str(spec_path), doraise=True)


# ── release_manifest.py ──

def test_manifest_single_file(tmp_path):
    """单文件产物：sha256 正确、manifest.txt 内容齐全。"""

    # 直接导入脚本函数（scripts 目录加入 path）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import release_manifest

    f = tmp_path / "fake.exe"
    f.write_bytes(b"hello build artifact")
    expected = hashlib.sha256(b"hello build artifact").hexdigest()
    assert release_manifest.sha256_of(f) == expected

    # 用子进程方式贴近真实调用（main 读 sys.argv）
    script = Path(__file__).resolve().parent.parent / "scripts" / "release_manifest.py"
    r = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode == 0, r.stderr
    mf = tmp_path / "manifest.txt"
    assert mf.exists()
    content = mf.read_text(encoding="utf-8")
    assert "fake.exe" in content
    assert expected in content
    assert "版本：" in content
    assert "sha256" in content


def test_manifest_tree_hash_deterministic(tmp_path):
    """目录产物树哈希：同内容两次计算一致，文件变化后哈希变化。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import release_manifest

    d = tmp_path / "demo.app"
    (d / "Contents").mkdir(parents=True)
    (d / "Contents" / "a.bin").write_bytes(b"aaa")
    (d / "Contents" / "b.bin").write_bytes(b"bbb")

    h1 = release_manifest.sha256_tree(d)
    h2 = release_manifest.sha256_tree(d)
    assert h1 == h2  # 确定性

    # 改内容 → 哈希变
    (d / "Contents" / "a.bin").write_bytes(b"aax")
    h3 = release_manifest.sha256_tree(d)
    assert h3 != h1


def test_spec_ci_aligned():
    """CI 双端都用 spec（与 macOS 同一入口），不再混用命令行参数。"""
    ci = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pyinstaller --noconfirm 审迹.spec" in ci
    # 不再有 --onefile 命令行打包
    assert "--onefile" not in ci
    # 双端 job 都存在
    assert "build-windows" in ci
    assert "build-macos" in ci
    # 两端都生成发布产物清单
    assert "release_manifest.py" in ci
    # 发布构建的启动冒烟失败必须阻断产物上传。
    assert "continue-on-error: true" not in ci
    assert ci.count("启动冒烟测试（发布阻断门禁）") == 2


def test_ci_reads_the_same_instance_endpoint_as_the_application():
    """打包冒烟必须读取当前改造版实际写入的端点文件，不能沿用 v1.1 默认名。"""
    root = Path(__file__).resolve().parent.parent
    main = (root / "backend" / "main.py").read_text(encoding="utf-8")
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lock_name = re.search(r'INSTANCE_LOCK_NAME\s*=\s*"([^"]+)"', main)
    assert lock_name is not None
    endpoint_name = f"{lock_name.group(1)}.endpoint.json"
    assert ci.count(endpoint_name) >= 2
    windows_build = (root / "scripts" / "build_windows.ps1").read_text(encoding="utf-8-sig")
    assert endpoint_name in windows_build
    assert "启动器会拉起独立服务子进程后退出" in windows_build
