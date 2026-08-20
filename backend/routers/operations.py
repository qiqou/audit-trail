"""导入、导出、备份和本机系统操作接口。"""

from collections.abc import Awaitable, Callable
from typing import Any

from api_models import (
    BackupSettingsReq,
    ExportReq,
    FolderReq,
    LocalBackupRestoreReq,
    LocalMergeReq,
    PackageReq,
    RecoveryPointRestoreReq,
)
from fastapi import APIRouter, Depends, File, Form, UploadFile


def build_router(
    get_operator: Callable[..., str],
    list_logs_action: Callable[..., list[dict]],
    export_audit_logs_action: Callable[..., dict],
    export_diagnostics_support_package_action: Callable[[str], dict],
    import_template_action: Callable[[str], Any],
    import_excel_action: Callable[..., Awaitable[dict]],
    import_excel_preflight_action: Callable[..., Awaitable[dict]],
    commit_excel_import_action: Callable[..., Awaitable[dict]],
    import_merge_action: Callable[..., Awaitable[dict]],
    import_merge_local_action: Callable[..., Awaitable[dict]],
    import_merge_local_preflight_action: Callable[..., Awaitable[dict]],
    export_excel_action: Callable[[ExportReq, str], dict],
    package_project_action: Callable[[PackageReq, str], dict],
    package_preflight_action: Callable[[PackageReq, str], dict],
    create_backup_action: Callable[[str], dict],
    get_backup_settings_action: Callable[[str], dict],
    save_backup_settings_action: Callable[[BackupSettingsReq, str], dict],
    create_auto_recovery_point_action: Callable[[str], dict],
    list_auto_recovery_points_action: Callable[[str], list[dict]],
    restore_auto_recovery_point_action: Callable[..., Awaitable[dict]],
    download_backup_action: Callable[[str, str], Any],
    restore_backup_action: Callable[..., Awaitable[dict]],
    restore_local_backup_action: Callable[..., Awaitable[dict]],
    download_export_action: Callable[[str, str], Any],
    restart_program_action: Callable[[str], dict],
    quit_program_action: Callable[[str], dict],
    choose_folder_action: Callable[[str], dict],
    open_folder_action: Callable[[FolderReq, str], dict],
) -> APIRouter:
    """保留既有操作动作，先将 HTTP 契约集中到 operations 路由。"""
    router = APIRouter()

    @router.get("/api/logs")
    def list_logs(
        limit: int = 500, actor: str = "", action: str = "", start_date: str = "", end_date: str = "",
        _: str = Depends(get_operator),
    ):
        return list_logs_action(limit, actor, action, start_date, end_date, _)

    @router.post("/api/logs/export")
    def export_audit_logs(
        actor: str = "", action: str = "", start_date: str = "", end_date: str = "",
        operator: str = Depends(get_operator),
    ):
        """导出项目永久操作日志 CSV，供项目经理复核或支持留档。"""
        return export_audit_logs_action(actor, action, start_date, end_date, operator)

    @router.post("/api/diagnostics/support-package")
    def export_diagnostics_support_package(operator: str = Depends(get_operator)):
        """生成默认剔除业务内容和路径的本机诊断支持包。"""
        return export_diagnostics_support_package_action(operator)

    @router.get("/api/import/template")
    def import_template(_: str = Depends(get_operator)):
        """下载导入模板 xlsx。"""
        return import_template_action(_)

    @router.post("/api/import/excel")
    async def import_excel(file: UploadFile = File(...), operator: str = Depends(get_operator)):
        """上传整理好的 xlsx，一键导入底稿（单位不存在自动创建）。"""
        return await import_excel_action(file, operator)

    @router.post("/api/import/excel/preflight")
    async def import_excel_preflight(file: UploadFile = File(...), _: str = Depends(get_operator)):
        """只读校验 Excel，显示影响摘要后发放一次性提交令牌。"""
        return await import_excel_preflight_action(file, _)

    @router.post("/api/import/excel/commit")
    async def commit_excel_import(
        file: UploadFile = File(...), confirmation_token: str = "", operator: str = Depends(get_operator),
    ):
        """提交与预检摘要一致的 Excel；底稿写入为整批原子替换。"""
        return await commit_excel_import_action(file, confirmation_token, operator)

    @router.post("/api/import/merge")
    async def import_merge(files: list[UploadFile] = File(...), operator: str = Depends(get_operator)):
        """旧上传入口已停用：正式合并需本机路径预检，避免大包限制和绕过冲突确认。"""
        return await import_merge_action(files, operator)

    @router.post("/api/import/merge-local")
    async def import_merge_local(req: LocalMergeReq, operator: str = Depends(get_operator)):
        """通过预检确认后从本机路径合并，适用于 50GB 场景。"""
        return await import_merge_local_action(req, operator)

    @router.post("/api/import/merge-local/preflight")
    async def import_merge_local_preflight(req: LocalMergeReq, _: str = Depends(get_operator)):
        """对本机备份来源做只读预检，发现冲突先展示，确认后才允许写入。"""
        return await import_merge_local_preflight_action(req, _)

    @router.post("/api/export/excel")
    def export_excel(req: ExportReq, operator: str = Depends(get_operator)):
        """导出问题汇总表 Excel（unit=当前单位 / project=全部单位）。"""
        return export_excel_action(req, operator)

    @router.post("/api/export/package")
    def package_project(req: PackageReq, operator: str = Depends(get_operator)):
        """通过归档核对令牌后打包 ZIP；项目变化或核对过期必须重新确认。"""
        return package_project_action(req, operator)

    @router.post("/api/export/package/preflight")
    def package_preflight(req: PackageReq, _: str = Depends(get_operator)):
        """生成归档核对清单。无阻断项时发放一次性确认令牌，有效期 10 分钟。"""
        return package_preflight_action(req, _)

    @router.post("/api/backup/create")
    def create_backup(operator: str = Depends(get_operator)):
        """备份项目（audit.db + 附件库）到上级目录 .auditbak。"""
        return create_backup_action(operator)

    @router.get("/api/backup/settings")
    def get_backup_settings(_: str = Depends(get_operator)):
        return get_backup_settings_action(_)

    @router.post("/api/backup/settings")
    def save_backup_settings(req: BackupSettingsReq, operator: str = Depends(get_operator)):
        return save_backup_settings_action(req, operator)

    @router.post("/api/backup/recovery-point")
    def create_auto_recovery_point(operator: str = Depends(get_operator)):
        """手工立即创建一份增量恢复点；仍使用已保存的自动备份目标和空间策略。"""
        return create_auto_recovery_point_action(operator)

    @router.get("/api/backup/recovery-points")
    def list_auto_recovery_points(_: str = Depends(get_operator)):
        """列出当前项目自动备份目标中的可用恢复点。"""
        return list_auto_recovery_points_action(_)

    @router.post("/api/backup/recovery-points/restore")
    async def restore_auto_recovery_point(
        req: RecoveryPointRestoreReq, operator: str = Depends(get_operator),
    ):
        """从内容寻址自动备份恢复点恢复；始终写入一个新项目目录。"""
        return await restore_auto_recovery_point_action(req, operator)

    @router.get("/api/backup/download/{filename}")
    def download_backup(filename: str, _: str = Depends(get_operator)):
        """下载备份 .auditbak（存放于项目上级目录，不走输出目录端点）。

        防目录穿越：解析后的路径必须落在项目上级目录内。
        """
        return download_backup_action(filename, _)

    @router.post("/api/backup/restore")
    async def restore_backup(
        file: UploadFile = File(...), target_dir: str = Form(...), operator: str = Depends(get_operator),
    ):
        """恢复备份：上传 .auditbak + 目标目录（须为空）。"""
        return await restore_backup_action(file, target_dir, operator)

    @router.post("/api/backup/restore-local")
    async def restore_local_backup(req: LocalBackupRestoreReq, operator: str = Depends(get_operator)):
        """从本机路径恢复完整 .auditbak，避免 50GB 文件经浏览器上传的大小限制。"""
        return await restore_local_backup_action(req, operator)

    @router.get("/api/export/file/{filename}")
    def download_export(filename: str, _: str = Depends(get_operator)):
        """下载输出目录中的导出文件（Excel/ZIP/备份）。"""
        return download_export_action(filename, _)

    @router.post("/api/system/restart")
    def restart_program(_: str = Depends(get_operator)):
        """重启程序：结束当前服务进程并重新拉起（页面卡死/数据异常时自救）。

        返回响应后延迟 0.8s 执行：先关闭所有项目连接（SQLite 干净落盘），
        再后台拉起新进程（开发态同命令同目录；打包态直接重启可执行文件），
        最后退出当前进程。新进程会重新分配端口并自动打开浏览器。
        """
        return restart_program_action(_)

    @router.post("/api/system/quit")
    def quit_program(_: str = Depends(get_operator)):
        """退出程序：关闭所有项目连接（SQLite 干净落盘）后退出进程。

        打包版为 LSUIElement（无 Dock 图标），页面内退出是唯一优雅关闭入口。
        """
        return quit_program_action(_)

    @router.post("/api/system/choose-folder")
    def choose_folder(_: str = Depends(get_operator)):
        """弹系统原生文件夹选择器（平台适配层 choose_folder）。

        浏览器安全限制无法直接选文件夹路径，由后端弹原生对话框返回路径。
        用户取消时返回空路径。
        """
        return choose_folder_action(_)

    @router.post("/api/system/open-folder")
    def open_folder(req: FolderReq, _: str = Depends(get_operator)):
        """在系统文件管理器中打开指定文件夹（平台适配层 open_path）。"""
        return open_folder_action(req, _)

    return router
