"""检查仓库中发布工具链版本是否一致；不安装依赖，也不访问页面。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
EXPECTED = {"python": "3.14.6", "node": "24.19.0", "pnpm": "11.5.0"}


def _read(relative: str) -> str:
    return (ROOT_DIR / relative).read_text(encoding="utf-8-sig")


def check() -> list[str]:
    errors: list[str] = []
    if _read(".python-version").strip() != EXPECTED["python"]:
        errors.append(".python-version 未锁定为 3.14.6")
    if _read(".node-version").strip() != EXPECTED["node"]:
        errors.append(".node-version 未锁定为 24.19.0")
    package = json.loads(_read("frontend-v3/package.json"))
    if package.get("packageManager") != f"pnpm@{EXPECTED['pnpm']}":
        errors.append("frontend-v3/package.json 的 pnpm 版本不一致")
    if package.get("engines", {}).get("node") != EXPECTED["node"]:
        errors.append("frontend-v3/package.json 的 Node 版本不一致")
    checks = {
        ".github/workflows/ci.yml": (EXPECTED["python"], EXPECTED["node"], EXPECTED["pnpm"]),
        "scripts/build_macos.sh": (EXPECTED["python"], EXPECTED["node"], EXPECTED["pnpm"]),
        "scripts/build_windows.ps1": (EXPECTED["python"], EXPECTED["node"], EXPECTED["pnpm"]),
    }
    for relative, required in checks.items():
        text = _read(relative)
        for version in required:
            if version not in text:
                errors.append(f"{relative} 未声明锁定版本 {version}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("运行时版本不一致：", *errors, sep="\n- ", file=sys.stderr)
        return 1
    print("运行时版本矩阵一致：Python 3.14.6 / Node 24.19.0 / pnpm 11.5.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
