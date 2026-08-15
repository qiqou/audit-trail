"""本地桌面服务的进程启动与单实例运行边界。

与 HTTP 路由解耦，便于在打包环境和开发环境复用相同的端口、锁文件和就绪探测逻辑。
"""

import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from platform_adapter import (
    PlatformError,
    acquire_single_instance,
    discover_instance_endpoint,
    is_windows,
    open_browser,
    release_single_instance,
    reserve_local_port,
    spawn_detached,
    write_instance_endpoint,
)


def launch_service(*, instance_lock_name: str, host: str, port: int) -> None:
    """探测已有实例，或拉起服务子进程并在就绪后打开本地页面。"""
    def probe(url: str) -> bool:
        try:
            parsed = urlparse(url)
            with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=0.5):
                return True
        except OSError:
            return False

    endpoint = discover_instance_endpoint(instance_lock_name)
    if endpoint and probe(endpoint):
        print(f"审迹已在运行，正在打开页面：{endpoint}")
        try:
            open_browser(endpoint)
        except PlatformError as error:
            print(str(error))
        return
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--serve"]
    else:
        script = Path(sys.argv[0]).resolve() if sys.argv else Path("main.py").resolve()
        command = [sys.executable, str(script), "--serve"]
    try:
        spawn_detached(command)
    except PlatformError as error:
        print(f"无法启动服务：{error}")
        return
    url = ""
    for _ in range(40):
        time.sleep(0.25)
        endpoint = discover_instance_endpoint(instance_lock_name)
        if endpoint and probe(endpoint):
            url = endpoint
            break
    url = url or f"http://{host}:{port}/"
    print(f"审迹已启动：{url}")
    try:
        open_browser(url)
    except PlatformError as error:
        print(str(error))


def serve_app(app, *, instance_lock_name: str, host: str, port: int) -> None:
    """以单实例锁和已预留的本地端口运行 ASGI 应用。"""
    lock = acquire_single_instance(instance_lock_name)
    if lock is None:
        print("已有服务实例，退出")
        return
    listener = None
    try:
        try:
            listener, bound_port = reserve_local_port(host, port)
        except PlatformError as error:
            print(str(error))
            return
        print(f"审迹正在启动：http://{host}:{bound_port}")
        write_instance_endpoint(host, bound_port, instance_lock_name)
        import uvicorn

        if is_windows():
            # Windows 的 uvicorn fd 模式会访问不存在的 socket.AF_UNIX；端口已经
            # 由 reserve_local_port 确定，关闭后重新绑定的竞态窗口极小。
            listener.close()
            listener = None
            uvicorn.run(app, host=host, port=bound_port)
        else:
            uvicorn.run(app, fd=listener.fileno())
    finally:
        if listener is not None:
            listener.close()
        release_single_instance(lock)
        try:
            from platform_adapter import _endpoint_path

            _endpoint_path(instance_lock_name).unlink(missing_ok=True)
        except OSError:
            pass
