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
- spawn_detached(cmd, cwd)              脱离当前进程启动子进程（启动器/重启共用）
- is_windows()                          平台能力判断，业务层不直接读取 sys.platform
- acquire_single_instance(name) -> lock 单实例锁（成功返回锁对象，失败返回 None）
- release_single_instance(lock)         释放单实例锁
- harden_project(path) -> bool          隐藏项目目录（防误删改；失败返回 False）
"""

import csv
import getpass
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# 程序配置目录（与数据库无关的运行时状态，如单实例锁）
CONFIG_DIR = Path.home() / ".shenji"
LOCK_FILE = "shenji.lock"
ENDPOINT_FILE = "shenji.endpoint.json"
RECENT_FILE = "recent_projects.json"
DEVICE_FILE = "device.json"
_RECENT_LOCK = threading.Lock()
_DEVICE_LOCK = threading.Lock()
_EPHEMERAL_DEVICE_ID = str(uuid.uuid4())


class PlatformError(Exception):
    """平台操作失败；message 是面向用户的可执行提示。"""


@dataclass(frozen=True)
class OSIdentity:
    """当前启动程序的操作系统账户及本安装实例标识。

    ``device_id`` 是本机本程序安装生成的随机 UUID，不采集硬件指纹；它只用于
    在离线项目日志中区分同一 OS 账户的不同安装实例。
    """

    account_name: str
    account_id: str
    device_id: str


def _device_id() -> str:
    """获取或创建稳定安装标识；配置目录不可写时降级为进程内标识。

    本机配置目录写入失败不应阻断离线审计工作；降级时日志仍可区分本次运行，
    只是服务重启后安装标识会变化。调用方无需处理权限异常。
    """
    with _DEVICE_LOCK:
        target = CONFIG_DIR / DEVICE_FILE
        try:
            value = str(json.loads(target.read_text(encoding="utf-8")).get("device_id", ""))
            uuid.UUID(value)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            value = str(uuid.uuid4())
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
            temp.write_text(json.dumps({"device_id": value}), encoding="utf-8")
            os.replace(temp, target)
            return value
        except OSError:
            return _EPHEMERAL_DEVICE_ID


def current_os_identity() -> OSIdentity:
    """返回实际运行审迹的 OS 账户，禁止由前端姓名字段伪造。

    macOS 使用 UID；Windows 优先使用当前账户 SID（读取失败才降级为
    ``DOMAIN\\USERNAME``）。
    这不是登录认证机制：审迹是离线单人工具，目的仅是让操作日志绑定系统账户。
    """
    try:
        account_name = getpass.getuser().strip()
    except (OSError, KeyError):
        account_name = ""
    account_name = account_name or os.environ.get("USERNAME") or os.environ.get("USER") or "未知账户"
    if _is_windows():
        domain = (os.environ.get("USERDOMAIN") or "").strip()
        fallback = f"{domain}\\{account_name}" if domain else account_name
        try:
            result = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                text=True, timeout=3, check=False,
            )
            row = next(csv.reader(io.StringIO(result.stdout)), [])
            candidate = row[-1].strip() if result.returncode == 0 and row else ""
            account_id = candidate if candidate.upper().startswith("S-") else fallback
        except (OSError, subprocess.SubprocessError):
            account_id = fallback
    else:
        account_id = str(os.getuid()) if hasattr(os, "getuid") else account_name
    return OSIdentity(account_name=account_name, account_id=account_id, device_id=_device_id())


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def is_windows() -> bool:
    """是否为Windows。供业务层选择平台能力，避免散落读取 ``sys.platform``。"""
    return _is_windows()


def spawn_detached(command: list[str], cwd: str | os.PathLike[str] | None = None) -> None:
    """以平台正确的方式启动脱离当前生命周期的子进程。

    启动器拉起服务、服务自重启都必须使用同一策略：macOS 保留新会话；
    Windows 使用独立进程组和脱离标志，避免父进程退出时误带走服务进程。
    """
    if not command:
        raise PlatformError("启动命令为空，请重新启动程序")
    kwargs: dict = {"cwd": str(cwd) if cwd is not None else None}
    if _is_windows():
        # 常量仅在Windows解释器暴露；数值来自Win32 CreateProcess 标志，
        # 用 getattr 也让非Windows的单元测试可以验证分支。
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        kwargs["creationflags"] = (
            new_group | detached
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError as e:
        raise PlatformError(f"无法启动本地服务：{e}。请完全退出审迹后重试") from e


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
        raise PlatformError(f"路径不存在：{target}。请确认路径后重试")
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
    except OSError as e:
        raise PlatformError(f"自动打开浏览器失败：{e}。请手动访问 {url}") from e


def load_recent_all() -> dict:
    """读取全部使用人的最近项目记录（JSON: {operator: [items]}，损坏时返回空）。"""
    try:
        return json.loads((CONFIG_DIR / RECENT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_recent_all(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_DIR / f"{RECENT_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG_DIR / RECENT_FILE)


def remember_recent(operator: str, path: str, name: str, limit: int = 20) -> None:
    """记录最近打开/创建的项目（按使用人隔离，最新在前，最多 limit 条）。

    path 相同视为同一项目（重命名时更新名称与时间）。写失败静默——
    本地快捷记录不阻塞项目打开。
    """
    if not operator or not path or not name:
        return
    with _RECENT_LOCK:
        data = load_recent_all()
        items = [it for it in data.get(operator, []) if isinstance(it, dict) and it.get("path") != path]
        items.insert(0, {"path": path, "name": name, "time": int(time.time() * 1000)})
        data[operator] = items[:limit]
        try:
            _save_recent_all(data)
        except OSError:
            pass


def forget_recent(operator: str, path: str) -> None:
    """从最近记录中移除指定项目（不移除磁盘上的项目）。"""
    if not operator or not path:
        return
    with _RECENT_LOCK:
        data = load_recent_all()
        items = data.get(operator, [])
        kept = [it for it in items if isinstance(it, dict) and it.get("path") != path]
        if len(kept) == len(items):
            return
        data[operator] = kept
        try:
            _save_recent_all(data)
        except OSError:
            pass


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
    """收紧项目目录访问并隐藏目录，降低误删和跨账户误读风险。

    POSIX 系统将项目目录设为 0700、已存在的数据库和清单设为 0600；macOS
    另用 chflags hidden，Windows 仍用 attrib +h。隐藏属性本身不等于加密或访问
    控制，目录权限也不替代 FileVault/BitLocker 等受控设备措施。失败不阻断创建。
    """
    target = Path(path).expanduser()
    if not target.is_dir():
        return False
    try:
        if not _is_windows():
            target.chmod(0o700)
            for protected in (target / "audit.db", target / "manifest.json"):
                if protected.is_file():
                    protected.chmod(0o600)
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
        return not _is_windows()
    except (subprocess.TimeoutExpired, OSError):
        return False
