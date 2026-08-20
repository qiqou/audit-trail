"""运行配置：把环境差异收口在一处，业务代码不再散落读取环境变量。"""

import os
from dataclasses import dataclass

# 目录伪装扩展名：新建/恢复项目目录名自动追加该后缀（资源管理器里看起来像文件），
# 配合 platform_adapter.harden_project 的隐藏属性，防止人员误入项目目录删改文件。
PROJECT_EXT = ".auditproj"


@dataclass(frozen=True)
class RuntimeSettings:
    """本地桌面服务配置。

    正式包始终仅监听回环地址；AUDIT_ASSISTANT_PORT 仅供开发和自动化测试覆盖。
    端口为 0 时由操作系统选择可用端口。
    """

    host: str = "127.0.0.1"
    # 改造版与原审迹 v1.1 使用不同固定端口，二者可同时运行且各自保留
    # localStorage（最近项目等按 origin 隔离）。测试/开发可用环境变量覆盖。
    port: int = 8766
    debug: bool = False
    use_webview: bool = True
    frontend: str = "v3"

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        # 默认固定端口 8766（与 dataclass 默认一致），避免与原审迹 v1.1
        # 的 8765 冲突；测试/打包可用 AUDIT_ASSISTANT_PORT 覆盖。
        raw_port = os.environ.get("AUDIT_ASSISTANT_PORT", "8766").strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("AUDIT_ASSISTANT_PORT 必须是 0 到 65535 的整数") from exc
        if not 0 <= port <= 65535:
            raise ValueError("AUDIT_ASSISTANT_PORT 必须是 0 到 65535 的整数")
        frontend = os.environ.get("AUDIT_ASSISTANT_FRONTEND", "v3").strip().lower()
        if frontend != "v3":
            raise ValueError("AUDIT_ASSISTANT_FRONTEND 当前仅支持 v3")
        return cls(
            port=port,
            debug=os.environ.get("AUDIT_ASSISTANT_DEBUG", "").lower() in {"1", "true", "yes"},
            use_webview=os.environ.get("AUDIT_ASSISTANT_WEBVIEW", "1").lower() not in {"0", "false", "no"},
            frontend=frontend,
        )
