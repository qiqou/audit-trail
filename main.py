"""根目录入口转发 — 供 `uvicorn main:app` / `hermes verify` / 打包使用。

实际逻辑在 backend/main.py。
"""

from backend.main import app  # noqa: F401

if __name__ == "__main__":
    from backend.main import main

    main()
