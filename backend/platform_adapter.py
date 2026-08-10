"""平台适配层 — 所有系统相关操作收敛于此（WP-02）。

设计原则：
- 业务层（main.py / export.py）禁止直接调用 osascript / open / explorer /
  powershell / webbrowser；统一走本模块接口
- 按 macOS / Windows 分别实现，接口一致；不支持的平台抛 PlatformError
- 错误消息面向用户：说"怎么做"，不说"怎么了"

接口：
- choose_folder(prompt) -> str          弹原生文件夹选择器，返回路径（空=取消）
- open_path(p)                          系统文件管理器打开路径（Finder/资源管理器）
- open_browser(url)                     默认浏览器打开
- port_in_use(host, port) -> bool       端口占用检测
- reserve_local_port(host, port)        预留动态本地端口，消除“探测后被抢占”窗口
- acquire_single_instance(name) -> lock 单实例锁（成功返回锁对象，失败返回 None）
- release_single_instance(lock)         释放单实例锁
- harden_project(path) -> bool          隐藏项目目录（防误删改；失败返回 False）
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

# 程序配置目录（与数据库无关的运行时状态，如单实例锁）
CONFIG_DIR = Path.home() / ".shenji"
LOCK_FILE = "shenji.lock"
ENDPOINT_FILE = "shenji.endpoint.json"


class PlatformError(Exception):
    """平台操作失败；message 是面向用户的可执行提示。"""


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def choose_folder(prompt: str = "请选择审计项目文件夹") -> str:
    """弹系统原生文件夹选择器，返回用户选择的绝对路径（取消=空串）。

    macOS 用 osascript；Windows 用 PowerShell FolderBrowserDialog。
    """
    if _is_darwin():
        # osascript 字符串内双引号需转义
        safe_prompt = prompt.replace('"', '\\"')
        cmd = ["osascript", "-e",
               f'POSIX path of (choose folder with prompt "{safe_prompt}")']
    elif _is_windows():
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f=New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$f.Description='{prompt.replace(chr(39), '')}'; "
            "if($f.ShowDialog() -eq 'OK'){$f.SelectedPath}else{''}"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps_script]
    else:
        raise PlatformError("当前系统暂不支持原生文件夹选择，请手动输入完整路径")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise PlatformError(f"无法弹出文件夹选择器：{e}。请手动输入完整路径") from e
    if r.returncode:
        detail = (r.stderr or r.stdout).strip()
        # macOS 取消原生对话框会返回 AppleScript -128；这是正常操作，不应报错。
        if "-128" in detail or "user canceled" in detail.lower() or "user cancelled" in detail.lower():
            return ""
        hint = detail or f"系统返回代码 {r.returncode}"
        raise PlatformError(f"无法弹出文件夹选择器：{hint}。请检查系统自动化权限，或手动输入完整路径")
    return r.stdout.strip()


def open_path(p) -> None:
    """在系统文件管理器中打开路径（macOS Finder / Windows 资源管理器）。"""
    target = Path(p).expanduser()
    if not target.exists():
        raise PlatformError(f"文件夹不存在：{target}。请确认路径后重试")
    try:
        if _is_darwin():
            subprocess.Popen(["open", str(target)])
        elif _is_windows():
            subprocess.Popen(["explorer", str(target)])
        else:
            raise PlatformError("当前系统暂不支持打开系统文件夹")
    except OSError as e:
        raise PlatformError(f"无法打开文件夹：{e}。请手动打开 {target}") from e


def open_browser(url: str) -> None:
    """默认浏览器打开 URL（启动自动开页面用；失败不致命）。"""
    import webbrowser

    try:
        # Finder 启动的 macOS GUI 进程有时没有可用的 webbrowser 控制器；
        # 直接交给 LaunchServices，避免“服务已运行但没有页面”的静默失败。
        if _is_darwin():
            subprocess.Popen(["open", url])
            return
        if not webbrowser.open(url, new=0, autoraise=True):
            raise OSError("系统未接受浏览器打开请求")
    except Exception as e:
        raise PlatformError(f"自动打开浏览器失败：{e}。请手动访问 {url}") from e


def write_instance_endpoint(host: str, port: int, name: str = LOCK_FILE) -> None:
    """登记当前实例地址，供重复启动时打开已有页面。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = CONFIG_DIR / (ENDPOINT_FILE if name == LOCK_FILE else f"{name}.endpoint.json")
    temp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    payload = {"pid": os.getpid(), "host": host, "port": int(port)}
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, target)


