"""生成发布产物清单（T10）。

用法：
    python scripts/release_manifest.py <dist目录> [产物名...]

在指定目录扫描构建产物（.app / .exe / .zip / .dmg），生成 manifest.txt：
    - 版本号（backend/version.py 单一来源）
    - 文件名 / 大小 / sha256
输出 manifest.txt 到同一目录，供发布时核对。

示例：
    python scripts/release_manifest.py dist
"""

import hashlib
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认 cp1252 编码，中文输出会 UnicodeEncodeError（CI Windows 实测踩坑）。
# 强制 UTF-8 输出；文件写入已显式 encoding="utf-8"。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(path: Path) -> str:
    """目录树确定性哈希：按相对路径排序逐个哈希后整体再哈希，可复核。"""
    files = sorted(p for p in path.rglob("*") if p.is_file())
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(path).as_posix().encode("utf-8")
        h.update(f"{len(rel):08d}".encode())
        h.update(rel)
        h.update(sha256_of(p).encode())
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/release_manifest.py <dist目录> [产物名...]")
        return 2
    dist_dir = Path(sys.argv[1]).resolve()
    if not dist_dir.is_dir():
        print(f"目录不存在：{dist_dir}")
        return 2

    # 版本单一来源
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from version import APP_NAME, APP_VERSION

    # 收集产物：.app 目录 / .exe 文件 / onedir 目录（含 .exe 的目录）/ zip/dmg
    names = sys.argv[2:]
    artifacts = []
    if names:
        for n in names:
            p = dist_dir / n
            if ((p.is_dir() and p.suffix == ".app") or p.is_file()
                    or (p.is_dir() and any(p.rglob("*.exe")))):
                artifacts.append(p)
    else:
        for p in sorted(dist_dir.iterdir()):
            if (p.is_dir() and p.suffix == ".app") or (
                p.is_file() and p.suffix.lower() in (".exe", ".zip", ".dmg")
            ) or (p.is_dir() and any(p.rglob("*.exe"))):
                artifacts.append(p)

    lines = [
        f"{APP_NAME} 发布产物清单",
        f"版本：{APP_VERSION}",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "文件名 / 大小 / sha256：",
    ]
    for p in artifacts:
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        digest = sha256_tree(p) if p.is_dir() else sha256_of(p)
        lines.append(f"{p.name}\t{size}\t{digest}")

    out = dist_dir / "manifest.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 {out}")
    for l in lines:
        print("  " + l)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
