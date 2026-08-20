"""将本地 FastAPI schema 导出为前端类型生成输入。

不启动 HTTP 服务，不访问页面；仅加载应用对象并序列化 OpenAPI JSON。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
app = importlib.import_module("main").app


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python scripts/export_openapi.py <输出路径>", file=sys.stderr)
        return 2
    output_path = Path(sys.argv[1]).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
