"""附件导入、文件操作与下载接口。"""

from collections.abc import Awaitable, Callable
from typing import Any

from api_models import BatchRenameReq, MoveFileReq, NameReq
from fastapi import APIRouter, Depends, File, Form, UploadFile


def build_router(
    get_operator: Callable[..., str],
    open_unit_attachment_directory: Callable[[int, str], dict],
    open_evidence_folder: Callable[[int, str], dict],
    upload_folder: Callable[..., Awaitable[dict]],
    upload_file: Callable[..., Awaitable[dict]],
    download_file: Callable[[int, str], Any],
    open_file: Callable[[int, str], dict],
    rename_file: Callable[[int, NameReq, str], dict],
    batch_rename_files: Callable[[BatchRenameReq, str], dict],
    move_file: Callable[[int, MoveFileReq, str], dict],
    remove_file: Callable[[int, str], dict],
) -> APIRouter:
    """路由只负责 HTTP 形状；既有文件操作动作暂由下一层逐步迁移。"""
    router = APIRouter()

    @router.get(
        "/api/units/{unit_id}/attachments/open",
        operation_id="open_unit_attachment_directory_get",
        summary="Open Unit Attachment Directory",
    )
    @router.post(
        "/api/units/{unit_id}/attachments/open",
        operation_id="open_unit_attachment_directory_post",
        summary="Open Unit Attachment Directory",
    )
    def open_unit_attachment_directory_route(unit_id: int, operator: str = Depends(get_operator)):
        """在系统文件管理器中打开单位附件库。

        路径只能由项目和 unit_id 在服务端解析，避免前端按显示名称拼接目录，
        也避免把任意本地路径暴露给工作台操作。
        """
        return open_unit_attachment_directory(unit_id, operator)

    @router.get(
        "/api/files/{file_id}/directory/open",
        operation_id="open_evidence_folder_get",
        summary="Open Evidence Folder",
    )
    @router.post(
        "/api/files/{file_id}/directory/open",
        operation_id="open_evidence_folder_post",
        summary="Open Evidence Folder",
    )
    def open_evidence_folder_route(file_id: int, operator: str = Depends(get_operator)):
        """打开“文件夹证据”自身目录，而不是错误地下载或跳到单位根目录。"""
        return open_evidence_folder(file_id, operator)

    @router.post(
        "/api/units/{unit_id}/folder-upload",
        operation_id="upload_folder_api_units__unit_id__folder_upload_post",
        summary="Upload Folder",
    )
    async def upload_folder_route(unit_id: int, folder_name: str = Form(...), files: list[UploadFile] = File(...),
                                  operator: str = Depends(get_operator)):
        """文件夹上传：内容打包 zip 存为单个附件实体（按单文件规则处理）。

        files 的 filename 携带 zip 内相对路径（前端递归展开后传入）。
        """
        return await upload_folder(unit_id, folder_name, files, operator)

    @router.post(
        "/api/units/{unit_id}/files",
        operation_id="upload_file_api_units__unit_id__files_post",
        summary="Upload File",
    )
    async def upload_file_route(unit_id: int, file: UploadFile = File(...), folder_path: str = Form(""),
                                operator: str = Depends(get_operator)):
        """附件上传：项目级重复检测（同内容只存一份）→ 入库 → 可关联。

        folder_path 可选：所属文件夹相对路径（文件夹上传时由前端递归展开传入）。
        """
        return await upload_file(unit_id, file, folder_path, operator)

    @router.get(
        "/api/files/{file_id}/download",
        operation_id="download_file_api_files__file_id__download_get",
        summary="Download File",
    )
    def download_file_route(file_id: int, _: str = Depends(get_operator)):
        return download_file(file_id, _)

    @router.post(
        "/api/files/{file_id}/open",
        operation_id="open_file_api_files__file_id__open_post",
        summary="Open File",
    )
    def open_file_route(file_id: int, operator: str = Depends(get_operator)):
        """用系统默认程序打开附件文件（macOS open / Windows 默认关联程序）。"""
        return open_file(file_id, operator)

    @router.patch(
        "/api/files/{file_id}",
        operation_id="rename_file_api_files__file_id__patch",
        summary="Rename File",
    )
    def rename_file_route(file_id: int, req: NameReq, operator: str = Depends(get_operator)):
        return rename_file(file_id, req, operator)

    @router.post(
        "/api/files/batch-rename",
        operation_id="batch_rename_files_api_files_batch_rename_post",
        summary="Batch Rename Files",
    )
    def batch_rename_files_route(req: BatchRenameReq, operator: str = Depends(get_operator)):
        """批量重命名附件：事务内冲突检测，冲突条目跳过并返回原因（审查 F-06 补齐）。"""
        return batch_rename_files(req, operator)

    @router.post(
        "/api/files/{file_id}/move",
        operation_id="move_file_api_files__file_id__move_post",
        summary="Move File",
    )
    def move_file_route(file_id: int, req: MoveFileReq, operator: str = Depends(get_operator)):
        """移动附件到其他单位：物理移动 + 事务更新归属（审查 F-06 补齐）。"""
        return move_file(file_id, req, operator)

    @router.delete(
        "/api/files/{file_id}",
        operation_id="remove_file_api_files__file_id__delete",
        summary="Remove File",
    )
    def remove_file_route(file_id: int, operator: str = Depends(get_operator)):
        return remove_file(file_id, operator)

    return router