def _endpoint_path(name: str) -> Path:
    return CONFIG_DIR / (ENDPOINT_FILE if name == LOCK_FILE else f"{name}.endpoint.json")


def read_instance_endpoint(name: str = LOCK_FILE) -> str | None:
    """读取并校验实例地址；不存在或已失效时返回 None。"""
    try:
        data = json.loads(_endpoint_path(name).read_text(encoding="utf-8"))
        host = str(data["host"])
        port = int(data["port"])
        if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535:
            return None
        with socket.create_connection((host, port), timeout=0.5):
            return f"http://{host}:{port}/"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def discover_instance_endpoint(name: str = LOCK_FILE) -> str | None:
    """获取现有实例地址；兼容没有 endpoint 文件的旧版 macOS 实例。"""
    endpoint = read_instance_endpoint(name)
    if endpoint:
        return endpoint
    try:
        pid = int((CONFIG_DIR / name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not _is_darwin():
        return None
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=2, check=False,
        )
        for line in result.stdout.splitlines():
            marker = "TCP 127.0.0.1:"
            if marker not in line:
                continue
            port = line.split(marker, 1)[1].split(" ", 1)[0].split("(", 1)[0]
            if port.isdigit():
                return f"http://127.0.0.1:{int(port)}/"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def port_in_use(host: str, port: int) -> bool:
    """探测端口是否已被占用（bind 测试，不真正监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def reserve_local_port(host: str = "127.0.0.1", port: int = 0) -> tuple[socket.socket, int]:
    """绑定并监听本地端口，返回持有该端口的 socket 与实际端口号。

    port=0 时由操作系统选择空闲端口。调用方将该 socket 的文件描述符交给
    Uvicorn，而不是先探测再 bind，避免启动期间被其他程序抢占端口。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
        return listener, int(listener.getsockname()[1])
    except OSError as e:
        listener.close()
        label = str(port) if port else "动态"
        raise PlatformError(f"无法预留{label}本地端口：{e}。请关闭占用程序后重试") from e


def acquire_single_instance(name: str = LOCK_FILE) -> object | None:
    """获取单实例锁。成功返回锁文件对象（持有引用即持锁）；失败返回 None。

    macOS/Linux 用 fcntl.flock；Windows 用 msvcrt.locking。锁文件在 ~/.shenji/。
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # 锁文件必须保持打开（flock 作用在 fd 上），不能进 with——close 即释放锁
        # 不能用 "w"：第二个实例会在尝试加锁前先清空 PID，导致已运行实例无法被发现。
        f = open(CONFIG_DIR / name, "a+")  # noqa: SIM115 持锁期间句柄必须存活
        try:
            if _is_windows():
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f
    except OSError:
        return None


def release_single_instance(lock) -> None:
    """释放单实例锁（锁对象不存在时静默，进程退出时 OS 自动释放）。"""
    if lock is None:
        return
    try:
        if _is_windows():
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    except OSError:
        pass


def harden_project(path) -> bool:
    """隐藏项目目录，默认资源管理器/Finder 不可见（防人员误删改）。

    macOS 用 chflags hidden；Windows 用 attrib +h。隐藏属性不影响程序按路径
    读写，只影响文件管理器的默认显示。目录不存在或命令失败返回 False
    （隐藏是增强措施，失败不阻断项目创建）。
    """
    target = Path(path).expanduser()
    if not target.is_dir():
        return False
    try:
        if _is_darwin():
            r = subprocess.run(
                ["chflags", "hidden", str(target)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return r.returncode == 0
        if _is_windows():
            r = subprocess.run(
                ["attrib", "+h", str(target)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return r.returncode == 0
        return False
    except (subprocess.TimeoutExpired, OSError):
        return False
