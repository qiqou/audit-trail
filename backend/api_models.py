"""HTTP 请求模型。

与 FastAPI 路由和桌面启动逻辑分离，统一维护接口输入边界，避免 ``main.py``
同时承担模型定义、业务编排和进程启动职责。
"""

from pydantic import BaseModel, Field


class OperatorReq(BaseModel):
    operator: str


class FolderReq(BaseModel):
    path: str


class ExportReq(BaseModel):
    scope: str = "project"
    unit_id: int | None = None


class PackageReq(BaseModel):
    scope: str = "all"
    unit_ids: list[int] = Field(default_factory=list)
    group_by_dept: bool = False
    confirmation_token: str = ""


class BackupSettingsReq(BaseModel):
    enabled: bool = False
    target_dir: str = ""
    interval_minutes: int = 360
    retention_days: int = 7
    max_bytes: int = 100 * 1024 * 1024 * 1024


class AmountSettingsReq(BaseModel):
    currency: str = "CNY"
    amount_unit: str = "元"


class LocalBackupRestoreReq(BaseModel):
    backup_path: str
    target_dir: str


class RecoveryPointRestoreReq(BaseModel):
    recovery_point_id: str
    target_dir: str


class LocalMergeReq(BaseModel):
    backup_paths: list[str] = Field(default_factory=list)
    confirmation_token: str = ""


class DeptReq(BaseModel):
    departments: list[str] = Field(default_factory=list)


class CategoryReq(BaseModel):
    categories: list[str] = Field(default_factory=list)


class OpenReq(BaseModel):
    path: str


class CreateReq(BaseModel):
    path: str
    name: str = ""


class NameReq(BaseModel):
    name: str


class OrderReq(BaseModel):
    """完整排序快照；服务端拒绝缺项、重复项和跨范围 ID。"""

    ids: list[int] = Field(default_factory=list)


class ResetReq(BaseModel):
    confirm_text: str


class IssueNumberReq(BaseModel):
    prefix: str = ""
    suffix: str = ""


class IssueReq(BaseModel):
    department: str | None = None
    category: str | None = None
    defect_type: str | None = None
    defect_desc: str | None = None
    defect_desc_rich: str | None = None
    amount: str | None = None
    currency: str | None = None
    amount_unit: str | None = None
    regulation_basis: str | None = None
    regulation_basis_rich: str | None = None
    suggestion: str | None = None
    suggestion_rich: str | None = None
    author: str | None = None
    reviewer: str | None = None
    status: str | None = None


class IssueDraftReq(BaseModel):
    """独立草稿保存请求；基线必须来自当前正式底稿读取结果。"""

    payload: IssueReq
    base_version_id: int = 0
    base_updated_at: str


class ReviewNoteCreateReq(BaseModel):
    """内部复核意见创建请求。v1.4 预留（2026-08-22 下线 API 路由）。"""

    body: str
    anchor_field: str = ""
    base_version_id: int = 0


class ReviewNoteEventReq(BaseModel):
    """内部复核意见事件请求。v1.4 预留（2026-08-22 下线 API 路由）。"""

    body: str = ""


class DuplicateIssueReq(BaseModel):
    """复制到同单位或指定单位；附件、版本和状态不参与复制。"""

    unit_id: int | None = None


class WorkpaperTemplateCreateReq(BaseModel):
    name: str
    issue_id: int


class WorkpaperTemplateApplyReq(BaseModel):
    unit_id: int


class BatchIssueMetadataReq(BaseModel):
    issue_ids: list[int] = Field(default_factory=list)
    changes: dict[str, str] = Field(default_factory=dict)
    confirmation_token: str = ""


class StatusReq(BaseModel):
    status: str
    comment: str = ""


class ExchangeRevisionReq(BaseModel):
    field_name: str
    new_value: str = ""
    reason: str = ""


class ExchangeRevisionDecisionReq(BaseModel):
    decision: str


class ExchangeCommentReq(BaseModel):
    body: str
    anchor_field: str = ""
    revision_uuid: str = ""


class ExchangeRequestReq(BaseModel):
    content: str


class ExchangeRequestUpdateReq(BaseModel):
    status: str
    provided_file_id: int | None = None
    note: str = ""


class ExchangeCloseReq(BaseModel):
    note: str = ""


class RenameItem(BaseModel):
    id: int
    name: str


class BatchRenameReq(BaseModel):
    renames: list[RenameItem]


class MoveFileReq(BaseModel):
    unit_id: int
