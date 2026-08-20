"""数据层 — SQLite 存储。

项目文件夹 = 自包含数据包（audit.db + 附件库/ + 输出/）。
整体拷走文件夹即可转移项目，程序不依赖项目文件夹外的任何数据。

设计要点：
- 所有写操作走事务，崩溃不损坏数据
- 底稿每次内容变化自动留版本快照（issue_versions），可恢复历史版本
- 所有变更类操作写 audit_log 留痕（谁/何时/做了什么），日志随项目走
- 附件磁盘名用 uuid 防重名，原始文件名只用于展示
- 使用人（operator）由调用方传入（API 层强制），数据层只负责记录
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import ClassVar

from db.migration_runner import prepare_schema_migration, record_schema_migration
from domain.errors import ConflictError
from domain.issue_workflow import (
    ISSUE_STATUSES,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_FLOW,
    STATUS_REJECTED,
    STATUS_REVIEWED,
    STATUS_SUBMITTED,
    validate_status_transition,
)
from domain.review_workflow import (
    EVENT_CREATED,
    EVENT_REOPENED,
    EVENT_REPLIED,
    EVENT_RESOLVED,
    note_state,
    validate_review_event,
)
from repositories.evidence import EvidenceRepository
from repositories.exchanges import ExchangeRepository
from repositories.issues import IssueRepository
from repositories.units import UnitRepository
from rich_text import rich_html_to_plain_text, sanitize_rich_html

DB_FILE = "audit.db"
ATTACH_DIR = "附件库"
OUT_DIR = "输出"
SNAPSHOT_DIR = "快照"
# 数据库 schema 版本（T12 版本兼容检查）
# - v1.1 及更早项目没有 schema_version 键 → 视为 0（兼容，迁移后写当前版本）
# - v2 引入 schema_migrations 和 jobs：迁移前用 SQLite backup API 创建项目内快照
# - v4 为项目、单位、底稿、附件和日志补充稳定 UUID、金额字段和日志链字段
# - v5 增加底稿回收站：删除先移入回收站，只有手动清空才物理删除
# - v6 增加项目级自动备份策略；备份对象与恢复点位于用户指定的项目外目录
# - v7 为单位和附件补充回收站软删除标识，统一所有业务实体的可恢复删除语义
# - v8 增加合并批次/冲突留痕，归档前可阻断未确认的来源冲突
# - v9 为项目核心关系补齐实际外键、活动编号唯一约束和状态 CHECK；迁移先拒绝
#   已有孤儿/重复数据，绝不为通过迁移静默删除审计记录
# - v10 增加问题交流修订：正式底稿与现场修订稿隔离，接受后才一次性写入新版本
# - v11 交流会话支持多轮持续记录：每次应用后更新基线但不结束会话，审阅信息永久保留
# - v12 每条交流修订直接绑定其生成的底稿版本，供版本时间线与修订定位可靠联动
# - v13 为 v12 前已保存的交流修订回填版本绑定，避免旧项目时间线无法联动
# - v14 交流修订按“结束本轮”批量固化为一个版本，而非每次保存生成版本
# - v15 为三个长文本字段增加受控富文本存储；纯文本投影继续用于检索、导出和交流
# - v16 增加项目级资料请求台账；请求可关联单位、底稿和已提供附件
# - v17 增加项目内底稿模板；模板只复用正文元数据，不承载人员、状态、证据或历史
# - v18 增加独立底稿草稿层；草稿不生成正式版本、不改变正式正文，且绑定基线时间
# - 打开时若项目 schema_version > 当前 → 拒绝（项目由更新版本创建，需升级程序）
SCHEMA_VERSION = 18
SCHEMA_VERSION_KEY = "schema_version"
PROJECT_UUID_KEY = "project_uuid"
DEFAULT_BACKUP_INTERVAL_MINUTES = 6 * 60
MIN_BACKUP_INTERVAL_MINUTES = 30
DEFAULT_BACKUP_RETENTION_DAYS = 7
DEFAULT_BACKUP_MAX_BYTES = 100 * 1024 * 1024 * 1024
MIN_BACKUP_RETENTION_DAYS = 1
MAX_BACKUP_RETENTION_DAYS = 3650
# 仅跳过系统自动生成、没有审计证据语义的 macOS 元数据。以“点”开头并不表示
# 文件不是业务资料；例如 .secret、.well-known 或隐藏目录中的合同均须完整保存。
SYSTEM_METADATA_NAMES = {".DS_Store"}


class _SerializedCursor:
    """为共享 SQLite 连接的游标操作补上同一把可重入锁。

    FastAPI 请求线程和项目任务工作线程共用一个项目连接。SQLite 本身支持
    ``check_same_thread=False``，但 Python 3.14 下不同线程同时推进同一连接的
    cursor 会触发 ``sqlite3.InterfaceError``。这里将 execute、fetch 和迭代都
    串行化，同时不在附件复制、压缩等长时间文件操作期间占住数据库锁。
    """

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock):
        self._cursor = cursor
        self._lock = lock

    def __getattr__(self, name):
        value = getattr(self._cursor, name)
        if not callable(value):
            return value

        def locked_call(*args, **kwargs):
            with self._lock:
                return value(*args, **kwargs)

        return locked_call

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            return next(self._cursor)


class _SerializedConnection:
    """使单项目 SQLite 连接的所有数据库调用遵循 ``AuditProject._lock``。"""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self._connection = connection
        self._lock = lock

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._connection.row_factory = value

    def __enter__(self):
        self._lock.acquire()
        try:
            self._connection.__enter__()
        except Exception:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self._lock.release()

    def execute(self, *args, **kwargs) -> _SerializedCursor:
        with self._lock:
            return _SerializedCursor(self._connection.execute(*args, **kwargs), self._lock)

    def executemany(self, *args, **kwargs) -> _SerializedCursor:
        with self._lock:
            return _SerializedCursor(self._connection.executemany(*args, **kwargs), self._lock)

    def executescript(self, *args, **kwargs) -> _SerializedCursor:
        with self._lock:
            return _SerializedCursor(self._connection.executescript(*args, **kwargs), self._lock)

    def commit(self) -> None:
        with self._lock:
            self._connection.commit()

    def backup(self, *args, **kwargs) -> None:
        with self._lock:
            self._connection.backup(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

# 底稿内容字段（更新白名单 + 版本快照范围）。``seq`` 是单位内可复用编号，
# 不作为永久实体标识；前后缀只是当前显示规则，变更不追溯保存历史。
ISSUE_FIELDS = [
    "department", "category", "defect_type", "defect_desc", "defect_desc_rich", "amount",
    "amount_minor", "currency", "amount_unit", "regulation_basis", "regulation_basis_rich", "suggestion", "suggestion_rich",
    "author", "reviewer", "status",
]
TEMPLATE_FIELDS = [
    "department", "category", "defect_type", "defect_desc", "amount", "currency", "amount_unit",
    "regulation_basis", "suggestion",
]
_TEXT_ISSUE_FIELDS = set(ISSUE_FIELDS) - {"amount_minor"}
_RICH_TEXT_FIELD_MAP = {
    "defect_desc_rich": "defect_desc",
    "regulation_basis_rich": "regulation_basis",
    "suggestion_rich": "suggestion",
}
_AMOUNT_UNITS = ("元", "万元", "亿元")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
EXCHANGE_REVISION_FIELDS = (
    "department", "category", "defect_type", "defect_desc", "amount",
    "regulation_basis", "suggestion", "author", "reviewer",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(name, max_length: int = 100) -> str:
    """清洗文件名/目录名并限制长度，避免 Windows/macOS 路径组件超限。

    去掉路径分隔符等危险字符，防止破坏目录结构；统一供 database/export 使用。
    """
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip().rstrip(".") or "未命名"
    if len(cleaned) <= max_length:
        return cleaned
    suffix = Path(cleaned).suffix
    # 长文件名仍保留常见扩展名，方便归档接收方直接识别和打开。
    if suffix and len(suffix) <= 20:
        stem_length = max(1, max_length - len(suffix))
        return (cleaned[:-len(suffix)][:stem_length].rstrip() or "未命名") + suffix
    return cleaned[:max_length].rstrip() or "未命名"


class AuditProject:
    """一个审计项目 = 一个文件夹。打开或创建项目都走这里。"""

    def __init__(self, root):
        # 统一项目根路径，避免 /var → /private/var 等符号链接层级在 relative_to()
        # 比较时产生“不是子路径”错误。
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ATTACH_DIR).mkdir(exist_ok=True)
        (self.root / OUT_DIR).mkdir(exist_ok=True)
        (self.root / SNAPSHOT_DIR).mkdir(exist_ok=True)
        self.db_path = self.root / DB_FILE
        # FastAPI 多线程会共用连接，check_same_thread=False + 可重入互斥锁保护。
        # 3.0 的任务进度回调会在同一业务操作内更新 jobs，必须允许同线程重入，
        # 但不同线程仍串行访问这一条 SQLite 连接。
        self._lock = threading.RLock()
        # 健康检查的常规抽查可复用未变化文件的摘要；归档前全量检查显式绕过它。
        self._hash_cache: dict[str, tuple[int, int, str]] = {}
        self._hash_cache_lock = threading.RLock()
        # API 层在项目打开后注入实际 OS 账户；纯数据层/历史调用保持空值兼容。
        self._actor_uid = ""
        self._device_id = ""
        self._conn = _SerializedConnection(
            sqlite3.connect(self.db_path, check_same_thread=False),
            self._lock,
        )
        self._conn.row_factory = sqlite3.Row
        self._units = UnitRepository(self._conn)
        self._issues = IssueRepository(self._conn)
        self._evidence = EvidenceRepository(self._conn)
        self._exchanges = ExchangeRepository(self._conn)
        self._conn.execute("PRAGMA foreign_keys = ON")
        # 合并/导入换库交换窗口标记（I2）：交换期间读请求短暂等待而非命中已关闭连接
        self._swapping = False
        # 单项目连接仍可能与 SQLite backup/短暂文件系统抖动相遇；有限等待比立即
        # 抛出“database is locked”更符合离线工作台的可恢复行为，且不引入多连接并发写。
        self._conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self._init_schema()
            # 先审查数据库中的路径，再迁移旧附件目录，避免不可信项目在打开时
            # 触发任何项目外的文件操作；迁移后再做一次断言性校验。
            self._validate_attachment_paths()
            self._migrate_attach_dirs()
            self._validate_attachment_paths()
        except Exception:
            self.close()
            raise

    # ───────────────────────── 建表 ─────────────────────────

    def _init_schema(self):
        """初始化或升级项目数据库。

        迁移只在版本落后时执行；在任何 DDL/ALTER 之前生成 SQLite 一致性快照。
        这比直接复制正在使用的 audit.db 更可靠，也给现场项目保留了可回退点。
        """
        with self._lock:
            migration = prepare_schema_migration(
                self._conn, self.root, schema_version_key=SCHEMA_VERSION_KEY,
                target_version=SCHEMA_VERSION, snapshot_dir=SNAPSHOT_DIR,
            )

        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key   TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS units(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    unit_uuid  TEXT,
                    name       TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    deleted_at TEXT,
                    deleted_by TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS issues(
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_uuid      TEXT,
                    unit_id         INTEGER NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
                    seq             INTEGER NOT NULL,
                    issue_code      TEXT DEFAULT '',
                    sort_order      INTEGER DEFAULT 0,
                    department      TEXT DEFAULT '',
                    category        TEXT DEFAULT '',
                    defect_type     TEXT DEFAULT '',
                    defect_desc     TEXT DEFAULT '',
                    defect_desc_rich TEXT DEFAULT '',
                    amount          TEXT DEFAULT '',
                    amount_minor    INTEGER,
                    currency        TEXT DEFAULT '',
                    amount_unit     TEXT DEFAULT '',
                    regulation_basis TEXT DEFAULT '',
                    regulation_basis_rich TEXT DEFAULT '',
                    suggestion      TEXT DEFAULT '',
                    suggestion_rich TEXT DEFAULT '',
                    author          TEXT DEFAULT '',
                    reviewer        TEXT DEFAULT '',
                    status          TEXT DEFAULT '草稿',
                    deleted_at      TEXT,
                    deleted_by      TEXT DEFAULT '',
                    created_at      TEXT,
                    updated_at      TEXT,
                    CHECK (status IN ('草稿', '编制完成', '复核退回', '已复核', '已归档'))
                );
                CREATE INDEX IF NOT EXISTS idx_issues_unit ON issues(unit_id);

                -- 项目内模板仅保存可复用的底稿正文/元数据快照。人员、状态、附件、
                -- 版本、交流和单位均不属于模板，避免跨单位复用时带入错误责任或证据范围。
                CREATE TABLE IF NOT EXISTS workpaper_templates(
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_uuid TEXT NOT NULL UNIQUE,
                    name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    snapshot    TEXT NOT NULL,
                    created_by  TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_by  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workpaper_templates_updated ON workpaper_templates(updated_at DESC, id DESC);

                -- 草稿仅用于异常恢复和跨会话续写，不能混入正式 issues 或版本历史。
                -- base_updated_at 是正式底稿基线；基线变化时恢复必须显式确认，不能静默覆盖。
                CREATE TABLE IF NOT EXISTS issue_drafts(
                    issue_id        INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
                    issue_uuid      TEXT NOT NULL,
                    base_version_id INTEGER NOT NULL DEFAULT 0,
                    base_updated_at TEXT NOT NULL,
                    payload         TEXT NOT NULL,
                    saved_by        TEXT NOT NULL,
                    saved_at        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_issue_drafts_saved ON issue_drafts(saved_at DESC);

                -- 复核意见以不可变事件保存：提出、回复、清除和重开均追加新行，
                -- 不覆盖原意见，且所有事件固定锚定创建时的正式版本。
                CREATE TABLE IF NOT EXISTS review_note_events(
                    event_uuid      TEXT PRIMARY KEY,
                    note_uuid       TEXT NOT NULL,
                    issue_id        INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                    issue_uuid      TEXT NOT NULL,
                    base_version_id INTEGER NOT NULL DEFAULT 0,
                    anchor_field    TEXT NOT NULL DEFAULT '',
                    event_seq       INTEGER NOT NULL,
                    event_type      TEXT NOT NULL,
                    body            TEXT NOT NULL DEFAULT '',
                    created_by      TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    CHECK (event_type IN ('created', 'replied', 'resolved', 'reopened'))
                );
                CREATE INDEX IF NOT EXISTS idx_review_note_events_note ON review_note_events(note_uuid, event_seq);
                CREATE INDEX IF NOT EXISTS idx_review_note_events_issue ON review_note_events(issue_id, event_seq);

                CREATE TABLE IF NOT EXISTS issue_versions(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id   INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                    version_no INTEGER NOT NULL,
                    snapshot   TEXT NOT NULL,      -- 全字段 JSON 快照
                    saved_by   TEXT DEFAULT '',
                    created_at TEXT,
                    UNIQUE(issue_id, version_no)
                );
                CREATE INDEX IF NOT EXISTS idx_versions_issue ON issue_versions(issue_id);

                CREATE TABLE IF NOT EXISTS files(
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_uuid   TEXT,
                    unit_id     INTEGER NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
                    stored_name TEXT NOT NULL,     -- 磁盘名（uuid+ext，防重名）
                    orig_name   TEXT NOT NULL,     -- 原始文件名（展示用）
                    folder_path TEXT NOT NULL DEFAULT '',  -- 所属文件夹相对路径（如 证据包/子目录/），空=根
                    rel_path    TEXT NOT NULL,     -- 附件库/{单位名}/{stored_name}
                    size        INTEGER DEFAULT 0,
                    sha256      TEXT DEFAULT '',
                    mime        TEXT DEFAULT '',          -- 文件类型标记（folder=文件夹实体）
                    exclusive_to INTEGER REFERENCES issues(id) ON DELETE SET NULL,
                    deleted_at  TEXT,
                    deleted_by  TEXT DEFAULT '',
                    created_at  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_files_unit ON files(unit_id);

                CREATE TABLE IF NOT EXISTS issue_files(
                    issue_id  INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE RESTRICT,
                    linked_at TEXT,
                    PRIMARY KEY (issue_id, file_id)
                );
                CREATE INDEX IF NOT EXISTS idx_issue_files_file ON issue_files(file_id);

                CREATE TABLE IF NOT EXISTS audit_log(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_uuid TEXT,
                    project_uuid TEXT DEFAULT '',
                    issue_uuid TEXT DEFAULT '',
                    file_uuid TEXT DEFAULT '',
                    actor_account TEXT DEFAULT '',
                    actor_uid TEXT DEFAULT '',
                    device_id TEXT DEFAULT '',
                    operator   TEXT NOT NULL,
                    action     TEXT NOT NULL,
                    target     TEXT DEFAULT '',
                    detail     TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    prev_hash TEXT DEFAULT '',
                    event_hash TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_log_operator ON audit_log(operator);
                CREATE INDEX IF NOT EXISTS idx_log_created ON audit_log(created_at);

                CREATE TABLE IF NOT EXISTS recycle_bin(
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    recycle_uuid TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id   INTEGER NOT NULL,
                    entity_uuid TEXT NOT NULL,
                    deleted_by  TEXT NOT NULL,
                    deleted_at  TEXT NOT NULL,
                    restored_at TEXT,
                    purged_at   TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recycle_uuid ON recycle_bin(recycle_uuid);
                CREATE INDEX IF NOT EXISTS idx_recycle_active ON recycle_bin(entity_type, purged_at, restored_at, deleted_at);

                CREATE TABLE IF NOT EXISTS backup_settings(
                    id               INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled          INTEGER NOT NULL DEFAULT 0,
                    target_dir       TEXT NOT NULL DEFAULT '',
                    interval_minutes INTEGER NOT NULL DEFAULT 360,
                    retention_days   INTEGER NOT NULL DEFAULT 7,
                    max_bytes        INTEGER NOT NULL DEFAULT 107374182400,
                    last_success_at  TEXT DEFAULT '',
                    last_error_at    TEXT DEFAULT '',
                    last_error       TEXT DEFAULT ''
                );
                INSERT OR IGNORE INTO backup_settings(id) VALUES(1);

                CREATE TABLE IF NOT EXISTS schema_migrations(
                    version         INTEGER PRIMARY KEY,
                    applied_at      TEXT NOT NULL,
                    backup_rel_path TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS jobs(
                    id               TEXT PRIMARY KEY,
                    type             TEXT NOT NULL,
                    status           TEXT NOT NULL,
                    payload          TEXT NOT NULL DEFAULT '{}',
                    progress         TEXT NOT NULL DEFAULT '{}',
                    result           TEXT NOT NULL DEFAULT '{}',
                    error            TEXT DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at       TEXT NOT NULL,
                    started_at       TEXT,
                    finished_at      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);

                CREATE TABLE IF NOT EXISTS merge_batches(
                    batch_uuid   TEXT PRIMARY KEY,
                    operator     TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    created_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS merge_conflicts(
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_uuid  TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    conflict_type TEXT NOT NULL,
                    message     TEXT NOT NULL,
                    resolution  TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'open',
                    resolved_by TEXT DEFAULT '',
                    created_at  TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(batch_uuid) REFERENCES merge_batches(batch_uuid)
                );
                CREATE INDEX IF NOT EXISTS idx_merge_conflicts_status ON merge_conflicts(status, created_at);

                -- 交流模式采用“正式底稿 + 修订层”。base_snapshot 是进入交流时的
                -- 不可变基线；交流期间不会直接修改 issues，避免现场讨论覆盖正式记录。
                CREATE TABLE IF NOT EXISTS exchange_sessions(
                    session_uuid    TEXT PRIMARY KEY,
                    issue_id        INTEGER REFERENCES issues(id) ON DELETE SET NULL,
                    issue_uuid      TEXT NOT NULL,
                    base_version_id INTEGER REFERENCES issue_versions(id) ON DELETE SET NULL,
                    base_snapshot   TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'open',
                    opened_by       TEXT NOT NULL,
                    opened_at       TEXT NOT NULL,
                    closed_by       TEXT DEFAULT '',
                    closed_at       TEXT,
                    close_note      TEXT DEFAULT '',
                    CHECK (status IN ('open', 'closed'))
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_sessions_issue ON exchange_sessions(issue_uuid, opened_at DESC);

                CREATE TABLE IF NOT EXISTS exchange_revisions(
                    revision_uuid   TEXT PRIMARY KEY,
                    session_uuid    TEXT NOT NULL REFERENCES exchange_sessions(session_uuid) ON DELETE CASCADE,
                    version_id      INTEGER REFERENCES issue_versions(id) ON DELETE SET NULL,
                    field_name      TEXT NOT NULL,
                    old_value       TEXT NOT NULL DEFAULT '',
                    new_value       TEXT NOT NULL DEFAULT '',
                    reason          TEXT DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'proposed',
                    proposed_by     TEXT NOT NULL,
                    proposed_at     TEXT NOT NULL,
                    decided_by      TEXT DEFAULT '',
                    decided_at      TEXT,
                    applied_by      TEXT DEFAULT '',
                    applied_at      TEXT,
                    CHECK (status IN ('proposed', 'accepted', 'rejected', 'withdrawn'))
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_revisions_session ON exchange_revisions(session_uuid, proposed_at);

                CREATE TABLE IF NOT EXISTS exchange_comments(
                    comment_uuid    TEXT PRIMARY KEY,
                    session_uuid    TEXT NOT NULL REFERENCES exchange_sessions(session_uuid) ON DELETE CASCADE,
                    revision_uuid   TEXT REFERENCES exchange_revisions(revision_uuid) ON DELETE SET NULL,
                    anchor_field    TEXT DEFAULT '',
                    body            TEXT NOT NULL,
                    created_by      TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_comments_session ON exchange_comments(session_uuid, created_at);

                CREATE TABLE IF NOT EXISTS exchange_requests(
                    request_uuid    TEXT PRIMARY KEY,
                    session_uuid    TEXT NOT NULL REFERENCES exchange_sessions(session_uuid) ON DELETE CASCADE,
                    content         TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'open',
                    provided_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                    note            TEXT DEFAULT '',
                    created_by      TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    updated_by      TEXT DEFAULT '',
                    updated_at      TEXT,
                    CHECK (status IN ('open', 'provided', 'verified', 'withdrawn'))
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_requests_session ON exchange_requests(session_uuid, created_at);

                CREATE TABLE IF NOT EXISTS project_requests(
                    request_uuid TEXT PRIMARY KEY,
                    unit_id INTEGER REFERENCES units(id) ON DELETE SET NULL,
                    issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    responsible TEXT DEFAULT '',
                    due_date TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    provided_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                    note TEXT DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_by TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    CHECK (status IN ('open', 'provided', 'verified', 'withdrawn'))
                );
                CREATE INDEX IF NOT EXISTS idx_project_requests_status_due ON project_requests(status, due_date, created_at);
                CREATE INDEX IF NOT EXISTS idx_project_requests_unit ON project_requests(unit_id, issue_id);
                """
            )
            # 迁移：旧库 files 表补充 exclusive_to 列（仅关联模式）
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(files)").fetchall()}
            if "exclusive_to" not in cols:
                self._conn.execute("ALTER TABLE files ADD COLUMN exclusive_to INTEGER")
            # 迁移：files 表补充 folder_path 列（文件夹上传）
            cols2 = {r[1] for r in self._conn.execute("PRAGMA table_info(files)").fetchall()}
            if "folder_path" not in cols2:
                self._conn.execute("ALTER TABLE files ADD COLUMN folder_path TEXT NOT NULL DEFAULT ''")
            # 迁移：files 表补充 mime 列（文件夹实体标记）
            cols3 = {r[1] for r in self._conn.execute("PRAGMA table_info(files)").fetchall()}
            if "mime" not in cols3:
                self._conn.execute("ALTER TABLE files ADD COLUMN mime TEXT DEFAULT ''")
            # v3：问题分类（可选），与版块预设分开维护；历史项目默认空值。
            issue_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(issues)").fetchall()}
            if "category" not in issue_cols:
                self._conn.execute("ALTER TABLE issues ADD COLUMN category TEXT DEFAULT ''")

            # v4：实体使用永不复用 UUID；显示编号单独冻结并允许后续按业务规则复用。
            self._ensure_columns("units", {
                "unit_uuid": "TEXT", "deleted_at": "TEXT", "deleted_by": "TEXT DEFAULT ''",
            })
            self._ensure_columns("issues", {
                "issue_uuid": "TEXT",
                "issue_code": "TEXT DEFAULT ''",
                "sort_order": "INTEGER DEFAULT 0",
                "amount_minor": "INTEGER",
                "currency": "TEXT DEFAULT ''",
                "amount_unit": "TEXT DEFAULT ''",
                "defect_desc_rich": "TEXT DEFAULT ''",
                "regulation_basis_rich": "TEXT DEFAULT ''",
                "suggestion_rich": "TEXT DEFAULT ''",
                "deleted_at": "TEXT",
                "deleted_by": "TEXT DEFAULT ''",
            })
            self._ensure_columns("files", {
                "file_uuid": "TEXT", "deleted_at": "TEXT", "deleted_by": "TEXT DEFAULT ''",
            })
            self._ensure_columns("audit_log", {
                "event_uuid": "TEXT",
                "project_uuid": "TEXT DEFAULT ''",
                "issue_uuid": "TEXT DEFAULT ''",
                "file_uuid": "TEXT DEFAULT ''",
                "actor_account": "TEXT DEFAULT ''",
                "actor_uid": "TEXT DEFAULT ''",
                "device_id": "TEXT DEFAULT ''",
                "prev_hash": "TEXT DEFAULT ''",
                "event_hash": "TEXT DEFAULT ''",
            })
            # v11：从首版交流模式升级而来的项目，为既有修订补上“已应用”标记。
            # 不能以结束会话来表示应用，否则后续多轮交流会丢失同一问题的审阅上下文。
            self._ensure_columns("exchange_revisions", {
                "version_id": "INTEGER REFERENCES issue_versions(id) ON DELETE SET NULL",
                "applied_by": "TEXT DEFAULT ''",
                "applied_at": "TEXT",
            })
            # I4：自动备份失败冷却——记录失败时间，持久故障下避免每次心跳重试
            self._ensure_columns("backup_settings", {"last_error_at": "TEXT DEFAULT ''"})
            self._ensure_columns("review_note_events", {"event_seq": "INTEGER NOT NULL DEFAULT 0"})
            for note in self._conn.execute(
                "SELECT DISTINCT note_uuid FROM review_note_events WHERE event_seq=0"
            ).fetchall():
                for sequence, row in enumerate(self._conn.execute(
                    "SELECT rowid FROM review_note_events WHERE note_uuid=? ORDER BY rowid", (note["note_uuid"],)
                ).fetchall(), start=1):
                    self._conn.execute(
                        "UPDATE review_note_events SET event_seq=? WHERE rowid=?", (sequence, row["rowid"]),
                    )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_exchange_revisions_version ON exchange_revisions(version_id)")
            self._backfill_exchange_revision_versions()
            self._backfill_v4_identity_fields()
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_units_uuid ON units(unit_uuid)")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_uuid ON issues(issue_uuid)")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_uuid ON files(file_uuid)")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_logs_event_uuid ON audit_log(event_uuid)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_project_created ON audit_log(project_uuid, created_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_active_unit ON issues(unit_id, deleted_at, seq)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_issues_active_unit_seq "
                "ON issues(unit_id, seq) WHERE deleted_at IS NULL"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_units_active ON units(deleted_at, sort_order, id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_active_unit ON files(unit_id, deleted_at, orig_name)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_active_sha ON files(sha256, deleted_at, id)")
            # 进程异常退出后，线程已不存在；把遗留活动任务标为失败，不能在下次打开
            # 项目时继续显示“正在运行”并永久占住破坏性操作入口。
            self._conn.execute(
                "UPDATE jobs SET status='error', error=CASE WHEN error='' THEN '程序在任务执行期间退出，请重新发起任务' ELSE error END, "
                "finished_at=COALESCE(finished_at, ?) WHERE status IN ('queued', 'running')",
                (_now(),),
            )

        # SQLite 不能直接为既有列补 FOREIGN KEY；旧项目在完整校验后以表重建方式
        # 迁移。新项目已在上方 CREATE TABLE 中直接得到约束。
        self._rebuild_relational_tables_if_needed()
        with self._lock, self._conn:
            record_schema_migration(
                self._conn, schema_version_key=SCHEMA_VERSION_KEY, target_version=SCHEMA_VERSION,
                applied_at=_now(), backup_rel_path=migration.backup_rel_path,
            )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """为历史项目补列；列定义由代码固定，绝不拼接外部输入。"""
        existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _backfill_exchange_revision_versions(self) -> None:
        """为早期交流修订补齐其生成的版本，不再按显示时间做前端猜测。

        旧实现没有 ``version_id``。这里按同一会话的修订顺序，以“修订前后字段值”
        匹配版本快照及其前一版快照；一版内的多字段修订允许绑定同一个版本。
        """
        rows = self._conn.execute(
            "SELECT r.rowid, r.session_uuid, r.field_name, r.old_value, r.new_value, s.issue_id "
            "FROM exchange_revisions r JOIN exchange_sessions s ON s.session_uuid=r.session_uuid "
            "WHERE r.version_id IS NULL AND r.applied_at IS NOT NULL AND s.issue_id IS NOT NULL "
            "ORDER BY r.session_uuid, r.proposed_at, r.rowid"
        ).fetchall()
        by_session: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_session.setdefault(str(row["session_uuid"]), []).append(row)
        for revision_rows in by_session.values():
            issue_id = int(revision_rows[0]["issue_id"])
            versions = self._conn.execute(
                "SELECT id, version_no, snapshot FROM issue_versions WHERE issue_id=? ORDER BY version_no", (issue_id,)
            ).fetchall()
            snapshots: list[tuple[sqlite3.Row, dict]] = []
            for version in versions:
                try:
                    snapshots.append((version, json.loads(version["snapshot"] or "{}")))
                except json.JSONDecodeError:
                    continue
            last_version_no = 0
            for revision in revision_rows:
                field = str(revision["field_name"])
                old_value = str(revision["old_value"] or "")
                new_value = str(revision["new_value"] or "")
                for index, (version, snapshot) in enumerate(snapshots):
                    if int(version["version_no"]) < last_version_no:
                        continue
                    before = snapshots[index - 1][1] if index else {}
                    if str(snapshot.get(field) or "") != new_value:
                        continue
                    if str(before.get(field) or "") != old_value:
                        continue
                    self._conn.execute(
                        "UPDATE exchange_revisions SET version_id=? WHERE rowid=?", (int(version["id"]), revision["rowid"])
                    )
                    last_version_no = int(version["version_no"])
                    break

    def _backfill_v4_identity_fields(self) -> None:
        """只为历史行补稳定标识，不猜测旧金额文本，不改写业务内容。"""
        project_uuid = self.get_meta(PROJECT_UUID_KEY, "").strip()
        if not project_uuid:
            project_uuid = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (PROJECT_UUID_KEY, project_uuid),
            )

        for table, column in (("units", "unit_uuid"), ("issues", "issue_uuid"), ("files", "file_uuid")):
            rows = self._conn.execute(
                f"SELECT id FROM {table} WHERE {column} IS NULL OR TRIM({column})=''"
            ).fetchall()
            for row in rows:
                self._conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (str(uuid.uuid4()), row["id"]))

        self._conn.execute("UPDATE issues SET sort_order=seq WHERE sort_order IS NULL OR sort_order=0")
        # ``issue_code`` 是早期 v4 预留列，不参与当前展示/唯一性；用户已确认
        # 编号可复用且不追溯前后缀变更。旧金额文本也不猜测结构化结果。
        for row in self._conn.execute(
            "SELECT id, seq FROM issues WHERE issue_code IS NULL OR TRIM(issue_code)=''"
        ).fetchall():
            self._conn.execute("UPDATE issues SET issue_code=? WHERE id=?", (self.issue_no(row["seq"]), row["id"]))

        # 日志链只迁移一次。若每次打开项目都重写全部日志，不但放大 50GB
        # 项目的启动 I/O，也会让“日志永久保存”的语义变得含混。
        needs_log_backfill = self._conn.execute(
            "SELECT 1 FROM audit_log "
            "WHERE event_uuid IS NULL OR TRIM(event_uuid)='' "
            "OR project_uuid IS NULL OR TRIM(project_uuid)='' "
            "OR event_hash IS NULL OR TRIM(event_hash)='' LIMIT 1"
        ).fetchone() is not None
        if not needs_log_backfill:
            return

        previous = ""
        rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        for row in rows:
            event_uuid = str(row["event_uuid"] or uuid.uuid4())
            payload = self._audit_event_payload(
                event_uuid=event_uuid,
                project_uuid=project_uuid,
                issue_uuid=str(row["issue_uuid"] or ""),
                file_uuid=str(row["file_uuid"] or ""),
                actor_account=str(row["actor_account"] or row["operator"] or "未知"),
                actor_uid=str(row["actor_uid"] or ""),
                device_id=str(row["device_id"] or ""),
                action=str(row["action"] or ""),
                target=str(row["target"] or ""),
                detail=str(row["detail"] or ""),
                created_at=str(row["created_at"] or ""),
                prev_hash=previous,
            )
            event_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            self._conn.execute(
                "UPDATE audit_log SET event_uuid=?, project_uuid=?, actor_account=?, prev_hash=?, event_hash=? WHERE id=?",
                (event_uuid, project_uuid, str(row["actor_account"] or row["operator"] or "未知"),
                 previous, event_hash, row["id"]),
            )
            previous = event_hash

    def _rebuild_relational_tables_if_needed(self) -> None:
        """为历史项目补齐 SQLite 实际外键，且不静默修复脏数据。

        SQLite 不支持 ``ALTER TABLE ... ADD FOREIGN KEY``。因此仅对仍是旧表结构
        的项目做一次可回滚表重建；迁移前先报告孤儿引用或活动编号/版本号重复，
        让使用人从备份修复，而不是在迁移中删除审计证据。
        """
        required = {
            "issues": {"units"},
            "issue_versions": {"issues"},
            "files": {"units", "issues"},
            "issue_files": {"issues", "files"},
        }
        existing = {
            table: {str(row[2]) for row in self._conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}
            for table in required
        }
        if all(required[table].issubset(existing[table]) for table in required):
            return

        checks = {
            "底稿指向不存在单位": "SELECT COUNT(*) FROM issues i LEFT JOIN units u ON u.id=i.unit_id WHERE u.id IS NULL",
            "附件指向不存在单位": "SELECT COUNT(*) FROM files f LEFT JOIN units u ON u.id=f.unit_id WHERE u.id IS NULL",
            "附件独占关联指向不存在底稿": "SELECT COUNT(*) FROM files f LEFT JOIN issues i ON i.id=f.exclusive_to WHERE f.exclusive_to IS NOT NULL AND i.id IS NULL",
            "版本指向不存在底稿": "SELECT COUNT(*) FROM issue_versions v LEFT JOIN issues i ON i.id=v.issue_id WHERE i.id IS NULL",
            "附件关联指向不存在实体": "SELECT COUNT(*) FROM issue_files x LEFT JOIN issues i ON i.id=x.issue_id LEFT JOIN files f ON f.id=x.file_id WHERE i.id IS NULL OR f.id IS NULL",
            "活动底稿编号重复": "SELECT COUNT(*) FROM (SELECT unit_id, seq FROM issues WHERE deleted_at IS NULL GROUP BY unit_id, seq HAVING COUNT(*) > 1)",
            "底稿版本号重复": "SELECT COUNT(*) FROM (SELECT issue_id, version_no FROM issue_versions GROUP BY issue_id, version_no HAVING COUNT(*) > 1)",
        }
        problems = [name for name, sql in checks.items() if int(self._conn.execute(sql).fetchone()[0])]
        if problems:
            raise ValueError(
                "项目包含无法安全迁移的关系数据：" + "、".join(problems)
                + "。请先从可信备份修复，不会自动删除或改写历史数据。"
            )

        # 必须在事务外关闭开关；重建在单个事务内完成，失败会回滚到旧表。
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with self._lock, self._conn:
                for index in (
                    "idx_issues_unit", "idx_versions_issue", "idx_files_unit", "idx_issue_files_file",
                    "idx_issues_active_unit", "idx_files_active_unit", "idx_files_active_sha",
                ):
                    self._conn.execute(f"DROP INDEX IF EXISTS {index}")
                # ``exchange_*`` / ``project_requests`` 是本次启动中由 CREATE TABLE IF NOT EXISTS 新建的
                # 表，但在重命名 issues/files 时 SQLite 会把它们的外键目标改写为
                # ``*_v8_legacy``。一旦旧表删除，旧项目会因此无法打开。把所有依赖
                # 表一起重建，且下面按列名复制，兼容早期候选版已有交流记录。
                legacy_tables = (
                    "review_note_events", "issue_drafts", "project_requests", "exchange_comments", "exchange_requests", "exchange_revisions", "exchange_sessions",
                    "issue_files", "issue_versions", "files", "issues", "units",
                )
                for table in legacy_tables:
                    self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v8_legacy")

                self._conn.executescript(
                    """
                    CREATE TABLE units(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, unit_uuid TEXT, name TEXT NOT NULL,
                        sort_order INTEGER DEFAULT 0, deleted_at TEXT, deleted_by TEXT DEFAULT '',
                        created_at TEXT DEFAULT (datetime('now','localtime'))
                    );
                    CREATE TABLE issues(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, issue_uuid TEXT,
                        unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
                        seq INTEGER NOT NULL, issue_code TEXT DEFAULT '', sort_order INTEGER DEFAULT 0,
                        department TEXT DEFAULT '', category TEXT DEFAULT '', defect_type TEXT DEFAULT '',
                        defect_desc TEXT DEFAULT '', defect_desc_rich TEXT DEFAULT '', amount TEXT DEFAULT '', amount_minor INTEGER,
                        currency TEXT DEFAULT '', amount_unit TEXT DEFAULT '', regulation_basis TEXT DEFAULT '',
                        regulation_basis_rich TEXT DEFAULT '', suggestion TEXT DEFAULT '', suggestion_rich TEXT DEFAULT '',
                        author TEXT DEFAULT '', reviewer TEXT DEFAULT '',
                        status TEXT DEFAULT '草稿', deleted_at TEXT, deleted_by TEXT DEFAULT '',
                        created_at TEXT, updated_at TEXT,
                        CHECK (status IN ('草稿', '编制完成', '复核退回', '已复核', '已归档'))
                    );
                    CREATE TABLE issue_versions(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                        version_no INTEGER NOT NULL, snapshot TEXT NOT NULL, saved_by TEXT DEFAULT '',
                        created_at TEXT, UNIQUE(issue_id, version_no)
                    );
                    CREATE TABLE issue_drafts(
                        issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
                        issue_uuid TEXT NOT NULL,
                        base_version_id INTEGER NOT NULL DEFAULT 0,
                        base_updated_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        saved_by TEXT NOT NULL,
                        saved_at TEXT NOT NULL
                    );
                    CREATE TABLE review_note_events(
                        event_uuid TEXT PRIMARY KEY,
                        note_uuid TEXT NOT NULL,
                        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                        issue_uuid TEXT NOT NULL,
                        base_version_id INTEGER NOT NULL DEFAULT 0,
                        anchor_field TEXT NOT NULL DEFAULT '',
                        event_seq INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        body TEXT NOT NULL DEFAULT '',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        CHECK (event_type IN ('created', 'replied', 'resolved', 'reopened'))
                    );
                    CREATE TABLE files(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, file_uuid TEXT,
                        unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
                        stored_name TEXT NOT NULL, orig_name TEXT NOT NULL,
                        folder_path TEXT NOT NULL DEFAULT '', rel_path TEXT NOT NULL, size INTEGER DEFAULT 0,
                        sha256 TEXT DEFAULT '', mime TEXT DEFAULT '',
                        exclusive_to INTEGER REFERENCES issues(id) ON DELETE SET NULL,
                        deleted_at TEXT, deleted_by TEXT DEFAULT '', created_at TEXT
                    );
                    CREATE TABLE issue_files(
                        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE RESTRICT,
                        linked_at TEXT, PRIMARY KEY(issue_id, file_id)
                    );
                    CREATE TABLE exchange_sessions(
                        session_uuid TEXT PRIMARY KEY,
                        issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
                        issue_uuid TEXT NOT NULL,
                        base_version_id INTEGER REFERENCES issue_versions(id) ON DELETE SET NULL,
                        base_snapshot TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        opened_by TEXT NOT NULL,
                        opened_at TEXT NOT NULL,
                        closed_by TEXT DEFAULT '',
                        closed_at TEXT,
                        close_note TEXT DEFAULT '',
                        CHECK (status IN ('open', 'closed'))
                    );
                    CREATE TABLE exchange_revisions(
                        revision_uuid TEXT PRIMARY KEY,
                        session_uuid TEXT NOT NULL REFERENCES exchange_sessions(session_uuid) ON DELETE CASCADE,
                        version_id INTEGER REFERENCES issue_versions(id) ON DELETE SET NULL,
                        field_name TEXT NOT NULL,
                        old_value TEXT NOT NULL DEFAULT '',
                        new_value TEXT NOT NULL DEFAULT '',
                        reason TEXT DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'proposed',
                        proposed_by TEXT NOT NULL,
                        proposed_at TEXT NOT NULL,
                        decided_by TEXT DEFAULT '',
                        decided_at TEXT,
                        applied_by TEXT DEFAULT '',
                        applied_at TEXT,
                        CHECK (status IN ('proposed', 'accepted', 'rejected', 'withdrawn'))
                    );
                    CREATE TABLE exchange_comments(
                        comment_uuid TEXT PRIMARY KEY,
                        session_uuid TEXT NOT NULL REFERENCES exchange_sessions(session_uuid) ON DELETE CASCADE,
                        revision_uuid TEXT REFERENCES exchange_revisions(revision_uuid) ON DELETE SET NULL,
                        anchor_field TEXT DEFAULT '',
                        body TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE exchange_requests(
                        request_uuid TEXT PRIMARY KEY,
                        session_uuid TEXT NOT NULL REFERENCES exchange_sessions(session_uuid) ON DELETE CASCADE,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        provided_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                        note TEXT DEFAULT '',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_by TEXT DEFAULT '',
                        updated_at TEXT,
                        CHECK (status IN ('open', 'provided', 'verified', 'withdrawn'))
                    );
                    CREATE TABLE project_requests(
                        request_uuid TEXT PRIMARY KEY,
                        unit_id INTEGER REFERENCES units(id) ON DELETE SET NULL,
                        issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
                        title TEXT NOT NULL,
                        detail TEXT DEFAULT '',
                        responsible TEXT DEFAULT '',
                        due_date TEXT DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'open',
                        provided_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                        note TEXT DEFAULT '',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_by TEXT DEFAULT '',
                        updated_at TEXT NOT NULL,
                        CHECK (status IN ('open', 'provided', 'verified', 'withdrawn'))
                    );
                    """
                )
                # 绝不能用 ``INSERT INTO table SELECT *``：v1.1 的新增列都追加在
                # 表尾，而 v1.2 的规范列定义把 UUID 等字段置于逻辑字段之前。位置
                # 复制会把单位名写成 UUID、把状态写入金额字段，属于不可接受的数据
                # 篡改风险。每张表明确列名，确保新旧物理列顺序无关。
                copy_columns = {
                    "units": "id, unit_uuid, name, sort_order, deleted_at, deleted_by, created_at",
                    "issues": (
                        "id, issue_uuid, unit_id, seq, issue_code, sort_order, department, category, defect_type, "
                        "defect_desc, defect_desc_rich, amount, amount_minor, currency, amount_unit, regulation_basis, "
                        "regulation_basis_rich, suggestion, suggestion_rich, "
                        "author, reviewer, status, deleted_at, deleted_by, created_at, updated_at"
                    ),
                    "issue_versions": "id, issue_id, version_no, snapshot, saved_by, created_at",
                    "issue_drafts": "issue_id, issue_uuid, base_version_id, base_updated_at, payload, saved_by, saved_at",
                    "review_note_events": (
                        "event_uuid, note_uuid, issue_id, issue_uuid, base_version_id, anchor_field, event_seq, event_type, "
                        "body, created_by, created_at"
                    ),
                    "files": (
                        "id, file_uuid, unit_id, stored_name, orig_name, folder_path, rel_path, size, sha256, mime, "
                        "exclusive_to, deleted_at, deleted_by, created_at"
                    ),
                    "issue_files": "issue_id, file_id, linked_at",
                    "exchange_sessions": (
                        "session_uuid, issue_id, issue_uuid, base_version_id, base_snapshot, status, opened_by, opened_at, "
                        "closed_by, closed_at, close_note"
                    ),
                    "exchange_revisions": (
                        "revision_uuid, session_uuid, version_id, field_name, old_value, new_value, reason, status, "
                        "proposed_by, proposed_at, decided_by, decided_at, applied_by, applied_at"
                    ),
                    "exchange_comments": (
                        "comment_uuid, session_uuid, revision_uuid, anchor_field, body, created_by, created_at"
                    ),
                    "exchange_requests": (
                        "request_uuid, session_uuid, content, status, provided_file_id, note, created_by, created_at, "
                        "updated_by, updated_at"
                    ),
                    "project_requests": (
                        "request_uuid, unit_id, issue_id, title, detail, responsible, due_date, status, provided_file_id, "
                        "note, created_by, created_at, updated_by, updated_at"
                    ),
                }
                for table, columns in copy_columns.items():
                    self._conn.execute(
                        f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_v8_legacy"
                    )
                for table in legacy_tables:
                    self._conn.execute(f"DROP TABLE {table}_v8_legacy")
                self._conn.executescript(
                    """
                    CREATE INDEX idx_issues_unit ON issues(unit_id);
                    CREATE UNIQUE INDEX uq_issues_active_unit_seq ON issues(unit_id, seq) WHERE deleted_at IS NULL;
                    CREATE INDEX idx_versions_issue ON issue_versions(issue_id);
                    CREATE INDEX idx_issue_drafts_saved ON issue_drafts(saved_at DESC);
                    CREATE INDEX idx_review_note_events_note ON review_note_events(note_uuid, event_seq);
                    CREATE INDEX idx_review_note_events_issue ON review_note_events(issue_id, event_seq);
                    CREATE INDEX idx_files_unit ON files(unit_id);
                    CREATE INDEX idx_issue_files_file ON issue_files(file_id);
                    CREATE INDEX idx_issues_active_unit ON issues(unit_id, deleted_at, seq);
                    CREATE INDEX idx_files_active_unit ON files(unit_id, deleted_at, orig_name);
                    CREATE INDEX idx_files_active_sha ON files(sha256, deleted_at, id);
                    CREATE INDEX idx_exchange_sessions_issue ON exchange_sessions(issue_uuid, opened_at DESC);
                    CREATE INDEX idx_exchange_revisions_session ON exchange_revisions(session_uuid, proposed_at);
                    CREATE INDEX idx_exchange_revisions_version ON exchange_revisions(version_id);
                    CREATE INDEX idx_exchange_comments_session ON exchange_comments(session_uuid, created_at);
                    CREATE INDEX idx_exchange_requests_session ON exchange_requests(session_uuid, created_at);
                    CREATE INDEX idx_project_requests_status_due ON project_requests(status, due_date, created_at);
                    CREATE INDEX idx_project_requests_unit ON project_requests(unit_id, issue_id);
                    """
                )
                violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise ValueError("项目关系约束迁移校验失败，已回滚")
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    def _reopen_connection_after_swap(self) -> None:
        """原子导入/合并替换 audit.db 后重建受锁保护的连接与读取仓储。"""
        self._conn = _SerializedConnection(
            sqlite3.connect(self.db_path, check_same_thread=False), self._lock,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._units = UnitRepository(self._conn)
        self._issues = IssueRepository(self._conn)
        self._evidence = EvidenceRepository(self._conn)
        self._exchanges = ExchangeRepository(self._conn)

    def close(self):
        self._conn.close()

    def attachment_path(self, rel_path: str) -> Path:
        """解析附件记录路径，并强制限制在本项目的附件库中。

        ``files.rel_path`` 属于项目数据，不能因为项目库被篡改或来自不可信备份，
        就获得读取、删除或打包任意本地文件的能力。
        """
        raw = str(rel_path or "").replace("\\", "/")
        candidate = (self.root / PurePosixPath(raw)).resolve()
        attachment_root = (self.root / ATTACH_DIR).resolve()
        if candidate == attachment_root or not candidate.is_relative_to(attachment_root):
            raise ValueError("附件路径超出当前项目附件库")
        return candidate

    @staticmethod
    def _folder_member_path(dest_dir: Path, raw_path: str) -> Path:
        """校验文件夹导入成员路径，阻止 ``..`` 和绝对路径逃逸。"""
        relative = PurePosixPath(str(raw_path or "").replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise ValueError("文件夹内包含非法相对路径")
        root = dest_dir.resolve()
        candidate = (root / relative).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError("文件夹内包含非法相对路径")
        return candidate

    def _validate_attachment_paths(self) -> None:
        """拒绝打开携带非法附件路径的项目，避免后续操作访问项目外文件。"""
        with self._lock:
            rows = self._conn.execute("SELECT id, rel_path FROM files ORDER BY id").fetchall()
        invalid = [str(row["id"]) for row in rows if self._attachment_path_invalid(row["rel_path"])]
        if invalid:
            shown = invalid[:6]
            suffix = "等" if len(invalid) > len(shown) else ""
            raise ValueError(
                f"项目包含不安全的附件路径（记录 id={','.join(shown)}{suffix}），已拒绝打开。"
                "请从可信备份恢复项目。"
            )

    def _attachment_path_invalid(self, rel_path: str) -> bool:
        try:
            self.attachment_path(rel_path)
            return False
        except ValueError:
            return True

    # ───────────────────────── 项目健康检查 / 清单 ─────────────────────────

    MANIFEST_FILE = "manifest.json"

    def health_check(self, sample_size: int = 20, progress: Callable[[int, int, str], None] | None = None,
                     cancel_event: threading.Event | None = None) -> dict:
        """项目健康检查：数据完整性 + 物理文件一致性。

        检查项：
        - orphan_link  关联表指向不存在的底稿/附件（P0）
        - orphan_issue 底稿指向不存在的单位（P0）
        - orphan_filerow 附件记录指向不存在的单位（P0）
        - missing_file files 有记录但物理文件/目录缺失（P0）
        - orphan_phys  附件库存在但 files 无记录的物理文件（P1）
        - hash_mismatch 抽查附件哈希与登记不符（P1）

        sample_size: 哈希抽查数量（<=0 = 全量；默认 20，控制大项目耗时）。
        progress: 可选进度回调 progress(done, total, phase)——T7 扫描用。
                  phase: "db" / "phys" / "hash"。
        cancel_event: 可选取消事件，置位后扫描尽早退出并标记 cancelled。
        返回 {ok, checked_at, counts, sample, problems, cancelled}。不写库、不留痕。
        """
        cancelled = False
        problems = []
        with self._lock, self._conn:
            units = {r["id"] for r in self._conn.execute("SELECT id FROM units").fetchall()}
            issues = self._conn.execute("SELECT id, unit_id FROM issues").fetchall()
            files = [dict(r) for r in self._conn.execute("SELECT * FROM files").fetchall()]
            issue_ids = {r["id"] for r in issues}
            file_ids = {r["id"] for r in files}
            if progress:
                progress(0, 1, "db")

            # 1) 关联表孤儿
            for r in self._conn.execute(
                "SELECT issue_id, file_id FROM issue_files"
            ).fetchall():
                if r["issue_id"] not in issue_ids or r["file_id"] not in file_ids:
                    problems.append({
                        "type": "orphan_link", "severity": "P0",
                        "message": f"附件关联指向不存在的底稿/附件（issue_id={r['issue_id']}, file_id={r['file_id']}）",
                    })
            # 2) 底稿/附件记录指向不存在的单位
            for r in issues:
                if r["unit_id"] not in units:
                    problems.append({
                        "type": "orphan_issue", "severity": "P0",
                        "message": f"底稿(id={r['id']}) 指向不存在的单位(id={r['unit_id']})",
                    })
            for f in files:
                if f["unit_id"] not in units:
                    problems.append({
                        "type": "orphan_filerow", "severity": "P0",
                        "message": f"附件记录(id={f['id']}) 指向不存在的单位(id={f['unit_id']})",
                    })
                exclusive_to = f.get("exclusive_to")
                if exclusive_to is not None and exclusive_to not in issue_ids:
                    problems.append({
                        "type": "orphan_exclusive", "severity": "P0",
                        "message": f"附件「{f['orig_name']}」独占关联指向不存在的底稿(id={exclusive_to})，附件会从资料库隐藏",
                    })
            # 独占附件必须且只能关联 exclusive_to 指定的底稿；否则“仅关联”语义失真。
            link_map: dict[int, set[int]] = {}
            for row in self._conn.execute("SELECT issue_id, file_id FROM issue_files").fetchall():
                link_map.setdefault(row["file_id"], set()).add(row["issue_id"])
            for f in files:
                exclusive_to = f.get("exclusive_to")
                if exclusive_to is None or exclusive_to not in issue_ids:
                    continue
                linked = link_map.get(f["id"], set())
                if linked != {exclusive_to}:
                    problems.append({
                        "type": "exclusive_link_mismatch", "severity": "P0",
                        "message": f"附件「{f['orig_name']}」标记为仅关联底稿(id={exclusive_to})，但实际关联为 {sorted(linked)}",
                    })
            # 3) files 有记录但物理缺失
            for f in files:
                try:
                    phys = self.attachment_path(f["rel_path"])
                except ValueError:
                    problems.append({
                        "type": "unsafe_file_path", "severity": "P0",
                        "message": f"附件「{f['orig_name']}」路径超出项目附件库（{f['rel_path']}）",
                    })
                    continue
                is_folder = f.get("mime") == "folder"
                if is_folder:
                    if not phys.is_dir():
                        problems.append({
                            "type": "missing_file", "severity": "P0",
                            "message": f"文件夹实体「{f['orig_name']}」物理目录缺失（{f['rel_path']}）",
                        })
                elif not phys.is_file():
                    problems.append({
                        "type": "missing_file", "severity": "P0",
                        "message": f"附件「{f['orig_name']}」物理文件缺失（{f['rel_path']}）",
                    })

        # 4) 物理存在但无记录（孤儿物理文件）——注意文件夹实体目录内文件属实体，不算孤儿；
        #    仅忽略明确的系统元数据，隐藏业务资料也必须被登记或报告。
        known = {f["rel_path"] for f in files}
        folder_dirs = {f["rel_path"] for f in files if f.get("mime") == "folder"}
        att = self.root / ATTACH_DIR
        if att.is_dir():
            phys_files = [p for p in att.rglob("*") if p.is_file()]
            for i, phys in enumerate(phys_files, 1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if progress:
                    progress(i, len(phys_files), "phys")
                rel = phys.relative_to(self.root).as_posix()
                if any(part in SYSTEM_METADATA_NAMES for part in phys.relative_to(att).parts):
                    continue
                if rel in known:
                    continue
                # 位于某文件夹实体目录内 → 属于该实体，跳过
                if any(rel.startswith(fd + "/") for fd in folder_dirs):
                    continue
                problems.append({
                    "type": "orphan_phys", "severity": "P1",
                    "message": f"附件库存在未登记文件（{rel}）",
                })

        # 5) 文件夹实体目录摘要全量核验：相对路径、成员增删和任一成员内容
        # 改动都会改变摘要。它是归档前证据完整性校验的基础，不能只抽查。
        folder_targets = [f for f in files if f.get("mime") == "folder" and f.get("sha256")]
        for idx, f in enumerate(folder_targets, 1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if progress:
                progress(idx, len(folder_targets), "folder_hash")
            try:
                phys = self.attachment_path(f["rel_path"])
                if not phys.is_dir():
                    continue  # 物理缺失已在第 3 项报告
                actual = self._folder_digest(phys, use_cache=sample_size > 0)
            except (OSError, ValueError) as e:
                problems.append({
                    "type": "folder_hash_error", "severity": "P0",
                    "message": f"文件夹实体「{f['orig_name']}」无法核验目录摘要：{e}",
                })
                continue
            if actual != f["sha256"]:
                problems.append({
                    "type": "folder_hash_mismatch", "severity": "P0",
                    "message": f"文件夹实体「{f['orig_name']}」的成员或内容已变化",
                })

        # 6) 普通文件哈希抽查（文件夹实体已在上一步全量核验）
        sample = {"checked": 0, "total": 0}
        targets = [f for f in files if f.get("sha256") and f.get("mime") != "folder"]
        sample["total"] = len(targets)
        limit = len(targets) if sample_size <= 0 else min(sample_size, len(targets))
        for idx, f in enumerate(targets[:limit], 1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if progress:
                progress(idx, limit, "hash")
            try:
                phys = self.attachment_path(f["rel_path"])
            except ValueError:
                continue
            if not phys.is_file():
                continue  # 缺失已在第 3 项报
            actual = self._cached_sha256(phys, use_cache=sample_size > 0)
            sample["checked"] += 1
            if actual != f["sha256"]:
                problems.append({
                    "type": "hash_mismatch", "severity": "P1",
                    "message": f"附件「{f['orig_name']}」哈希与登记不符（可能被篡改或损坏）",
                })

        counts = {
            "units": len(units),
            "issues": len(issue_ids),
            "files": len(file_ids),
            "versions": self._conn.execute("SELECT COUNT(*) c FROM issue_versions").fetchone()["c"],
            "logs": self._conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"],
        }
        return {
            "ok": not problems,
            "checked_at": _now(),
            "counts": counts,
            "sample": sample,
            "problems": problems,
            "cancelled": cancelled,
        }

    def write_manifest(self) -> dict:
        """生成/刷新项目清单 manifest.json（原子写，项目根目录）。

        内容：schema 版本、项目名、生成时间、各表计数。
        与健康检查解耦：任何时刻调用都只反映当下状态。
        """
        with self._lock, self._conn:
            counts = {
                "units": self._conn.execute("SELECT COUNT(*) c FROM units").fetchone()["c"],
                "issues": self._conn.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"],
                "files": self._conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
                "versions": self._conn.execute("SELECT COUNT(*) c FROM issue_versions").fetchone()["c"],
                "logs": self._conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"],
            }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "project_name": self.project_name,
            "created_at": _now(),
            "counts": counts,
        }
        tmp = self.root / f"{self.MANIFEST_FILE}.tmp"
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.root / self.MANIFEST_FILE)
        return manifest

    # ───────────────────────── 项目元信息 ─────────────────────────

    @staticmethod
    def unit_dir_name(unit_id: int) -> str:
        """单位附件物理目录名：unit_{id}，稳定不随显示名变化（审查 F-05 修复）。

        显示名（units.name）可重命名，物理目录保持稳定，避免同名清洗碰撞。
        """
        return f"unit_{unit_id}"

    def unit_attachment_dir(self, unit_id: int) -> Path:
        """返回指定单位的附件目录，始终按稳定 unit_id 解析。

        供 API 打开本地目录时使用；前端不得以单位显示名称拼接物理路径，
        否则单位重命名或历史目录迁移后会打开错误位置。
        """
        if not self.get_unit(unit_id):
            raise KeyError(f"单位不存在: {unit_id}")
        directory = self.root / ATTACH_DIR / self.unit_dir_name(unit_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _migrate_attach_dirs(self) -> list[str]:
        """迁移旧版附件目录（附件库/{单位名}）到新结构（附件库/unit_{id}）。

        幂等：已迁移的单位目录不存在旧路径即跳过。返回迁移警告清单（无则为空）。
        迁移规则：
        - 旧目录存在且新目录不存在 → 重命名
        - 新目录已存在 → 合并内容（文件名冲突加 _dup 后缀，不覆盖）
        - 更新 files.rel_path 指向新物理路径
        """
        warnings = []
        units = self.list_units()
        att = self.root / ATTACH_DIR
        for u in units:
            old_dir = att / _safe(u["name"])
            new_dir = att / self.unit_dir_name(u["id"])
            if not old_dir.exists() or old_dir == new_dir:
                continue
            # 碰撞检测：多个单位映射到同一旧目录名（清洗后同名），人工处理
            collided = [x["name"] for x in units if x["id"] != u["id"]
                        and _safe(x["name"]) == _safe(u["name"])]
            if new_dir.exists():
                # 合并：把旧目录内容搬入新目录，冲突文件加后缀不覆盖
                for src in old_dir.iterdir():
                    dst = new_dir / src.name
                    if dst.exists():
                        dst = new_dir / f"{src.stem}_dup{src.suffix}"
                        warnings.append(f"单位「{u['name']}」目录合并：{src.name} 重名已存为 {dst.name}")
                    shutil.move(str(src), str(dst))
                old_dir.rmdir()  # 空目录清理（ignore 子目录递归情况）
                if old_dir.exists():
                    shutil.rmtree(old_dir, ignore_errors=True)
            else:
                old_dir.rename(new_dir)
            # 更新库中 rel_path 前缀：附件库/{旧名}/ → 附件库/unit_{id}/
            old_prefix = f"{ATTACH_DIR}/{_safe(u['name'])}/"
            new_prefix = f"{ATTACH_DIR}/{self.unit_dir_name(u['id'])}/"
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "UPDATE files SET rel_path = ? || substr(rel_path, ?) "
                    "WHERE rel_path LIKE ?",
                    (new_prefix, len(old_prefix) + 1, old_prefix + "%"),
                )
                if cur.rowcount:
                    self._conn.commit()
            if collided:
                warnings.append(f"单位「{u['name']}」与 {'、'.join(collided)} 清洗后目录同名，"
                                "迁移后按 unit_id 隔离；原混存内容需人工核对")
        return warnings

    def get_meta(self, key: str, default: str = "") -> str:
        r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def set_meta_with_log(self, key: str, value: str, operator: str, action: str, target: str, detail: str = "") -> None:
        """项目配置与永久日志在同一 SQLite 事务内更新。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._log_in_transaction(operator, action, target, detail)

    def save_issue_number_rule(self, operator: str, prefix: str, suffix: str) -> dict:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('issue_number_prefix',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (prefix,),
            )
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('issue_number_suffix',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (suffix,),
            )
            self._log_in_transaction(operator, "更新编号规则", f"前缀「{prefix}」后缀「{suffix}」")
        return {"prefix": prefix, "suffix": suffix}

    def get_backup_settings(self) -> dict:
        """读取项目自动备份策略；默认关闭，只有用户明确指定目标目录后才可启用。"""
        row = self._conn.execute("SELECT * FROM backup_settings WHERE id=1").fetchone()
        if row is None:  # 兼容损坏或极早期项目，后续保存时会修复。
            return {
                "enabled": False, "target_dir": "",
                "interval_minutes": DEFAULT_BACKUP_INTERVAL_MINUTES,
                "retention_days": DEFAULT_BACKUP_RETENTION_DAYS,
                "max_bytes": DEFAULT_BACKUP_MAX_BYTES,
                "last_success_at": "", "last_error": "",
            }
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def save_backup_settings(
        self, operator: str, *, enabled: bool, target_dir: str,
        interval_minutes: int, retention_days: int, max_bytes: int,
    ) -> dict:
        """保存自动备份策略，并对会造成失控任务的参数做硬校验。"""
        interval = int(interval_minutes)
        retention = int(retention_days)
        maximum = int(max_bytes)
        path = str(target_dir or "").strip()
        if interval < MIN_BACKUP_INTERVAL_MINUTES:
            raise ValueError(f"自动备份间隔不能短于 {MIN_BACKUP_INTERVAL_MINUTES} 分钟")
        if maximum <= 0:
            raise ValueError("自动备份最大保留空间必须大于 0")
        if not MIN_BACKUP_RETENTION_DAYS <= retention <= MAX_BACKUP_RETENTION_DAYS:
            raise ValueError(
                f"自动备份恢复点保留天数必须在 {MIN_BACKUP_RETENTION_DAYS} 至 "
                f"{MAX_BACKUP_RETENTION_DAYS} 天之间"
            )
        if enabled:
            if not path:
                raise ValueError("开启自动备份前必须指定目标目录")
            target = Path(path).expanduser()
            if not target.is_dir():
                raise ValueError("自动备份目标目录不存在或不是文件夹")
            root = self.root.resolve()
            resolved = target.resolve()
            if resolved == root or resolved.is_relative_to(root):
                raise ValueError("自动备份目标目录不能位于当前项目内")
            path = str(resolved)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE backup_settings SET enabled=?, target_dir=?, interval_minutes=?, retention_days=?, max_bytes=? WHERE id=1",
                (int(bool(enabled)), path, interval, retention, maximum),
            )
            self._log_in_transaction(
                operator, "更新自动备份策略",
                "自动备份" if enabled else "关闭自动备份",
                f"间隔 {interval} 分钟；保留 {retention} 天；最大空间 {maximum} 字节"
                + (f"；目标 {path}" if enabled else ""),
            )
        return self.get_backup_settings()

    def record_auto_backup_result(
        self, *, success: bool, message: str = "", operator: str = "", target: str = "",
    ) -> None:
        """记录自动备份运行结果，并在提供使用人时与永久日志同事务落库。"""
        with self._lock, self._conn:
            if success:
                self._conn.execute(
                    "UPDATE backup_settings SET last_success_at=?, last_error='' WHERE id=1", (_now(),)
                )
                if operator:
                    self._log_in_transaction(operator, "自动备份", target, message)
            else:
                self._conn.execute(
                    "UPDATE backup_settings SET last_error=?, last_error_at=? WHERE id=1",
                    (str(message)[:1000], _now()),
                )
                if operator:
                    self._log_in_transaction(operator, "自动备份失败", target, str(message)[:1000])

    @property
    def project_uuid(self) -> str:
        """项目不可变标识；v4迁移已保证打开后必有值。"""
        return self.get_meta(PROJECT_UUID_KEY, "")

    def set_audit_identity(self, actor_uid: str, device_id: str) -> None:
        """设置本次打开项目的 OS 身份元数据，供所有后续日志自动带入。"""
        self._actor_uid = str(actor_uid or "")
        self._device_id = str(device_id or "")

    def issue_no(self, seq) -> str:
        """当前显示编号：前缀 + 单位内数字 + 后缀。

        编号不是永久实体标识（永久标识为 ``issue_uuid``），删除后数字可复用；
        修改前后缀不回写、不追溯历史编号，所有展示/导出使用当前规则。
        """
        prefix = self.get_meta("issue_number_prefix", "")
        suffix = self.get_meta("issue_number_suffix", "")
        return f"{prefix}{seq}{suffix}"

    def get_amount_settings(self) -> dict:
        """项目级金额默认口径；只影响之后的新录入，不重写历史数据。"""
        currency = self.get_meta("amount_default_currency", "CNY").strip().upper() or "CNY"
        unit = self.get_meta("amount_default_unit", "元").strip() or "元"
        return {"currency": currency, "amount_unit": unit, "allowed_units": list(_AMOUNT_UNITS)}

    def save_amount_settings(self, operator: str, *, currency: str, amount_unit: str) -> dict:
        normalized_currency = str(currency or "").strip().upper()
        normalized_unit = str(amount_unit or "").strip()
        if not _CURRENCY_RE.fullmatch(normalized_currency):
            raise ValueError("币种必须为 3 位 ISO 代码，例如 CNY、USD")
        if normalized_unit not in _AMOUNT_UNITS:
            raise ValueError(f"金额单位仅支持：{'、'.join(_AMOUNT_UNITS)}")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('amount_default_currency',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (normalized_currency,),
            )
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('amount_default_unit',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (normalized_unit,),
            )
            self._log_in_transaction(operator, "更新金额默认口径", f"{normalized_currency} · {normalized_unit}")
        return self.get_amount_settings()

    @property
    def project_name(self) -> str:
        return self.get_meta("project_name", self.root.name)

    @project_name.setter
    def project_name(self, v: str):
        v = str(v).strip()
        if not v:
            raise ValueError("项目名称不能为空")
        self.set_meta("project_name", v)

    # ───────────────────────── 审计单位 ─────────────────────────

    def get_unit(self, unit_id: int, *, include_deleted: bool = False):
        return self._units.get(unit_id, include_deleted=include_deleted)

    def list_units(self) -> list[dict]:
        return self._units.list_active()

    def add_unit(self, name: str, operator: str) -> int:
        name = str(name).strip()
        if not name:
            raise ValueError("单位名称不能为空")
        if self.get_unit_by_name(name):
            raise ValueError(f"单位「{name}」已存在")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO units(unit_uuid, name, sort_order) VALUES(?,?, COALESCE((SELECT MAX(sort_order)+1 FROM units),0))",
                (str(uuid.uuid4()), name),
            )
            uid = cur.lastrowid
            self._log_in_transaction(operator, "新建单位", name)
        # 附件目录用稳定 ID（unit_{id}），不随单位显示名变化（审查 F-05 修复）
        (self.root / ATTACH_DIR / self.unit_dir_name(uid)).mkdir(exist_ok=True)
        return uid

    def get_unit_by_name(self, name: str):
        return self._units.get_active_by_name(name)

    def rename_unit(self, unit_id: int, new_name: str, operator: str):
        old = self.get_unit(unit_id)
        if not old:
            raise KeyError(f"单位不存在: {unit_id}")
        new_name = str(new_name).strip()
        if not new_name:
            raise ValueError("单位名称不能为空")
        if new_name != old["name"] and self.get_unit_by_name(new_name):
            raise ValueError(f"单位「{new_name}」已存在")
        # 物理目录用稳定 ID，重命名不搬目录（审查 F-05 修复）
        with self._lock, self._conn:
            self._conn.execute("UPDATE units SET name=? WHERE id=?", (new_name, unit_id))
            self._log_in_transaction(operator, "重命名单位", f"{old['name']} → {new_name}")

    @staticmethod
    def _validate_full_order(ids, current_ids: list[int], label: str) -> list[int]:
        """校验拖放提交的是当前作用域的完整排列，拒绝静默丢项。"""
        ordered_ids = list(ids)
        if len(ordered_ids) != len(current_ids) or len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError(f"{label}排序必须包含当前全部对象且不能重复")
        if set(ordered_ids) != set(current_ids):
            raise ValueError(f"{label}排序包含不存在、已删除或不属于当前范围的对象")
        return ordered_ids

    def reorder_units(self, ordered_unit_ids, operator: str) -> bool:
        """按完整拖放顺序重排被审单位；不改变单位稳定 ID 或附件目录。"""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, name FROM units WHERE deleted_at IS NULL ORDER BY sort_order, id"
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            ids = self._validate_full_order(ordered_unit_ids, current_ids, "被审单位")
            if ids == current_ids:
                return False
            names = {row["id"]: row["name"] for row in rows}
            self._conn.executemany(
                "UPDATE units SET sort_order=? WHERE id=?",
                [(index, unit_id) for index, unit_id in enumerate(ids)],
            )
            summary = "、".join(names[unit_id] for unit_id in ids[:5])
            more = "" if len(ids) <= 5 else f" 等 {len(ids)} 个"
            self._log_in_transaction(operator, "调整单位排序", f"{summary}{more}")
        return True

    def cross_unit_refs(self, unit_id: int) -> list[dict]:
        """查本单位附件被哪些其他单位底稿引用（审查 F-01 修复）。

        返回 [{file_id, file_name, ref_unit_id, ref_unit_name, ref_issue_id, ref_seq}, ...]
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT f.id AS file_id, f.orig_name AS file_name,
                   u2.id AS ref_unit_id, u2.name AS ref_unit_name,
                   i.id AS ref_issue_id, i.seq AS ref_seq
            FROM files f
            JOIN issue_files l ON l.file_id = f.id
            JOIN issues i ON i.id = l.issue_id
            JOIN units u2 ON u2.id = i.unit_id
            WHERE f.unit_id = ? AND i.unit_id != ?
            ORDER BY u2.sort_order, i.seq, f.orig_name
            """,
            (unit_id, unit_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_unit(self, unit_id: int, operator: str):
        """将单位移入回收站，保留其全部底稿、版本和附件以便恢复。"""
        unit = self.get_unit(unit_id)
        if not unit:
            raise KeyError(f"单位不存在: {unit_id}")
        # 跨单位引用保护（审查 F-01 修复）：本单位附件被其他单位底稿引用时禁止删除，
        # 避免审计证据在无关的单位删除中丢失
        refs = self.cross_unit_refs(unit_id)
        if refs:
            sample = "、".join(
                f"「{r['file_name']}」({r['ref_unit_name']} 问题{r['ref_seq']})"
                for r in refs[:3]
            )
            more = f" 等 {len(refs)} 处引用" if len(refs) > 3 else ""
            raise ValueError(
                f"单位「{unit['name']}」的附件正被其他单位底稿引用（{sample}{more}），"
                "请先在对应底稿中解除关联或转移附件后再删除"
            )
        n_issues = self._conn.execute(
            "SELECT COUNT(*) FROM issues WHERE unit_id=? AND deleted_at IS NULL", (unit_id,)
        ).fetchone()[0]
        n_files = self._conn.execute(
            "SELECT COUNT(*) FROM files WHERE unit_id=? AND deleted_at IS NULL", (unit_id,)
        ).fetchone()[0]
        deleted_at = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE units SET deleted_at=?, deleted_by=? WHERE id=?", (deleted_at, operator, unit_id)
            )
            self._conn.execute(
                "INSERT INTO recycle_bin(recycle_uuid, entity_type, entity_id, entity_uuid, deleted_by, deleted_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), "unit", unit_id, str(unit.get("unit_uuid") or ""), operator, deleted_at),
            )
            self._log_in_transaction(
                operator, "移入回收站", f"单位 {unit['name']}（含 {n_issues} 条底稿、{n_files} 个附件）",
                "单位、底稿版本和附件均保留，需在回收站内明确物理删除",
            )

    def list_recycled_units(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT r.id recycle_id, r.deleted_at, r.deleted_by, u.id, u.unit_uuid, u.name, "
            "(SELECT COUNT(*) FROM issues i WHERE i.unit_id=u.id AND i.deleted_at IS NULL) issue_count, "
            "(SELECT COUNT(*) FROM files f WHERE f.unit_id=u.id AND f.deleted_at IS NULL) file_count "
            "FROM recycle_bin r JOIN units u ON u.id=r.entity_id "
            "WHERE r.entity_type='unit' AND r.restored_at IS NULL AND r.purged_at IS NULL "
            "AND u.deleted_at IS NOT NULL ORDER BY r.deleted_at DESC, r.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def restore_recycled_unit(self, recycle_id: int, operator: str) -> dict:
        record = self._conn.execute(
            "SELECT * FROM recycle_bin WHERE id=? AND entity_type='unit' AND restored_at IS NULL AND purged_at IS NULL",
            (recycle_id,),
        ).fetchone()
        if not record:
            raise KeyError("单位回收站条目不存在或已处理")
        unit = self.get_unit(record["entity_id"], include_deleted=True)
        if not unit or not unit.get("deleted_at"):
            raise KeyError("待恢复单位不存在")
        missing = []
        for file in self._conn.execute(
            "SELECT id, orig_name, rel_path FROM files WHERE unit_id=? AND deleted_at IS NULL", (unit["id"],)
        ).fetchall():
            if not self.attachment_path(file["rel_path"]).exists():
                missing.append(str(file["orig_name"]))
        if missing:
            raise ValueError(f"单位附件不完整，不能恢复：{'、'.join(missing[:5])}")
        restored_name = unit["name"]
        if self.get_unit_by_name(restored_name):
            base = restored_name
            for index in range(1, 1000):
                candidate = f"{base}（恢复{index}）"
                if not self.get_unit_by_name(candidate):
                    restored_name = candidate
                    break
            else:
                raise ValueError("无法为恢复单位分配不重复名称")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE units SET name=?, deleted_at=NULL, deleted_by='' WHERE id=?", (restored_name, unit["id"])
            )
            self._conn.execute("UPDATE recycle_bin SET restored_at=? WHERE id=?", (now, recycle_id))
            detail = "恢复原名称" if restored_name == unit["name"] else f"原名称冲突，恢复为「{restored_name}」"
            self._log_in_transaction(operator, "从回收站恢复", f"单位 {restored_name}", detail)
        return self.get_unit(unit["id"])

    def purge_recycled_unit(self, recycle_id: int, operator: str) -> None:
        """仅在回收站内明确清空时，物理删除单位的数据库记录和附件目录。"""
        record = self._conn.execute(
            "SELECT * FROM recycle_bin WHERE id=? AND entity_type='unit' AND restored_at IS NULL AND purged_at IS NULL",
            (recycle_id,),
        ).fetchone()
        if not record:
            raise KeyError("单位回收站条目不存在或已处理")
        unit = self.get_unit(record["entity_id"], include_deleted=True)
        if not unit or not unit.get("deleted_at"):
            raise KeyError("待清空单位不存在")
        refs = self.cross_unit_refs(unit["id"])
        if refs:
            raise ValueError("单位附件仍被其他单位底稿引用，不能物理删除")
        attachment_dir = self.root / ATTACH_DIR / self.unit_dir_name(unit["id"])
        trash_dir = attachment_dir.with_name(f".{attachment_dir.name}.purge_{uuid.uuid4().hex}")
        if attachment_dir.exists():
            os.replace(attachment_dir, trash_dir)
        now = _now()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE recycle_bin SET purged_at=? WHERE entity_type='issue' "
                    "AND entity_id IN (SELECT id FROM issues WHERE unit_id=?) "
                    "AND restored_at IS NULL AND purged_at IS NULL",
                    (now, unit["id"]),
                )
                self._conn.execute(
                    "UPDATE recycle_bin SET purged_at=? WHERE entity_type='file' "
                    "AND entity_id IN (SELECT id FROM files WHERE unit_id=?) "
                    "AND restored_at IS NULL AND purged_at IS NULL",
                    (now, unit["id"]),
                )
                self._conn.execute("DELETE FROM issue_files WHERE issue_id IN (SELECT id FROM issues WHERE unit_id=?)", (unit["id"],))
                self._conn.execute("DELETE FROM issue_versions WHERE issue_id IN (SELECT id FROM issues WHERE unit_id=?)", (unit["id"],))
                self._conn.execute("DELETE FROM files WHERE unit_id=?", (unit["id"],))
                self._conn.execute("DELETE FROM issues WHERE unit_id=?", (unit["id"],))
                self._conn.execute("DELETE FROM units WHERE id=?", (unit["id"],))
                self._conn.execute("UPDATE recycle_bin SET purged_at=? WHERE id=?", (now, recycle_id))
                self._log_in_transaction(operator, "清空回收站", f"单位 {unit['name']}", "物理删除单位、底稿和附件")
        except Exception:
            if trash_dir.exists():
                os.replace(trash_dir, attachment_dir)
            raise
        shutil.rmtree(trash_dir, ignore_errors=True)

    def reset_all(self, operator: str):
        """清空项目全部业务数据并完全初始化（重置项目）。

        删除单位/底稿/版本快照/附件登记/关联/回收站/异步任务记录，并清空
        附件库与输出目录的物理文件；保留 meta（项目名、schema 版本、版块与
        分类预设）——预设是配置而非数据，重置后重录底稿仍可复用。

        ``audit_log`` 是永久留痕，绝不能随重置清空；业务表删除和“重置项目”
        日志在同一 SQLite 事务中提交，失败则两者均回滚。
        """
        with self._lock, self._conn:
            # 先取消在跑任务（健康检查/扫描有 cancel 检查点），让线程尽快退出，
            # 再清空任务表——运行中的线程 finish_job 时 job 已不存在会静默返回。
            self._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE status=?", (self.JOB_RUNNING,))
            # 顺序：关联表 → 子表 → 主表
            counts = {
                "units": self._conn.execute("SELECT COUNT(*) FROM units").fetchone()[0],
                "issues": self._conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0],
                "files": self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                "requests": self._conn.execute("SELECT COUNT(*) FROM project_requests").fetchone()[0],
                "recycled": self._conn.execute(
                    "SELECT COUNT(*) FROM recycle_bin WHERE restored_at IS NULL AND purged_at IS NULL"
                ).fetchone()[0],
            }
            self._conn.execute("DELETE FROM issue_files")
            self._conn.execute("DELETE FROM issue_drafts")
            self._conn.execute("DELETE FROM project_requests")
            self._conn.execute("DELETE FROM files")
            self._conn.execute("DELETE FROM issue_versions")
            self._conn.execute("DELETE FROM issues")
            self._conn.execute("DELETE FROM units")
            self._conn.execute("DELETE FROM recycle_bin")
            self._conn.execute("DELETE FROM jobs")
            self._log_in_transaction(
                operator,
                "重置项目",
                "清空全部业务数据",
                "单位 {units} 个、底稿 {issues} 条、附件 {files} 个、资料请求 {requests} 条、回收站条目 {recycled} 条；"
                "历史操作日志永久保留".format(**counts),
            )
        # 附件库与输出目录的物理文件清空（保留目录本身）
        for d in (self.root / ATTACH_DIR, self.root / OUT_DIR):
            if d.exists():
                for child in d.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)

    # ───────────────────────── 底稿 ─────────────────────────

    def list_issues(self, unit_id: int) -> list[dict]:
        """底稿列表，按单位内序号排序，附附件数。"""
        return self._issues.list_active_for_unit(unit_id)

    def list_issues_by_unit(self) -> dict[int, list[dict]]:
        """一次查询取得全项目底稿树，供 V3 单页双视图使用。

        不能让前端按每个单位重复调用 list_issues()，否则单位数量增加时会产生
        N+1 请求和 N 次 SQLite 查询。结果按单位显示顺序、底稿序号排序。
        """
        return self._issues.list_active_grouped_by_unit()

    def summary(self) -> dict:
        """项目汇总：以底稿明细为主体，附带中性的项目数据概览。

        保留原三维汇总字段以兼容导出和旧界面，同时返回 ``dashboard`` 作为轻量项目
        数据概览。概览只陈列已登记的底稿、附件和日志，不推断审计结论或流程待办。
        """
        # 三组 SQL 聚合替代“每单位再查底稿/附件”的 2N+1 查询。
        with self._lock:
            status_rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(i.status),''), ?) name, COUNT(*) count "
                "FROM issues i JOIN units u ON u.id=i.unit_id "
                "WHERE i.deleted_at IS NULL AND u.deleted_at IS NULL "
                "GROUP BY COALESCE(NULLIF(TRIM(i.status),''), ?)",
                (self.STATUS_DRAFT, self.STATUS_DRAFT),
            ).fetchall()
            dept_rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(i.department),''), '未分版块') name, COUNT(*) count "
                "FROM issues i JOIN units u ON u.id=i.unit_id "
                "WHERE i.deleted_at IS NULL AND u.deleted_at IS NULL "
                "GROUP BY COALESCE(NULLIF(TRIM(i.department),''), '未分版块')"
            ).fetchall()
            category_rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(i.category),''), '未分类') name, COUNT(*) count "
                "FROM issues i JOIN units u ON u.id=i.unit_id "
                "WHERE i.deleted_at IS NULL AND u.deleted_at IS NULL "
                "GROUP BY COALESCE(NULLIF(TRIM(i.category),''), '未分类')"
            ).fetchall()
            unit_rows = self._conn.execute(
                "SELECT u.id, u.name, COUNT(DISTINCT i.id) issues, COUNT(DISTINCT f.id) files "
                "FROM units u LEFT JOIN issues i ON i.unit_id=u.id AND i.deleted_at IS NULL "
                "LEFT JOIN files f ON f.unit_id=u.id AND f.deleted_at IS NULL "
                "WHERE u.deleted_at IS NULL "
                "GROUP BY u.id, u.name ORDER BY u.sort_order, u.id"
            ).fetchall()
            issue_rows = self._conn.execute(
                "SELECT i.id, i.seq, i.unit_id, u.name unit_name, i.department, i.defect_type, "
                "i.category, i.amount, i.amount_minor, i.currency, i.amount_unit, i.status, i.author, i.reviewer, "
                "COALESCE(file_counts.file_count, 0) file_count "
                "FROM issues i JOIN units u ON u.id=i.unit_id "
                "LEFT JOIN (SELECT issue_id, COUNT(*) AS file_count FROM issue_files GROUP BY issue_id) "
                "AS file_counts ON file_counts.issue_id=i.id "
                "WHERE i.deleted_at IS NULL AND u.deleted_at IS NULL "
                "ORDER BY u.sort_order, u.id, i.sort_order, i.id"
            ).fetchall()
            file_stats = self._conn.execute(
                """SELECT COUNT(*) files_total,
                          SUM(CASE WHEN EXISTS (
                              SELECT 1 FROM issue_files x JOIN issues i ON i.id=x.issue_id
                              WHERE x.file_id=f.id AND i.deleted_at IS NULL
                          ) THEN 1 ELSE 0 END) linked_files
                   FROM files f JOIN units u ON u.id=f.unit_id
                   WHERE f.deleted_at IS NULL AND u.deleted_at IS NULL"""
            ).fetchone()
            recent_rows = self._conn.execute(
                "SELECT operator, action, target, created_at FROM audit_log ORDER BY id DESC LIMIT 6"
            ).fetchall()
        by_status = {row["name"]: row["count"] for row in status_rows}
        by_dept = {row["name"]: row["count"] for row in dept_rows}
        by_category = {row["name"]: row["count"] for row in category_rows}
        by_unit = {row["name"]: {"issues": row["issues"], "files": row["files"]} for row in unit_rows}
        total = sum(by_status.values())
        issues = [dict(row) for row in issue_rows]
        empty_units = [row for row in unit_rows if not int(row["issues"] or 0)]
        files_total = int(file_stats["files_total"] or 0)
        linked_files = int(file_stats["linked_files"] or 0)
        return {
            "by_status": by_status,
            "by_dept": by_dept,
            "by_category": by_category,
            "by_unit": by_unit,
            "total": total,
            "issues": issues,
            "dashboard": {
                "overview": {
                    "units": len(unit_rows), "issues": total, "files": files_total,
                    "units_with_issues": len(unit_rows) - len(empty_units),
                    "departments": len(by_dept), "categories": len(by_category),
                },
                "evidence": {
                    "files_total": files_total, "linked_files": linked_files,
                    "unlinked_files": files_total - linked_files,
                    "issues_with_evidence": sum(1 for issue in issues if int(issue["file_count"] or 0) > 0),
                },
                "units": [
                    {
                        "id": row["id"], "name": row["name"], "issues": row["issues"], "files": row["files"],
                    }
                    for row in unit_rows
                ],
                "recent_activity": [dict(row) for row in recent_rows],
            },
        }

    def search(self, q: str) -> dict:
        """全局搜索：单位/底稿/附件按关键字模糊匹配，各类限 20 条。"""
        q = (q or "").strip()
        if not q:
            return {"units": [], "issues": [], "files": []}
        like = f"%{q}%"
        with self._lock:
            units = self._conn.execute(
                "SELECT id, name FROM units WHERE name LIKE ? ORDER BY sort_order, id LIMIT 20",
                (like,),
            ).fetchall()
            issues = self._conn.execute(
                "SELECT i.id, i.seq, i.unit_id, u.name unit_name, i.department, i.defect_type, "
                "i.category, i.amount, i.status FROM issues i JOIN units u ON u.id=i.unit_id "
                "WHERE i.deleted_at IS NULL AND (i.defect_type LIKE ? OR i.department LIKE ? OR i.defect_desc LIKE ? "
                "OR i.regulation_basis LIKE ? OR i.suggestion LIKE ? "
                ") ORDER BY i.id DESC LIMIT 20",
                (like, like, like, like, like),
            ).fetchall()
            files = self._conn.execute(
                "SELECT f.id, f.unit_id, u.name unit_name, f.orig_name, f.mime, f.exclusive_to, f.rel_path "
                "FROM files f JOIN units u ON u.id=f.unit_id "
                "WHERE f.orig_name LIKE ? ORDER BY f.id DESC LIMIT 20",
                (like,),
            ).fetchall()
        return {
            "units": [dict(r) for r in units],
            "issues": [dict(r) for r in issues],
            "files": [dict(r) for r in files],
        }

    def get_issue(self, issue_id: int, *, include_deleted: bool = False):
        return self._issues.get(issue_id, include_deleted=include_deleted)

    def _next_seq(self, unit_id: int) -> int:
        """取单位内最小可用正整数；删除后仅释放该号，不重排已有底稿。"""
        rows = self._conn.execute(
            "SELECT seq FROM issues WHERE unit_id=? AND deleted_at IS NULL ORDER BY seq", (unit_id,)
        ).fetchall()
        expected = 1
        for row in rows:
            value = int(row["seq"] or 0)
            if value == expected:
                expected += 1
            elif value > expected:
                break
        return expected

    def _normalize_amount_fields(self, fields: dict, *, require_structured: bool) -> dict:
        """规范金额：新结构化输入仅接受数字，固定两位小数，不隐式换算单位。

        历史 Excel/项目中的 ``120万`` 等自由文本仍可读取和迁移；只有调用方明确
        提交币种/单位时才启用严格校验，避免把旧项目误判为损坏数据。
        """
        raw = str(fields.get("amount", "") or "").strip()
        currency = str(fields.get("currency", "") or "").strip().upper()
        unit = str(fields.get("amount_unit", "") or "").strip()
        if not require_structured:
            return {"amount": raw, "amount_minor": fields.get("amount_minor"),
                    "currency": currency, "amount_unit": unit}
        if not raw:
            defaults = self.get_amount_settings()
            return {"amount": "", "amount_minor": None, "currency": currency or defaults["currency"],
                    "amount_unit": unit or defaults["amount_unit"]}
        try:
            value = Decimal(raw)
        except InvalidOperation as e:
            raise ValueError("问题金额必须是数字，最多保留两位小数") from e
        if not value.is_finite() or value.as_tuple().exponent < -2:
            raise ValueError("问题金额必须是数字，最多保留两位小数")
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if rounded != value:
            raise ValueError("问题金额最多保留两位小数")
        defaults = self.get_amount_settings()
        currency = currency or defaults["currency"]
        unit = unit or defaults["amount_unit"]
        if not _CURRENCY_RE.fullmatch(currency):
            raise ValueError("币种必须为 3 位 ISO 代码，例如 CNY、USD")
        if unit not in _AMOUNT_UNITS:
            raise ValueError(f"金额单位仅支持：{'、'.join(_AMOUNT_UNITS)}")
        return {"amount": f"{rounded:.2f}", "amount_minor": int(rounded * 100),
                "currency": currency, "amount_unit": unit}

    def add_issue(self, unit_id: int, operator: str, **fields) -> int:
        """新建底稿：插入 + 有业务内容时才创建初始版本 + 日志。"""
        if not self.get_unit(unit_id):
            raise KeyError(f"单位不存在: {unit_id}")
        data = {k: str(fields.get(k, "") or "") for k in _TEXT_ISSUE_FIELDS}
        self._normalize_rich_text_fields(data, fields)
        structured_amount = any(key in fields and fields[key] is not None for key in ("currency", "amount_unit", "amount_minor"))
        data.update(self._normalize_amount_fields(fields, require_structured=structured_amount))
        # 新建底稿必须从草稿开始，禁止调用方通过 POST/导入绕过状态机直接伪造已复核或已归档状态。
        data["status"] = self.STATUS_DRAFT
        now = _now()
        with self._lock, self._conn:
            # 序号计算与插入必须在同一把锁内，避免并发新建得到重复 seq。
            seq = self._next_seq(unit_id)
            issue_uuid = str(uuid.uuid4())
            issue_code = self.issue_no(seq)
            cur = self._conn.execute(
                f"INSERT INTO issues(issue_uuid, unit_id, seq, issue_code, sort_order, {', '.join(ISSUE_FIELDS)}, created_at, updated_at) "
                f"VALUES(?,?,?,?,?,{', '.join('?' * len(ISSUE_FIELDS))},?,?)",
                (issue_uuid, unit_id, seq, issue_code, seq, *[data[k] for k in ISSUE_FIELDS], now, now),
            )
            iid = cur.lastrowid
            # 空白底稿不产生“初稿”快照；第一次实际录入内容时由 update_issue 创建 v1。
            if self._has_meaningful_issue_content(data):
                self._conn.execute(
                    "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,1,?,?,?)",
                    (iid, json.dumps(data, ensure_ascii=False), operator, now),
                )
            self._log_in_transaction(
                operator, "新建底稿", self._issue_target(unit_id, seq, data), issue_uuid=issue_uuid,
            )
        return iid

    def duplicate_issue(self, issue_id: int, operator: str, target_unit_id: int | None = None) -> dict:
        """复制正文和元数据为新草稿，绝不复制证据、版本、状态或交流记录。"""
        source = self.get_issue(issue_id)
        if not source:
            raise KeyError("源底稿不存在")
        unit_id = int(target_unit_id or source["unit_id"])
        if not self.get_unit(unit_id):
            raise KeyError("目标被审计单位不存在")
        fields = {
            key: source.get(key, "")
            for key in ("department", "category", "defect_type", "defect_desc", "amount", "currency", "amount_unit", "regulation_basis", "suggestion", "author", "reviewer")
        }
        # 富文本功能临时下线，复制只使用纯文本字段，避免把停用功能的格式状态带入新稿。
        copied_id = self.add_issue(unit_id, operator, **fields)
        copied = self.get_issue(copied_id)
        assert copied is not None
        with self._lock, self._conn:
            self._log_in_transaction(
                operator,
                "复制底稿",
                self._issue_target(unit_id, copied["seq"], copied),
                f"来源：{self._issue_target(source['unit_id'], source['seq'], source)}；未复制附件、版本、状态和交流记录",
                issue_uuid=str(copied.get("issue_uuid") or ""),
            )
        return copied

    # ───────────────────────── 项目内底稿模板 ─────────────────────────

    @staticmethod
    def _normalize_template_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("模板名称不能为空")
        if len(normalized) > 100:
            raise ValueError("模板名称不能超过 100 个字符")
        return normalized

    @staticmethod
    def _template_snapshot(issue: dict) -> dict:
        """模板只复用通用编制内容，绝不夹带人员、状态、证据或内部实体标识。"""
        return {field: issue.get(field, "") for field in TEMPLATE_FIELDS}

    @staticmethod
    def _template_record(row: sqlite3.Row) -> dict:
        data = json.loads(str(row["snapshot"] or "{}"))
        if not isinstance(data, dict):
            raise TypeError("底稿模板数据损坏，请从可信备份恢复")
        return {
            "id": int(row["id"]),
            "template_uuid": str(row["template_uuid"]),
            "name": str(row["name"]),
            "data": {field: str(data.get(field, "") or "") for field in TEMPLATE_FIELDS},
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "updated_by": str(row["updated_by"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_workpaper_templates(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM workpaper_templates ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [self._template_record(row) for row in rows]

    def create_workpaper_template(self, name: str, source_issue_id: int, operator: str) -> dict:
        name = self._normalize_template_name(name)
        source = self.get_issue(source_issue_id)
        if not source:
            raise KeyError("源底稿不存在")
        now = _now()
        snapshot = json.dumps(self._template_snapshot(source), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO workpaper_templates(template_uuid,name,snapshot,created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), name, snapshot, operator, now, operator, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("模板名称已存在，请换一个名称") from exc
            row = self._conn.execute("SELECT * FROM workpaper_templates WHERE id=?", (cur.lastrowid,)).fetchone()
            assert row is not None
            self._log_in_transaction(
                operator, "保存底稿模板", f"底稿模板：{name}",
                f"来源：{self._issue_target(int(source['unit_id']), int(source['seq']), source)}；不包含人员、状态、附件、版本和交流记录",
                issue_uuid=str(source.get("issue_uuid") or ""),
            )
        return self._template_record(row)

    def create_issue_from_template(self, template_id: int, unit_id: int, operator: str) -> dict:
        if not self.get_unit(unit_id):
            raise KeyError("目标被审计单位不存在")
        row = self._conn.execute("SELECT * FROM workpaper_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise KeyError("底稿模板不存在")
        template = self._template_record(row)
        # 人员信息由实际创建人重新开始，避免旧项目组成员被带入新的责任链。
        issue_id = self.add_issue(unit_id, operator, **template["data"], author=operator, reviewer="")
        issue = self.get_issue(issue_id)
        assert issue is not None
        with self._lock, self._conn:
            self._log_in_transaction(
                operator, "按模板新建底稿", self._issue_target(unit_id, int(issue["seq"]), issue),
                f"模板：{template['name']}；未复制附件、状态、历史和交流记录",
                issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return issue

    def delete_workpaper_template(self, template_id: int, operator: str) -> None:
        row = self._conn.execute("SELECT * FROM workpaper_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise KeyError("底稿模板不存在")
        template = self._template_record(row)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM workpaper_templates WHERE id=?", (template_id,))
            self._log_in_transaction(operator, "删除底稿模板", f"底稿模板：{template['name']}")

    # ───────────────────────── 项目级资料请求 ─────────────────────────

    @staticmethod
    def _request_due_date(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed.isoformat()
        except ValueError as exc:
            raise ValueError("截止日格式应为 YYYY-MM-DD") from exc

    def list_project_requests(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT r.*, u.name AS unit_name, i.seq AS issue_seq, i.defect_type AS issue_type,
                      f.orig_name AS provided_file_name
               FROM project_requests r
               LEFT JOIN units u ON u.id=r.unit_id
               LEFT JOIN issues i ON i.id=r.issue_id
               LEFT JOIN files f ON f.id=r.provided_file_id
               ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'provided' THEN 1 WHEN 'verified' THEN 2 ELSE 3 END,
                        CASE WHEN r.due_date='' THEN 1 ELSE 0 END, r.due_date, r.created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def create_project_request(self, operator: str, *, title: str, detail: str = "", responsible: str = "", due_date: str = "", unit_id: int | None = None, issue_id: int | None = None) -> dict:
        title = str(title or "").strip()
        if not title:
            raise ValueError("资料请求事项不能为空")
        if len(title) > 300 or len(str(detail or "")) > 10000:
            raise ValueError("资料请求内容过长")
        unit_id = int(unit_id) if unit_id else None
        issue_id = int(issue_id) if issue_id else None
        if unit_id and not self.get_unit(unit_id):
            raise KeyError("被审计单位不存在")
        if issue_id:
            issue = self.get_issue(issue_id)
            if not issue:
                raise KeyError("关联底稿不存在")
            if unit_id and int(issue["unit_id"]) != unit_id:
                raise ValueError("关联底稿不属于所选被审计单位")
            unit_id = int(issue["unit_id"])
        now, request_uuid = _now(), str(uuid.uuid4())
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO project_requests(request_uuid,unit_id,issue_id,title,detail,responsible,due_date,created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (request_uuid,unit_id,issue_id,title,str(detail or "").strip(),str(responsible or "").strip(),self._request_due_date(due_date),operator,now,operator,now))
            self._log_in_transaction(operator, "新建资料请求", title, f"责任人：{responsible or '未指定'}；截止日：{due_date or '未指定'}")
        return next(item for item in self.list_project_requests() if item["request_uuid"] == request_uuid)

    def update_project_request(self, request_uuid: str, operator: str, *, status: str | None = None, note: str | None = None, provided_file_id: int | None = None) -> dict:
        row = self._conn.execute("SELECT * FROM project_requests WHERE request_uuid=?", (request_uuid,)).fetchone()
        if not row:
            raise KeyError("资料请求不存在")
        updates: dict[str, object] = {}
        if status is not None:
            if status not in {"open", "provided", "verified", "withdrawn"}:
                raise ValueError("资料请求状态无效")
            transitions = {
                "open": {"provided", "withdrawn"},
                "provided": {"open", "verified", "withdrawn"},
                "verified": {"open"},
                "withdrawn": {"open"},
            }
            current_status = str(row["status"])
            if status != current_status and status not in transitions[current_status]:
                raise ValueError(f"资料请求不能从 {current_status} 直接变更为 {status}")
            updates["status"] = status
        if note is not None:
            updates["note"] = str(note).strip()
        if provided_file_id is not None:
            file = self.get_file(provided_file_id)
            if not file:
                raise KeyError("关联附件不存在")
            if row["unit_id"] and int(file["unit_id"]) != int(row["unit_id"]):
                raise ValueError("关联附件不属于该被审计单位")
            updates["provided_file_id"] = provided_file_id
        next_status = str(updates.get("status") or row["status"])
        next_file_id = updates.get("provided_file_id", row["provided_file_id"])
        if next_status in {"provided", "verified"} and row["unit_id"] and not next_file_id:
            raise ValueError("标记已提供或已核验前，请关联该单位的已提供附件")
        if updates:
            updates.update(updated_by=operator, updated_at=_now())
            with self._lock, self._conn:
                self._conn.execute("UPDATE project_requests SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE request_uuid=?", (*updates.values(), request_uuid))
                self._log_in_transaction(operator, "更新资料请求", str(row["title"]), "；".join(f"{key}={value}" for key, value in updates.items() if key not in {"updated_by", "updated_at"}))
        return next(item for item in self.list_project_requests() if item["request_uuid"] == request_uuid)

    def reorder_issues(self, unit_id: int, ordered_issue_ids, operator: str) -> bool:
        """按完整拖放顺序重排一个单位下的底稿，不改动底稿编号 ``seq``。"""
        with self._lock, self._conn:
            unit = self.get_unit(unit_id)
            if not unit:
                raise KeyError(f"单位不存在: {unit_id}")
            rows = self._conn.execute(
                "SELECT id, seq FROM issues WHERE unit_id=? AND deleted_at IS NULL ORDER BY sort_order, id",
                (unit_id,),
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            ids = self._validate_full_order(ordered_issue_ids, current_ids, "底稿")
            if ids == current_ids:
                return False
            self._conn.executemany(
                "UPDATE issues SET sort_order=? WHERE id=? AND unit_id=? AND deleted_at IS NULL",
                [(index, issue_id, unit_id) for index, issue_id in enumerate(ids)],
            )
            self._log_in_transaction(operator, "调整底稿排序", f"{unit['name']}：{len(ids)} 条底稿")
        return True

    # ───────────────────────── 独立草稿层 ─────────────────────────

    @staticmethod
    def _draft_payload(payload: dict) -> dict[str, str | int | None]:
        """限制草稿字段，绝不接受状态、编号、单位或附件归属的客户端覆盖。"""
        allowed = set(ISSUE_FIELDS) - {"status"}
        result: dict[str, str | int | None] = {}
        for key, value in (payload or {}).items():
            if key not in allowed or value is None:
                continue
            result[str(key)] = int(value) if key == "amount_minor" else str(value)
        return result

    def _issue_draft_baseline(self, issue_id: int) -> tuple[dict, int]:
        issue = self.get_issue(issue_id)
        if not issue:
            raise KeyError(f"底稿不存在: {issue_id}")
        version_id = int(self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM issue_versions WHERE issue_id=?", (issue_id,)
        ).fetchone()[0])
        return issue, version_id

    def get_issue_draft(self, issue_id: int) -> dict | None:
        """读取独立草稿及其与当前正式底稿基线是否一致的判断。"""
        issue, current_version_id = self._issue_draft_baseline(issue_id)
        row = self._conn.execute(
            "SELECT * FROM issue_drafts WHERE issue_id=?", (issue_id,)
        ).fetchone()
        if not row:
            return None
        draft = dict(row)
        try:
            draft["payload"] = json.loads(draft["payload"] or "{}")
        except json.JSONDecodeError:
            # 保存表被人为损坏时绝不返回半解析内容覆盖用户的正式底稿。
            raise ValueError("草稿内容损坏，未自动恢复；请从项目备份或正式版本处理") from None
        draft["current_version_id"] = current_version_id
        draft["current_updated_at"] = str(issue.get("updated_at") or "")
        draft["conflicted"] = (
            int(draft["base_version_id"] or 0) != current_version_id
            or str(draft["base_updated_at"] or "") != str(issue.get("updated_at") or "")
        )
        return draft

    def get_issue_draft_state(self, issue_id: int) -> dict:
        """返回草稿（可为空）以及创建下一次草稿所需的正式基线。"""
        issue, current_version_id = self._issue_draft_baseline(issue_id)
        draft = self.get_issue_draft(issue_id)
        return {
            "draft": draft,
            "current_version_id": current_version_id,
            "current_updated_at": str(issue.get("updated_at") or ""),
        }

    def save_issue_draft(
        self, issue_id: int, payload: dict, base_version_id: int, base_updated_at: str, operator: str,
    ) -> dict:
        """原子保存异常恢复草稿；正式底稿或版本绝不在这里写入。"""
        issue, current_version_id = self._issue_draft_baseline(issue_id)
        if int(base_version_id) != current_version_id or str(base_updated_at or "") != str(issue.get("updated_at") or ""):
            raise ConflictError("正式底稿已更新，草稿未保存；请重新读取后决定恢复或放弃")
        normalized = self._draft_payload(payload)
        if not normalized:
            raise ValueError("草稿至少应包含一个可编辑字段")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO issue_drafts(issue_id, issue_uuid, base_version_id, base_updated_at, payload, saved_by, saved_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(issue_id) DO UPDATE SET
                    issue_uuid=excluded.issue_uuid,
                    base_version_id=excluded.base_version_id,
                    base_updated_at=excluded.base_updated_at,
                    payload=excluded.payload,
                    saved_by=excluded.saved_by,
                    saved_at=excluded.saved_at
                """,
                (
                    issue_id, str(issue.get("issue_uuid") or ""), current_version_id,
                    str(issue.get("updated_at") or ""), json.dumps(normalized, ensure_ascii=False), operator, now,
                ),
            )
        return self.get_issue_draft_state(issue_id)

    def discard_issue_draft(self, issue_id: int) -> bool:
        """仅删除独立草稿；不改正式正文、版本、状态或审计日志。"""
        self._issue_draft_baseline(issue_id)
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM issue_drafts WHERE issue_id=?", (issue_id,)).rowcount > 0

    # ───────────────────────── 内部复核意见（不可变事件） ─────────────────────────

    def list_review_notes(self, issue_id: int) -> list[dict]:
        """按意见聚合不可变事件，并显示其是否仍锚定当前正式版本。"""
        issue, current_version_id = self._issue_draft_baseline(issue_id)
        rows = self._conn.execute(
            "SELECT * FROM review_note_events WHERE issue_id=? ORDER BY note_uuid, event_seq", (issue_id,)
        ).fetchall()
        notes: dict[str, dict] = {}
        for row in rows:
            event = dict(row)
            note_uuid = str(event["note_uuid"])
            note = notes.get(note_uuid)
            if note is None:
                note = {
                    "note_uuid": note_uuid,
                    "issue_id": issue_id,
                    "issue_uuid": str(issue.get("issue_uuid") or ""),
                    "base_version_id": int(event["base_version_id"] or 0),
                    "anchor_field": str(event["anchor_field"] or ""),
                    "created_by": str(event["created_by"]),
                    "created_at": str(event["created_at"]),
                    "body": str(event["body"]),
                    "events": [],
                }
                notes[note_uuid] = note
            note["events"].append(event)
        result = list(notes.values())
        for note in result:
            note["status"] = note_state(event["event_type"] for event in note["events"])
            note["is_stale"] = int(note["base_version_id"]) != current_version_id
        return result

    def _review_note_events(self, note_uuid: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM review_note_events WHERE note_uuid=? ORDER BY event_seq", (note_uuid,)
        ).fetchall()
        if not rows:
            raise KeyError("复核意见不存在")
        return [dict(row) for row in rows]

    def create_review_note(
        self, issue_id: int, body: str, anchor_field: str, base_version_id: int, operator: str,
    ) -> dict:
        """提出一条复核意见，必须锚定当前正式版本，避免误评旧正文。"""
        issue, current_version_id = self._issue_draft_baseline(issue_id)
        if int(base_version_id) != current_version_id:
            raise ConflictError("正式底稿版本已变化，请刷新后再提出复核意见")
        validate_review_event("open", EVENT_CREATED, body)
        note_uuid, event_uuid, now = str(uuid.uuid4()), str(uuid.uuid4()), _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO review_note_events(
                    event_uuid, note_uuid, issue_id, issue_uuid, base_version_id, anchor_field,
                    event_seq, event_type, body, created_by, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_uuid, note_uuid, issue_id, str(issue.get("issue_uuid") or ""), current_version_id,
                    str(anchor_field or "").strip(), 1, EVENT_CREATED, str(body).strip(), operator, now,
                ),
            )
            self._log_in_transaction(
                operator, "提出复核意见", self._issue_target(issue["unit_id"], issue["seq"], issue),
                f"意见：{note_uuid}；锚定版本：{current_version_id}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return next(note for note in self.list_review_notes(issue_id) if note["note_uuid"] == note_uuid)

    def append_review_note_event(self, note_uuid: str, event_type: str, body: str, operator: str) -> dict:
        """回复、清除或重开意见，只追加事件，不覆盖任何既有意见。"""
        events = self._review_note_events(note_uuid)
        first = events[0]
        current_state = note_state(event["event_type"] for event in events)
        validate_review_event(current_state, event_type, body)
        issue, _current_version_id = self._issue_draft_baseline(int(first["issue_id"]))
        event_uuid, now = str(uuid.uuid4()), _now()
        with self._lock, self._conn:
            event_seq = int(self._conn.execute(
                "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM review_note_events WHERE note_uuid=?", (note_uuid,)
            ).fetchone()[0])
            self._conn.execute(
                """
                INSERT INTO review_note_events(
                    event_uuid, note_uuid, issue_id, issue_uuid, base_version_id, anchor_field,
                    event_seq, event_type, body, created_by, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_uuid, note_uuid, int(first["issue_id"]), str(first["issue_uuid"]),
                    int(first["base_version_id"] or 0), str(first["anchor_field"] or ""),
                    event_seq, event_type, str(body or "").strip(), operator, now,
                ),
            )
            labels = {
                EVENT_REPLIED: "回复复核意见",
                EVENT_RESOLVED: "清除复核意见",
                EVENT_REOPENED: "重开复核意见",
            }
            self._log_in_transaction(
                operator, labels[event_type], self._issue_target(issue["unit_id"], issue["seq"], issue),
                f"意见：{note_uuid}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return next(note for note in self.list_review_notes(int(first["issue_id"])) if note["note_uuid"] == note_uuid)

    @staticmethod
    def _has_meaningful_issue_content(data: dict) -> bool:
        """仅业务内容都为空的底稿不生成版本，避免无意义的空初稿。"""
        fields = ("department", "category", "defect_type", "defect_desc", "amount", "regulation_basis", "suggestion")
        return any(str(data.get(field) or "").strip() for field in fields)

    def update_issue(self, issue_id: int, operator: str, **fields) -> bool:
        """更新底稿：只更新显式提交的字段，未提交字段保持原值（审查 F-02 修复）。

        内容有变化才留版本快照 + 日志。无变化返回 False。
        版本快照保存全字段（旧值 + 本次提交的合并结果），保证历史可完整回溯。
        """
        old = self.get_issue(issue_id)
        if not old:
            raise KeyError(f"底稿不存在: {issue_id}")
        # 状态只走 change_status 接口，update_issue 不接受 status 字段（防止绕过状态机）
        # 已归档底稿不可直接编辑（DESIGN.md 1.4）：只能走归档后编辑（自动开新版本+原因）
        if (old.get("status") or self.STATUS_DRAFT) == self.STATUS_ARCHIVED:
            raise ValueError("已归档底稿不能直接修改，请使用『归档后编辑』（自动开新版本并填写修改原因）")
        # 只接受白名单字段，且仅写显式传入的（不把未提交字段当空串写回）；status 除外
        updates = {k: str(v or "") for k, v in fields.items()
                   if k in _TEXT_ISSUE_FIELDS and k != "status" and v is not None}
        self._normalize_rich_text_fields(updates, fields)
        structured_amount = any(key in fields and fields[key] is not None for key in ("currency", "amount_unit", "amount_minor"))
        if structured_amount:
            updates.update(self._normalize_amount_fields(fields, require_structured=True))
        changed = [
            k for k, value in updates.items()
            if value != (old.get(k) if k == "amount_minor" else str(old.get(k) or ""))
        ]
        if not changed:
            return False
        # 已复核被编辑 → 自动降回编制完成（DESIGN.md 1.3：避免"改了但显示已复核"假象）
        if (old.get("status") or self.STATUS_DRAFT) == self.STATUS_REVIEWED:
            updates["status"] = self.STATUS_SUBMITTED
            if "status" not in changed:
                changed.append("status")
        now = _now()
        # 快照 = 旧全字段 + 本次更新合并（未提交字段保留旧值）
        data = {k: old[k] for k in ISSUE_FIELDS}
        data.update(updates)
        with self._lock, self._conn:
            vno = self._conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM issue_versions WHERE issue_id=?", (issue_id,)
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,?,?,?,?)",
                (issue_id, vno, json.dumps(data, ensure_ascii=False), operator, now),
            )
            sets = ", ".join(f"{k}=?" for k in updates)
            self._conn.execute(
                f"UPDATE issues SET {sets}, updated_at=? WHERE id=?",
                (*[updates[k] for k in updates], now, issue_id),
            )
            self._log_in_transaction(
                operator, "修改底稿", self._issue_target(old["unit_id"], old["seq"], data),
                f"修改字段：{'、'.join(changed)}", issue_uuid=str(old.get("issue_uuid") or ""),
            )
        return True

    _BATCH_METADATA_FIELDS: ClassVar[set[str]] = {"department", "category", "author", "reviewer"}

    def preflight_batch_issue_metadata(self, issue_ids: list[int], changes: dict) -> dict:
        """只读核对批量元数据修改范围；拒绝归档、删除或跨项目的底稿。"""
        ids = [int(issue_id) for issue_id in issue_ids]
        if not ids or len(ids) > 500 or len(set(ids)) != len(ids):
            raise ValueError("请一次选择 1 至 500 条且不重复的底稿")
        normalized = {
            str(key): str(value or "").strip()
            for key, value in (changes or {}).items()
            if str(key) in self._BATCH_METADATA_FIELDS
        }
        if not normalized or len(normalized) != len(changes or {}):
            raise ValueError("批量维护仅支持所属版块、问题分类、编制人和复核人")
        if any(len(value) > 200 for value in normalized.values()):
            raise ValueError("批量维护的字段内容不得超过 200 字")
        marks = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT id, unit_id, seq, issue_uuid, defect_type, status, updated_at, department, category, author, reviewer "
            f"FROM issues WHERE id IN ({marks}) AND deleted_at IS NULL", ids,
        ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        missing = [issue_id for issue_id in ids if issue_id not in by_id]
        if missing:
            raise KeyError(f"底稿不存在或已删除：{missing[0]}")
        archived = [issue_id for issue_id in ids if by_id[issue_id]["status"] == self.STATUS_ARCHIVED]
        if archived:
            raise ValueError(f"所选底稿含已归档记录，不能批量直接修改：{archived[0]}")
        changed_ids = [
            issue_id for issue_id in ids
            if any(normalized[field] != str(by_id[issue_id].get(field) or "") for field in normalized)
        ]
        reviewed = [issue_id for issue_id in changed_ids if by_id[issue_id]["status"] == self.STATUS_REVIEWED]
        fingerprint = "|".join(
            f"{issue_id}:{by_id[issue_id]['updated_at']}:{by_id[issue_id]['status']}" for issue_id in ids
        )
        return {
            "issue_ids": ids, "changes": normalized, "selected": len(ids), "affected": len(changed_ids),
            "unchanged": len(ids) - len(changed_ids), "reviewed": len(reviewed), "fingerprint": fingerprint,
            "issues": [
                {"id": issue_id, "unit_id": by_id[issue_id]["unit_id"], "seq": by_id[issue_id]["seq"],
                 "defect_type": by_id[issue_id]["defect_type"], "status": by_id[issue_id]["status"]}
                for issue_id in ids
            ],
        }

    def batch_update_issue_metadata(self, issue_ids: list[int], changes: dict, operator: str) -> dict:
        """事务内批量维护白名单元数据，并为每条发生变化的底稿写入版本快照。"""
        preflight = self.preflight_batch_issue_metadata(issue_ids, changes)
        ids, normalized = preflight["issue_ids"], preflight["changes"]
        if not preflight["affected"]:
            return {"updated": 0, "unchanged": preflight["unchanged"], "issue_ids": []}
        marks = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT * FROM issues WHERE id IN ({marks}) AND deleted_at IS NULL", ids,
        ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        now, updated_ids = _now(), []
        with self._lock, self._conn:
            for issue_id in ids:
                old = by_id[issue_id]
                updates = {field: value for field, value in normalized.items() if value != str(old.get(field) or "")}
                if not updates:
                    continue
                if old.get("status") == self.STATUS_REVIEWED:
                    updates["status"] = self.STATUS_SUBMITTED
                data = {key: old[key] for key in ISSUE_FIELDS}
                data.update(updates)
                version_no = self._conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 FROM issue_versions WHERE issue_id=?", (issue_id,)
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,?,?,?,?)",
                    (issue_id, version_no, json.dumps(data, ensure_ascii=False), operator, now),
                )
                assignments = ", ".join(f"{field}=?" for field in updates)
                self._conn.execute(
                    f"UPDATE issues SET {assignments}, updated_at=? WHERE id=?",
                    (*updates.values(), now, issue_id),
                )
                updated_ids.append(issue_id)
            self._log_in_transaction(
                operator, "批量维护底稿元数据", f"{len(updated_ids)} 条底稿",
                f"字段：{'、'.join(normalized)}；底稿 ID：{','.join(str(issue_id) for issue_id in updated_ids)}",
            )
        return {"updated": len(updated_ids), "unchanged": preflight["unchanged"], "issue_ids": updated_ids}

    @staticmethod
    def _normalize_rich_text_fields(data: dict, fields: dict) -> None:
        """规范富文本，并同步更新其纯文本投影。

        前端只要提交 ``*_rich``，富文本视图就是唯一来源；旧版调用方仅提交纯文本
        时清空对应富文本，避免陈旧排版覆盖新的普通文本修改。
        """
        for rich_field, plain_field in _RICH_TEXT_FIELD_MAP.items():
            if rich_field in fields and fields[rich_field] is not None:
                rich_value = sanitize_rich_html(str(fields[rich_field] or ""))
                data[rich_field] = rich_value
                data[plain_field] = rich_html_to_plain_text(rich_value)
            elif plain_field in fields and fields[plain_field] is not None:
                data[rich_field] = ""

    def delete_issue(self, issue_id: int, operator: str):
        """移入回收站而非物理删除；附件、关联和版本均随底稿保留以便恢复。"""
        old = self.get_issue(issue_id)
        if not old:
            raise KeyError(f"底稿不存在: {issue_id}")
        unit = self.get_unit(old["unit_id"])
        unit_name = unit["name"] if unit else f"单位{old['unit_id']}"
        deleted_at = _now()
        with self._lock, self._conn:
            # 独占附件回到资料库，避免因底稿进入回收站而成为不可访问对象；
            # issue_files 关联本身仍保留，恢复底稿后证据关联可继续追溯。
            self._conn.execute("UPDATE files SET exclusive_to=NULL WHERE exclusive_to=?", (issue_id,))
            self._conn.execute(
                "UPDATE issues SET deleted_at=?, deleted_by=?, updated_at=? WHERE id=?",
                (deleted_at, operator, deleted_at, issue_id),
            )
            self._conn.execute(
                "INSERT INTO recycle_bin(recycle_uuid, entity_type, entity_id, entity_uuid, deleted_by, deleted_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), "issue", issue_id, str(old.get("issue_uuid") or ""), operator, deleted_at),
            )
            self._log_in_transaction(
                operator, "移入回收站", f"{unit_name} · 问题{old['seq']}.{old['defect_type']}",
                issue_uuid=str(old.get("issue_uuid") or ""),
            )

    def list_recycled_issues(self) -> list[dict]:
        """返回尚未恢复或清空的底稿；回收站默认永不自动清空。"""
        rows = self._conn.execute(
            "SELECT r.id recycle_id, r.deleted_at, r.deleted_by, i.id, i.issue_uuid, i.unit_id, i.seq, "
            "i.department, i.defect_type, i.status, u.name unit_name "
            "FROM recycle_bin r JOIN issues i ON i.id=r.entity_id "
            "LEFT JOIN units u ON u.id=i.unit_id "
            "WHERE r.entity_type='issue' AND r.restored_at IS NULL AND r.purged_at IS NULL "
            "AND i.deleted_at IS NOT NULL ORDER BY r.deleted_at DESC, r.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recycled_issue_detail(self, recycle_id: int) -> dict:
        """回收站底稿只读预览：保留全文、版本数和附件清单，不允许借此编辑。"""
        row = self._conn.execute(
            "SELECT r.id recycle_id, r.deleted_at, r.deleted_by, i.*, u.name unit_name "
            "FROM recycle_bin r JOIN issues i ON i.id=r.entity_id "
            "LEFT JOIN units u ON u.id=i.unit_id "
            "WHERE r.id=? AND r.entity_type='issue' AND r.restored_at IS NULL AND r.purged_at IS NULL "
            "AND i.deleted_at IS NOT NULL",
            (recycle_id,),
        ).fetchone()
        if not row:
            raise KeyError("回收站条目不存在或已处理")
        data = dict(row)
        issue_id = int(data["id"])
        attachments = self._conn.execute(
            "SELECT f.id, f.orig_name, f.mime, f.size, f.sha256 "
            "FROM files f JOIN issue_files l ON l.file_id=f.id "
            "WHERE l.issue_id=? ORDER BY f.orig_name LIMIT 100",
            (issue_id,),
        ).fetchall()
        attachment_total = self._conn.execute(
            "SELECT COUNT(*) FROM issue_files WHERE issue_id=?", (issue_id,)
        ).fetchone()[0]
        version_count = self._conn.execute(
            "SELECT COUNT(*) FROM issue_versions WHERE issue_id=?", (issue_id,)
        ).fetchone()[0]
        issue = {key: value for key, value in data.items()
                 if key not in {"recycle_id", "deleted_at", "deleted_by", "unit_name"}}
        return {
            "recycle_id": data["recycle_id"],
            "deleted_at": data["deleted_at"],
            "deleted_by": data["deleted_by"],
            "unit_name": data.get("unit_name") or f"单位{issue['unit_id']}",
            "issue": issue,
            "version_count": version_count,
            "attachment_total": attachment_total,
            "attachments": [dict(item) for item in attachments],
            "attachments_truncated": attachment_total > len(attachments),
        }

    def restore_recycled_issue(self, recycle_id: int, operator: str) -> dict:
        """恢复底稿；原编号已被复用时自动分配当前最小可用号并留痕。"""
        record = self._conn.execute(
            "SELECT * FROM recycle_bin WHERE id=? AND entity_type='issue' AND restored_at IS NULL AND purged_at IS NULL",
            (recycle_id,),
        ).fetchone()
        if not record:
            raise KeyError("回收站条目不存在或已处理")
        issue = self.get_issue(record["entity_id"], include_deleted=True)
        if not issue or not issue.get("deleted_at"):
            raise KeyError("待恢复底稿不存在")
        unit = self.get_unit(issue["unit_id"])
        if not unit:
            raise ValueError("原单位已不存在，暂不能恢复该底稿")
        now = _now()
        with self._lock, self._conn:
            # 同一事务内判定和换号，避免并发恢复/新增时得到相同编号。
            wanted = int(issue["seq"])
            occupied = self._conn.execute(
                "SELECT 1 FROM issues WHERE unit_id=? AND seq=? AND deleted_at IS NULL LIMIT 1",
                (issue["unit_id"], wanted),
            ).fetchone() is not None
            restored_seq = self._next_seq(issue["unit_id"]) if occupied else wanted
            self._conn.execute(
                "UPDATE issues SET seq=?, deleted_at=NULL, deleted_by='', updated_at=? WHERE id=?",
                (restored_seq, now, issue["id"]),
            )
            self._conn.execute("UPDATE recycle_bin SET restored_at=? WHERE id=?", (now, recycle_id))
            detail = "恢复原编号" if restored_seq == wanted else f"原编号{wanted}已被复用，自动改为{restored_seq}"
            self._log_in_transaction(
                operator, "从回收站恢复", f"{unit['name']} · 问题{restored_seq}.{issue['defect_type']}", detail,
                issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_issue(issue["id"])

    def purge_recycled_issue(self, recycle_id: int, operator: str) -> None:
        """手动清空单条回收站记录：物理删数据，永久审计日志仍保留。"""
        record = self._conn.execute(
            "SELECT * FROM recycle_bin WHERE id=? AND entity_type='issue' AND restored_at IS NULL AND purged_at IS NULL",
            (recycle_id,),
        ).fetchone()
        if not record:
            raise KeyError("回收站条目不存在或已处理")
        issue = self.get_issue(record["entity_id"], include_deleted=True)
        if not issue or not issue.get("deleted_at"):
            raise KeyError("待清空底稿不存在")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute("UPDATE files SET exclusive_to=NULL WHERE exclusive_to=?", (issue["id"],))
            self._conn.execute("DELETE FROM issue_files WHERE issue_id=?", (issue["id"],))
            self._conn.execute("DELETE FROM issue_versions WHERE issue_id=?", (issue["id"],))
            self._conn.execute("DELETE FROM issues WHERE id=?", (issue["id"],))
            self._conn.execute("UPDATE recycle_bin SET purged_at=? WHERE id=?", (now, recycle_id))
            self._log_in_transaction(
                operator, "清空回收站", f"问题{issue['seq']}.{issue['defect_type']}", "物理删除底稿数据",
                issue_uuid=str(issue.get("issue_uuid") or ""),
            )

    def _issue_target(self, unit_id: int, seq: int, data: dict = None) -> str:
        unit = self.get_unit(unit_id)
        u = unit["name"] if unit else f"单位{unit_id}"
        t = (data or {}).get("defect_type", "")
        return f"{u} · 问题{seq}" + (f".{t}" if t else "")

    # ───────────────────────── 交流修订（P1-14） ─────────────────────────

    @staticmethod
    def _exchange_snapshot(issue: dict) -> dict:
        """只保存正式底稿字段，交流修订绝不携带数据库内部列。"""
        return {field: issue.get(field) for field in ISSUE_FIELDS}

    def _exchange_session_row(self, session_uuid: str):
        return self._exchanges.get_session(session_uuid)

    def _require_open_exchange(self, session_uuid: str):
        row = self._exchange_session_row(session_uuid)
        if not row:
            raise KeyError("交流会话不存在")
        if row["status"] != "open":
            raise ValueError("交流会话已结束，不能继续修改")
        if row["issue_id"] is None:
            raise ValueError("原底稿已清空，交流记录仅可查看，不能再修改")
        return row

    def start_exchange_session(self, issue_id: int, operator: str) -> dict:
        """开始或恢复同一底稿的未结束交流会话。

        基线来自进入交流时的正式底稿。本轮内保存会更新工作中的底稿内容并追加
        修订记录；只有结束本轮时才把本轮整体固化成一个正式版本。
        """
        issue = self.get_issue(issue_id)
        if not issue:
            raise KeyError("底稿不存在")
        existing_uuid = self._exchanges.find_open_session_for_issue_uuid(str(issue.get("issue_uuid") or ""))
        if existing_uuid:
            return self.get_exchange_session(existing_uuid)
        version = self._conn.execute(
            "SELECT id FROM issue_versions WHERE issue_id=? ORDER BY version_no DESC LIMIT 1", (issue_id,)
        ).fetchone()
        session_uuid = str(uuid.uuid4())
        now = _now()
        snapshot = self._exchange_snapshot(issue)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO exchange_sessions(session_uuid,issue_id,issue_uuid,base_version_id,base_snapshot,status,opened_by,opened_at) "
                "VALUES(?,?,?,?,?,'open',?,?)",
                (session_uuid, issue_id, str(issue.get("issue_uuid") or ""),
                 int(version["id"]) if version else None, json.dumps(snapshot, ensure_ascii=False), operator, now),
            )
            self._log_in_transaction(
                operator, "开始问题交流", self._issue_target(issue["unit_id"], issue["seq"], issue),
                f"交流会话：{session_uuid}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_exchange_session(session_uuid)

    def get_exchange_session(self, session_uuid: str) -> dict:
        row = self._exchange_session_row(session_uuid)
        if not row:
            raise KeyError("交流会话不存在")
        session = dict(row)
        try:
            session["base_snapshot"] = json.loads(session["base_snapshot"] or "{}")
        except json.JSONDecodeError:
            session["base_snapshot"] = {}
        issue = self.get_issue(int(session["issue_id"]), include_deleted=True) if session.get("issue_id") else None
        session["issue"] = issue
        session["files"] = self.files_for_issue(int(session["issue_id"])) if issue and not issue.get("deleted_at") else []
        # 审阅记录属于底稿而非单次会话。重新开始交流时必须继续返回该底稿全部
        # 历史修订、批注和待补资料，否则上轮版本虽存在，正文却无法还原修订痕迹。
        issue_uuid = str(session.get("issue_uuid") or "")
        if issue_uuid:
            history_where, history_key = "s.issue_uuid=?", issue_uuid
        elif session.get("issue_id"):
            history_where, history_key = "s.issue_id=?", int(session["issue_id"])
        else:
            history_where, history_key = "s.session_uuid=?", session_uuid
        revisions = self._conn.execute(
            "SELECT r.* FROM exchange_revisions r "
            "JOIN exchange_sessions s ON s.session_uuid=r.session_uuid "
            f"WHERE {history_where} ORDER BY r.proposed_at, r.rowid",
            (history_key,),
        ).fetchall()
        comments = self._conn.execute(
            "SELECT c.* FROM exchange_comments c "
            "JOIN exchange_sessions s ON s.session_uuid=c.session_uuid "
            f"WHERE {history_where} ORDER BY c.created_at, c.comment_uuid",
            (history_key,),
        ).fetchall()
        requests = self._conn.execute(
            "SELECT r.*, f.orig_name provided_file_name, f.sha256 provided_file_sha256 "
            "FROM exchange_requests r JOIN exchange_sessions s ON s.session_uuid=r.session_uuid "
            "LEFT JOIN files f ON f.id=r.provided_file_id "
            f"WHERE {history_where} ORDER BY r.created_at, r.request_uuid",
            (history_key,),
        ).fetchall()
        session["revisions"] = [dict(item) for item in revisions]
        session["comments"] = [dict(item) for item in comments]
        session["requests"] = [dict(item) for item in requests]
        # 交流轮次版本时间线（审查 F 修复）：只包含交流轮次固化生成的版本
        # （exchange_revisions.version_id 非空），普通编辑保存的版本不进入交流时间线。
        round_versions: list[dict] = []
        version_rows = self._conn.execute(
            "SELECT v.* FROM issue_versions v "
            "JOIN exchange_revisions r ON r.version_id=v.id "
            "JOIN exchange_sessions s ON s.session_uuid=r.session_uuid "
            f"WHERE {history_where} "
            "GROUP BY v.id ORDER BY v.version_no",
            (history_key,),
        ).fetchall()
        for row in version_rows:
            item = dict(row)
            try:
                item["snapshot"] = json.loads(item["snapshot"] or "{}")
            except json.JSONDecodeError:
                item["snapshot"] = {}
            round_versions.append(item)
        session["round_versions"] = round_versions
        return session

    def propose_exchange_revision(self, session_uuid: str, field_name: str, new_value: str,
                                  reason: str, operator: str) -> dict:
        """保存一条本轮交流修订，但不单独生成正式版本。

        交流不是审批队列：保存后的内容就是下一次修订的原文；右侧只保留
        可回溯的修改记录，而不要求“接受”或“应用”两个额外动作；由结束本轮
        一次性确认本轮全部修订并生成一个版本。
        """
        session = self._require_open_exchange(session_uuid)
        field = str(field_name or "").strip()
        if field not in EXCHANGE_REVISION_FIELDS:
            raise ValueError("该字段不支持在交流模式中修订")
        value = str(new_value or "")
        if len(value) > 20_000 or len(str(reason or "")) > 2_000:
            raise ValueError("修订内容或修改理由过长")
        issue = self.get_issue(int(session["issue_id"]))
        if not issue:
            raise KeyError("底稿不存在")
        if issue.get("status") == self.STATUS_ARCHIVED:
            raise ValueError("已归档底稿不能直接修订，请先执行归档后编辑")
        old_value = str(issue.get(field) or "")
        if value == old_value:
            raise ValueError("修订后的内容与当前底稿一致，无需新增修订")
        revision_uuid = str(uuid.uuid4())
        now = _now()
        data = self._exchange_snapshot(issue)
        if field == "amount":
            data.update(self._normalize_amount_fields({
                "amount": value, "currency": issue.get("currency"), "amount_unit": issue.get("amount_unit"),
            }, require_structured=True))
        else:
            data[field] = value
            self._normalize_rich_text_fields(data, {field: value})
        if issue.get("status") == self.STATUS_REVIEWED:
            data["status"] = self.STATUS_SUBMITTED
        with self._lock, self._conn:
            sets = ", ".join(f"{name}=?" for name in ISSUE_FIELDS)
            self._conn.execute(
                f"UPDATE issues SET {sets}, updated_at=? WHERE id=?",
                (*[data[name] for name in ISSUE_FIELDS], now, issue["id"]),
            )
            self._conn.execute(
                "INSERT INTO exchange_revisions(revision_uuid,session_uuid,version_id,field_name,old_value,new_value,reason,status,proposed_by,proposed_at) "
                "VALUES(?,?,?,?,?,?,?, 'accepted', ?,?)",
                (revision_uuid, session_uuid, None, field, old_value, value, str(reason or "").strip(), operator, now),
            )
            self._log_in_transaction(
                operator, "保存本轮交流修订", self._issue_target(issue["unit_id"], issue["seq"], data),
                f"字段：{field}；修订：{revision_uuid}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_exchange_session(session_uuid)

    def decide_exchange_revision(self, session_uuid: str, revision_uuid: str, decision: str,
                                 operator: str) -> dict:
        raise ValueError("修订保存时已自动写入正式底稿，无需接受或拒绝")

    def add_exchange_comment(self, session_uuid: str, body: str, anchor_field: str,
                             revision_uuid: str, operator: str) -> dict:
        session = self._require_open_exchange(session_uuid)
        content = str(body or "").strip()
        anchor = str(anchor_field or "").strip()
        if not content:
            raise ValueError("批注内容不能为空")
        if len(content) > 10_000 or (anchor and anchor not in EXCHANGE_REVISION_FIELDS):
            raise ValueError("批注内容或定位字段不合法")
        if revision_uuid and not self._conn.execute(
            "SELECT 1 FROM exchange_revisions WHERE revision_uuid=? AND session_uuid=?", (revision_uuid, session_uuid)
        ).fetchone():
            raise ValueError("批注关联的修订不存在")
        comment_uuid = str(uuid.uuid4())
        now = _now()
        issue = self.get_issue(int(session["issue_id"]))
        if not issue:
            raise KeyError("底稿已删除，交流记录仅可查看")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO exchange_comments(comment_uuid,session_uuid,revision_uuid,anchor_field,body,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (comment_uuid, session_uuid, revision_uuid or None, anchor, content, operator, now),
            )
            self._log_in_transaction(
                operator, "新增交流批注", self._issue_target(issue["unit_id"], issue["seq"], issue),
                f"批注：{comment_uuid}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_exchange_session(session_uuid)

    def create_exchange_request(self, session_uuid: str, content: str, operator: str) -> dict:
        session = self._require_open_exchange(session_uuid)
        text = str(content or "").strip()
        if not text or len(text) > 10_000:
            raise ValueError("待补资料内容不能为空且不得超过 10000 字")
        request_uuid = str(uuid.uuid4())
        now = _now()
        issue = self.get_issue(int(session["issue_id"]))
        if not issue:
            raise KeyError("底稿已删除，交流记录仅可查看")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO exchange_requests(request_uuid,session_uuid,content,created_by,created_at) VALUES(?,?,?,?,?)",
                (request_uuid, session_uuid, text, operator, now),
            )
            self._log_in_transaction(
                operator, "提出待补资料", self._issue_target(issue["unit_id"], issue["seq"], issue),
                f"资料请求：{request_uuid}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_exchange_session(session_uuid)

    def update_exchange_request(self, session_uuid: str, request_uuid: str, status: str,
                                provided_file_id: int | None, note: str, operator: str) -> dict:
        session = self._require_open_exchange(session_uuid)
        next_status = str(status or "").strip()
        if next_status not in {"open", "provided", "verified", "withdrawn"}:
            raise ValueError("待补资料状态不合法")
        request = self._conn.execute(
            "SELECT * FROM exchange_requests WHERE request_uuid=? AND session_uuid=?", (request_uuid, session_uuid)
        ).fetchone()
        if not request:
            raise KeyError("待补资料记录不存在")
        file_id = int(provided_file_id) if provided_file_id else None
        if next_status in {"provided", "verified"} and not file_id:
            raise ValueError("标记已提供或已核验时必须关联当前底稿附件")
        if file_id and not self._conn.execute(
            "SELECT 1 FROM issue_files WHERE issue_id=? AND file_id=?", (int(session["issue_id"]), file_id)
        ).fetchone():
            raise ValueError("补充资料必须是当前底稿已关联的附件")
        now = _now()
        issue = self.get_issue(int(session["issue_id"]))
        if not issue:
            raise KeyError("底稿已删除，交流记录仅可查看")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE exchange_requests SET status=?, provided_file_id=?, note=?, updated_by=?, updated_at=? WHERE request_uuid=?",
                (next_status, file_id, str(note or "").strip(), operator, now, request_uuid),
            )
            self._log_in_transaction(
                operator, "更新待补资料", self._issue_target(issue["unit_id"], issue["seq"], issue),
                f"资料请求：{request_uuid}；状态：{next_status}", issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_exchange_session(session_uuid)

    def apply_exchange_revisions(self, session_uuid: str, operator: str) -> dict:
        raise ValueError("修订保存时已自动写入正式底稿，无需应用本轮修订")

    def close_exchange_session(self, session_uuid: str, note: str, operator: str) -> dict:
        session = self._require_open_exchange(session_uuid)
        now = _now()
        issue = self.get_issue(int(session["issue_id"]))
        if not issue:
            raise KeyError("底稿不存在")
        pending = self._conn.execute(
            "SELECT field_name FROM exchange_revisions WHERE session_uuid=? AND version_id IS NULL ORDER BY proposed_at, rowid",
            (session_uuid,),
        ).fetchall()
        with self._lock, self._conn:
            version_id = None
            if pending:
                data = self._exchange_snapshot(issue)
                vno = self._conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 FROM issue_versions WHERE issue_id=?", (issue["id"],)
                ).fetchone()[0]
                version_cursor = self._conn.execute(
                    "INSERT INTO issue_versions(issue_id,version_no,snapshot,saved_by,created_at) VALUES(?,?,?,?,?)",
                    (issue["id"], vno, json.dumps(data, ensure_ascii=False), operator, now),
                )
                version_id = int(version_cursor.lastrowid)
                self._conn.execute(
                    "UPDATE exchange_revisions SET version_id=?, decided_by=?, decided_at=?, applied_by=?, applied_at=? "
                    "WHERE session_uuid=? AND version_id IS NULL",
                    (version_id, operator, now, operator, now, session_uuid),
                )
                self._conn.execute(
                    "UPDATE exchange_sessions SET base_version_id=?, base_snapshot=? WHERE session_uuid=?",
                    (version_id, json.dumps(data, ensure_ascii=False), session_uuid),
                )
            self._conn.execute(
                "UPDATE exchange_sessions SET status='closed', closed_by=?, closed_at=?, close_note=? WHERE session_uuid=?",
                (operator, now, str(note or "").strip(), session_uuid),
            )
            fields = "、".join(dict.fromkeys(str(row["field_name"]) for row in pending))
            detail = f"交流会话：{session_uuid}"
            if version_id:
                detail += f"；固化版本：{vno}；修订字段：{fields}"
            self._log_in_transaction(
                operator, "结束本轮交流", self._issue_target(issue["unit_id"], issue["seq"], issue),
                detail, issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return self.get_exchange_session(session_uuid)

    # ───────────────────────── 状态机（T3） ─────────────────────────

    # 状态枚举（复用 issues.status 字段，零新增列；DESIGN.md 1.1）
    STATUS_DRAFT = STATUS_DRAFT
    STATUS_SUBMITTED = STATUS_SUBMITTED
    STATUS_REJECTED = STATUS_REJECTED
    STATUS_REVIEWED = STATUS_REVIEWED
    STATUS_ARCHIVED = STATUS_ARCHIVED
    STATUSES = ISSUE_STATUSES

    # 流转矩阵：{旧状态: {允许的新状态}}（DESIGN.md 1.2）
    STATUS_FLOW: ClassVar[dict[str, frozenset[str]]] = STATUS_FLOW

    def change_status(self, issue_id: int, new_status: str, operator: str, comment: str = "") -> dict:
        """状态流转（DESIGN.md 1.5）：校验矩阵 + 必填规则，留痕。

        - 非法迁移抛 ValueError（消息含可走路径）
        - 复核退回：comment（退回意见）必填，写入 audit_log
        - 归档后编辑（已归档→编制完成）：comment（修改原因）必填，
          自动开新版本（snapshot 内嵌 change_reason）
        返回 {old, new}。
        """
        issue = self.get_issue(issue_id)
        if not issue:
            raise KeyError(f"底稿不存在: {issue_id}")
        transition = validate_status_transition(
            str(issue.get("status") or self.STATUS_DRAFT), new_status, issue, comment,
        )
        old = transition.old
        new = transition.new

        now = _now()
        with self._lock, self._conn:
            if old == self.STATUS_ARCHIVED:
                # 归档后编辑：自动开新版本，快照内嵌 change_reason
                data = {k: issue[k] for k in ISSUE_FIELDS}
                data["status"] = new
                data["change_reason"] = str(comment or "").strip()
                vno = self._conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 FROM issue_versions WHERE issue_id=?",
                    (issue_id,),
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,?,?,?,?)",
                    (issue_id, vno, json.dumps(data, ensure_ascii=False), operator, now),
                )
            self._conn.execute(
                "UPDATE issues SET status=?, updated_at=? WHERE id=?",
                (new, now, issue_id),
            )
            self._log_in_transaction(
                operator, "状态流转", self._issue_target(issue["unit_id"], issue["seq"], issue), transition.detail,
                issue_uuid=str(issue.get("issue_uuid") or ""),
            )
        return {"old": old, "new": new}

    # ───────────────────────── 版本历史 ─────────────────────────

    def list_versions(self, issue_id: int) -> list[dict]:
        """版本列表（snapshot 已解析为 dict），按版本号升序。"""
        rows = self._conn.execute(
            "SELECT id, issue_id, version_no, snapshot, saved_by, created_at "
            "FROM issue_versions WHERE issue_id=? ORDER BY version_no",
            (issue_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["snapshot"] = json.loads(d["snapshot"])
            except json.JSONDecodeError:
                d["snapshot"] = {}
            out.append(d)
        return out

    def version_counts(self) -> dict[int, int]:
        """各底稿版本数（台账导出 N+1 优化）：一次 GROUP BY 查询。"""
        rows = self._conn.execute(
            "SELECT issue_id, COUNT(*) c FROM issue_versions GROUP BY issue_id"
        ).fetchall()
        return {r["issue_id"]: r["c"] for r in rows}

    def get_version(self, version_id: int):
        r = self._conn.execute("SELECT * FROM issue_versions WHERE id=?", (version_id,)).fetchone()
        return dict(r) if r else None

    def restore_version(self, issue_id: int, version_id: int, operator: str):
        """恢复历史版本：先把当前内容追加为新版本（不丢），再把目标版本写回当前值。"""
        cur = self.get_issue(issue_id)
        v = self.get_version(version_id)
        if not cur or not v:
            raise KeyError("底稿或版本不存在")
        if v["issue_id"] != issue_id:
            raise ValueError("版本不属于该底稿")
        current_status = cur.get("status") or self.STATUS_DRAFT
        if current_status == self.STATUS_ARCHIVED:
            raise ValueError("已归档底稿不能直接恢复历史版本，请先执行『归档后编辑』")
        snap = json.loads(v["snapshot"])
        # 恢复的是底稿内容，不允许历史快照绕过状态机。已复核内容发生变化时重新进入复核。
        # 历史快照没有结构化金额字段时沿用当前值，避免“恢复文本旧版本”
        # 意外清空后来补录的币种/单位。
        restored = {k: snap[k] if k in snap else cur[k] for k in ISSUE_FIELDS}
        # 早期纯文本快照恢复后不能沿用当前富文本排版，否则页面会显示与该历史
        # 版本不同的内容。旧快照的纯文本字段仍是恢复来源，富文本安全回退为空。
        for rich_field, plain_field in _RICH_TEXT_FIELD_MAP.items():
            if rich_field not in snap and plain_field in snap:
                restored[rich_field] = ""
        restored["status"] = self.STATUS_SUBMITTED if current_status == self.STATUS_REVIEWED else current_status
        now = _now()
        with self._lock, self._conn:
            vno = self._conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM issue_versions WHERE issue_id=?", (issue_id,)
            ).fetchone()[0]
            # 恢复前的当前内容留档
            self._conn.execute(
                "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,?,?,?,?)",
                (issue_id, vno, json.dumps({k: cur[k] for k in ISSUE_FIELDS}, ensure_ascii=False),
                 operator, now),
            )
            sets = ", ".join(f"{k}=?" for k in ISSUE_FIELDS)
            self._conn.execute(
                f"UPDATE issues SET {sets}, updated_at=? WHERE id=?",
                (*[restored[k] for k in ISSUE_FIELDS], now, issue_id),
            )
            self._log_in_transaction(
                operator, "恢复版本", f"问题{cur['seq']}",
                f"恢复至版本{v['version_no']}（{v['created_at']}，保存人 {v['saved_by']}）",
                issue_uuid=str(cur.get("issue_uuid") or ""),
            )

    # ───────────────────────── 附件 ─────────────────────────

    def add_folder(self, unit_id: int, folder_files: list, folder_name: str, operator: str,
                   sha256: str = "") -> dict:
        """文件夹上传：内容原样复制到 附件库/{单位}/{文件夹名}_{id}/，作为一个附件实体。

        folder_files: [(相对路径, 临时文件路径[, 流式摘要]), ...]——目录内按相对路径还原结构。
        按单文件规则处理：列表一个条目、整体关联/删除/反查，不展开、不打包。
        sha256: 文件夹内容指纹（相对路径+文件内容哈希的排序摘要），重复检测用。
        """
        unit = self.get_unit(unit_id)
        if not unit:
            raise KeyError(f"单位不存在: {unit_id}")
        if not folder_files:
            raise ValueError("文件夹为空")
        oname = str(folder_name or "未命名文件夹").strip() or "未命名文件夹"
        dirname = f"{_safe(oname)}_{uuid.uuid4().hex[:8]}"
        dest_dir = self.root / ATTACH_DIR / self.unit_dir_name(unit_id) / dirname
        dest_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        seen_members: set[str] = set()
        try:
            digest_parts: list[str] = []
            for item in folder_files:
                rel, tmp = item[:2]
                target = self._folder_member_path(dest_dir, rel)
                member_key = target.relative_to(dest_dir).as_posix().casefold()
                if member_key in seen_members:
                    raise ValueError(f"文件夹内存在重复路径：{rel}")
                seen_members.add(member_key)
                target.parent.mkdir(parents=True, exist_ok=True)
                copied_size, copied_sha = self._copy_file_with_digest(Path(tmp), target)
                total += copied_size
                relative = target.relative_to(dest_dir).as_posix()
                if not any(part in SYSTEM_METADATA_NAMES for part in PurePosixPath(relative).parts):
                    digest_parts.append(f"{relative}\t{copied_sha}")
            actual_sha = self._folder_digest_from_parts(digest_parts)
            if sha256 and sha256 != actual_sha:
                raise ValueError("导入文件夹时内容发生变化，请重新选择后再上传")
            rel = f"{ATTACH_DIR}/{self.unit_dir_name(unit_id)}/{dirname}"
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO files(file_uuid, unit_id, stored_name, orig_name, rel_path, size, sha256, mime, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), unit_id, dirname, oname, rel, total, actual_sha, "folder", _now()),
                )
                fid = cur.lastrowid
                self._log_in_transaction(
                    operator, "导入文件夹", f"{unit['name']} · {oname}", f"{len(folder_files)} 个文件",
                    file_uuid=self.get_file(fid, include_deleted=True).get("file_uuid", ""),
                )
        except Exception:
            # 复制或登记失败时清除半成品目录，避免健康检查出现孤儿物理证据。
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        return self.get_file(fid)

    def find_folder_by_fingerprint(self, sha256: str) -> dict | None:
        """文件夹查重：按内容指纹（相对路径+文件内容哈希）找已存在文件夹实体。"""
        return self._evidence.find_folder_by_fingerprint(sha256)

    def get_file(self, file_id: int, *, include_deleted: bool = False):
        return self._evidence.get(file_id, include_deleted=include_deleted)

    def find_file_by_sha(self, sha256: str) -> dict | None:
        """项目级查重：按内容指纹找已存在文件（同一实体只存一份）。"""
        return self._evidence.find_file_by_sha(sha256)

    def list_files(self, unit_id: int) -> list[dict]:
        return self._evidence.list_active_for_unit(unit_id)

    def unlinked_files(self, unit_id: int) -> list[dict]:
        """资料库：该单位所有非独占文件（无论是否已关联其他底稿），
        共享模式下其他底稿可继续关联使用。前端自行过滤已关联当前问题的。"""
        return self._evidence.list_shareable_for_unit(unit_id)

    def add_file(self, unit_id: int, src_path, operator: str, orig_name: str = None,
                 folder_path: str = "", verified_sha256: str = "", verified_size: int | None = None) -> dict:
        """复制文件到 附件库/{单位名}/，磁盘名 uuid 防重名。返回文件记录。

        orig_name 可选：上传场景下临时文件名不是真实名，由调用方传入原始文件名。
        folder_path 可选：所属文件夹相对路径（如 证据包/子目录/），空=根目录。
        verified_sha256/verified_size 仅供 API 已在流式接收阶段完成核验的临时文件使用，
        可避免复制后第三次全量读取；其他调用仍由本方法自行计算摘要。
        """
        unit = self.get_unit(unit_id)
        if not unit:
            raise KeyError(f"单位不存在: {unit_id}")
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"文件不存在: {src}")
        source_size = src.stat().st_size
        if verified_size is not None and verified_size != source_size:
            raise ValueError("附件暂存大小与流式接收结果不一致，请重新上传")
        if verified_sha256 and not re.fullmatch(r"[0-9a-f]{64}", verified_sha256):
            raise ValueError("附件摘要格式无效，请重新上传")
        oname = str(orig_name or src.name).strip() or src.name
        ext = Path(oname).suffix.lower() or src.suffix.lower()
        stored = f"{uuid.uuid4().hex}{ext}"
        dest_dir = self.root / ATTACH_DIR / self.unit_dir_name(unit_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / stored
        try:
            shutil.copy2(src, dest)
            rel = f"{ATTACH_DIR}/{self.unit_dir_name(unit_id)}/{stored}"
            sha = verified_sha256 or self._sha256(dest)
            # folder_path 仅作展示元数据，但仍拒绝绝对路径和 ..，避免后续导出误用。
            folder_parts = PurePosixPath((folder_path or "").strip().replace("\\", "/")).parts
            if any(part in {".", ".."} for part in folder_parts):
                raise ValueError("附件所属文件夹包含非法相对路径")
            fpath = "/".join(folder_parts).lstrip("/")
            if fpath and not fpath.endswith("/"):
                fpath += "/"
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO files(file_uuid, unit_id, stored_name, orig_name, folder_path, rel_path, size, sha256, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), unit_id, stored, oname, fpath, rel, source_size, sha, _now()),
                )
                fid = cur.lastrowid
                file_uuid = str(self.get_file(fid, include_deleted=True).get("file_uuid") or "")
                self._log_in_transaction(
                    operator, "导入附件", f"{unit['name']} · {oname}", f"{source_size} 字节",
                    file_uuid=file_uuid,
                )
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        return self.get_file(fid)

    def remove_file(self, file_id: int, operator: str):
        """移入附件回收站；物理证据保留到用户明确清空回收站为止。"""
        f = self.get_file(file_id)
        if not f:
            raise KeyError(f"附件不存在: {file_id}")
        # 删除保护：被任何底稿引用的附件不能删除，必须先解除所有关联
        refs = self.linked_issue_ids_for_file(file_id)
        if refs:
            raise ValueError(
                f"附件「{f['orig_name']}」正被 {len(refs)} 个底稿引用，"
                "请先在问题中解除关联后再删除"
            )
        unit = self.get_unit(f["unit_id"])
        unit_name = unit["name"] if unit else f"单位{f['unit_id']}"
        deleted_at = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE files SET deleted_at=?, deleted_by=?, exclusive_to=NULL WHERE id=?",
                (deleted_at, operator, file_id),
            )
            self._conn.execute(
                "INSERT INTO recycle_bin(recycle_uuid, entity_type, entity_id, entity_uuid, deleted_by, deleted_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), "file", file_id, str(f.get("file_uuid") or ""), operator, deleted_at),
            )
            self._log_in_transaction(
                operator, "移入回收站", f"附件 {unit_name} · {f['orig_name']}",
                file_uuid=str(f.get("file_uuid") or ""),
            )

    def list_recycled_files(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT r.id recycle_id, r.deleted_at, r.deleted_by, f.id, f.file_uuid, f.unit_id, f.orig_name, f.mime, f.size, "
            "u.name unit_name FROM recycle_bin r JOIN files f ON f.id=r.entity_id "
            "LEFT JOIN units u ON u.id=f.unit_id "
            "WHERE r.entity_type='file' AND r.restored_at IS NULL AND r.purged_at IS NULL "
            "AND f.deleted_at IS NOT NULL ORDER BY r.deleted_at DESC, r.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def restore_recycled_file(self, recycle_id: int, operator: str) -> dict:
        record = self._conn.execute(
            "SELECT * FROM recycle_bin WHERE id=? AND entity_type='file' AND restored_at IS NULL AND purged_at IS NULL",
            (recycle_id,),
        ).fetchone()
        if not record:
            raise KeyError("附件回收站条目不存在或已处理")
        file = self.get_file(record["entity_id"], include_deleted=True)
        if not file or not file.get("deleted_at"):
            raise KeyError("待恢复附件不存在")
        if not self.get_unit(file["unit_id"]):
            raise ValueError("所属单位仍在回收站中，请先恢复单位")
        path = self.attachment_path(file["rel_path"])
        if not path.exists():
            raise ValueError("附件物理文件缺失，不能恢复")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute("UPDATE files SET deleted_at=NULL, deleted_by='' WHERE id=?", (file["id"],))
            self._conn.execute("UPDATE recycle_bin SET restored_at=? WHERE id=?", (now, recycle_id))
            self._log_in_transaction(
                operator, "从回收站恢复", f"附件 {file['orig_name']}",
                file_uuid=str(file.get("file_uuid") or ""),
            )
        return self.get_file(file["id"])

    def purge_recycled_file(self, recycle_id: int, operator: str) -> None:
        record = self._conn.execute(
            "SELECT * FROM recycle_bin WHERE id=? AND entity_type='file' AND restored_at IS NULL AND purged_at IS NULL",
            (recycle_id,),
        ).fetchone()
        if not record:
            raise KeyError("附件回收站条目不存在或已处理")
        file = self.get_file(record["entity_id"], include_deleted=True)
        if not file or not file.get("deleted_at"):
            raise KeyError("待清空附件不存在")
        if self.linked_issue_ids_for_file(file["id"]):
            raise ValueError("附件仍有底稿关联，不能物理删除")
        path = self.attachment_path(file["rel_path"])
        trash = path.with_name(f".{path.name}.purge_{uuid.uuid4().hex}")
        if path.exists():
            os.replace(path, trash)
        now = _now()
        try:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM issue_files WHERE file_id=?", (file["id"],))
                self._conn.execute("DELETE FROM files WHERE id=?", (file["id"],))
                self._conn.execute("UPDATE recycle_bin SET purged_at=? WHERE id=?", (now, recycle_id))
                self._log_in_transaction(
                    operator, "清空回收站", f"附件 {file['orig_name']}", "物理删除附件",
                    file_uuid=str(file.get("file_uuid") or ""),
                )
        except Exception:
            if trash.exists():
                os.replace(trash, path)
            raise
        if trash.is_dir():
            shutil.rmtree(trash, ignore_errors=True)
        else:
            trash.unlink(missing_ok=True)

    def issues_for_file(self, file_id: int) -> list[dict]:
        """反查：附件被哪些底稿引用（含单位信息）。"""
        rows = self._conn.execute(
            "SELECT i.id, i.seq, i.defect_type, i.department, i.defect_desc, "
            "       u.id AS unit_id, u.name AS unit_name "
            "FROM issue_files l JOIN issues i ON i.id = l.issue_id "
            "JOIN units u ON u.id = i.unit_id "
            "WHERE l.file_id=? ORDER BY u.sort_order, i.seq",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def rename_file(self, file_id: int, new_name: str, operator: str):
        f = self.get_file(file_id)
        if not f:
            raise KeyError(f"附件不存在: {file_id}")
        new_name = str(new_name).strip()
        if not new_name:
            raise ValueError("文件名不能为空")
        with self._lock, self._conn:
            self._conn.execute("UPDATE files SET orig_name=? WHERE id=?", (new_name, file_id))
            self._log_in_transaction(
                operator, "重命名附件", f"{f['orig_name']} → {new_name}",
                file_uuid=str(f.get("file_uuid") or ""),
            )

    def batch_rename_files(self, renames: list[dict], operator: str) -> dict:
        """批量重命名附件（审查 F-06 补齐）：事务内做冲突检测，冲突跳过。

        renames: [{id, name}, ...]。返回 {renamed, conflicts:[{id,name,reason}]}。
        冲突规则：目标名为空、目标单位内已有同名附件（排除自身）。
        """
        renamed = 0
        conflicts = []
        with self._lock, self._conn:
            for item in renames:
                fid = int(item.get("id"))
                new_name = str(item.get("name") or "").strip()
                f = self.get_file(fid)
                if not f:
                    conflicts.append({"id": fid, "name": new_name, "reason": "附件不存在"})
                    continue
                if not new_name:
                    conflicts.append({"id": fid, "name": new_name, "reason": "文件名不能为空"})
                    continue
                dup = self._conn.execute(
                    "SELECT id FROM files WHERE unit_id=? AND orig_name=? AND id!=? LIMIT 1",
                    (f["unit_id"], new_name, fid),
                ).fetchone()
                if dup:
                    conflicts.append({"id": fid, "name": new_name,
                                      "reason": f"单位内已存在同名附件（id={dup['id']}）"})
                    continue
                self._conn.execute("UPDATE files SET orig_name=? WHERE id=?", (new_name, fid))
                renamed += 1
        if renamed:
            self.log(operator, "批量重命名附件", f"成功 {renamed} 个",
                     f"跳过 {len(conflicts)} 个" if conflicts else "")
        return {"renamed": renamed, "conflicts": conflicts}

    def move_file_to_unit(self, file_id: int, target_unit_id: int, operator: str) -> dict:
        """移动附件到其他单位（审查 F-06 补齐）：物理移动 + 事务更新归属。

        引用关系（issue_files）按 file_id 不变，仍指向原底稿——移动后形成
        跨单位引用，删除单位时由 F-01 保护兜底。
        """
        f = self.get_file(file_id)
        if not f:
            raise KeyError(f"附件不存在: {file_id}")
        target = self.get_unit(target_unit_id)
        if not target:
            raise KeyError(f"目标单位不存在: {target_unit_id}")
        if f["unit_id"] == target_unit_id:
            raise ValueError("附件已属于该单位，无需移动")
        src = self.attachment_path(f["rel_path"])
        if not src.exists():
            raise FileNotFoundError(f"附件文件已丢失：{src}")
        # 物理移动：跨单位目录，文件夹实体整目录搬移
        new_dir = self.root / ATTACH_DIR / self.unit_dir_name(target_unit_id)
        new_dir.mkdir(parents=True, exist_ok=True)
        if f.get("mime") == "folder" and src.is_dir():
            dest = new_dir / src.name
            if dest.exists():
                raise ValueError(f"目标单位已存在同名文件夹：{src.name}")
        else:
            stored = f.get("stored_name") or src.name
            dest = new_dir / stored
            if dest.exists():
                raise ValueError(f"目标单位已存在同名文件：{stored}")
        rel = f"{ATTACH_DIR}/{self.unit_dir_name(target_unit_id)}/{dest.name}"
        old_unit = self.get_unit(f["unit_id"])
        old_name = old_unit["name"] if old_unit else f"单位{f['unit_id']}"
        shutil.move(str(src), str(dest))
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE files SET unit_id=?, rel_path=?, stored_name=? WHERE id=?",
                    (target_unit_id, rel, dest.name, file_id),
                )
                self._log_in_transaction(
                    operator, "移动附件", f"{old_name} → {target['name']} · {f['orig_name']}",
                    file_uuid=str(f.get("file_uuid") or ""),
                )
        except Exception:
            if dest.exists() and not src.exists():
                shutil.move(str(dest), str(src))
            raise
        return self.get_file(file_id)

    # ───────────────────────── 底稿↔附件 关联 ─────────────────────────

    def link_file(self, issue_id: int, file_id: int, operator: str):
        iss = self.get_issue(issue_id)
        f = self.get_file(file_id)
        if not iss or not f:
            raise KeyError("底稿或附件不存在")
        if f.get("exclusive_to") is not None and f["exclusive_to"] != issue_id:
            raise ValueError(
                f"附件「{f['orig_name']}」已仅关联到其他底稿，请先在原底稿中恢复为共享附件"
            )
        # 项目级去重后，附件可能被其他单位的问题引用（工作区内一份），允许跨单位关联
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO issue_files(issue_id, file_id, linked_at) VALUES(?,?,?)",
                (issue_id, file_id, _now()),
            )
            self._log_in_transaction(
                operator, "关联附件", f"问题{iss['seq']} ↔ {f['orig_name']}",
                issue_uuid=str(iss.get("issue_uuid") or ""), file_uuid=str(f.get("file_uuid") or ""),
            )

    def link_file_exclusive(self, issue_id: int, file_id: int, operator: str):
        """仅关联到当前问题：独占模式——附件移出资料库，只归该底稿，其他底稿不可见。"""
        iss = self.get_issue(issue_id)
        f = self.get_file(file_id)
        if not iss or not f:
            raise KeyError("底稿或附件不存在")
        old_refs = self.linked_issue_ids_for_file(file_id)
        with self._lock, self._conn:
            # “仅关联”必须先解除该附件的全部旧关联，再保留当前底稿这一条。
            self._conn.execute("DELETE FROM issue_files WHERE file_id=?", (file_id,))
            self._conn.execute("UPDATE files SET exclusive_to=? WHERE id=?", (issue_id, file_id))
            self._conn.execute(
                "INSERT OR IGNORE INTO issue_files(issue_id, file_id, linked_at) VALUES(?,?,?)",
                (issue_id, file_id, _now()),
            )
            removed = len([ref for ref in old_refs if ref != issue_id])
            detail = "独占，不进入资料库" + (f"；已解除其他 {removed} 处关联" if removed else "")
            self._log_in_transaction(
                operator, "仅关联附件", f"问题{iss['seq']} ↔ {f['orig_name']}", detail,
                issue_uuid=str(iss.get("issue_uuid") or ""), file_uuid=str(f.get("file_uuid") or ""),
            )

    def clear_file_exclusive(self, file_id: int, operator: str):
        """恢复共享：附件回到资料库，其他底稿可继续关联使用。"""
        f = self.get_file(file_id)
        if not f:
            raise KeyError(f"附件不存在: {file_id}")
        with self._lock, self._conn:
            self._conn.execute("UPDATE files SET exclusive_to=NULL WHERE id=?", (file_id,))
            self._log_in_transaction(
                operator, "恢复共享附件", f"{f['orig_name']}（回到资料库）",
                file_uuid=str(f.get("file_uuid") or ""),
            )

    def unlink_file(self, issue_id: int, file_id: int, operator: str):
        iss = self.get_issue(issue_id)
        f = self.get_file(file_id)
        if not iss or not f:
            raise KeyError("底稿或附件不存在")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM issue_files WHERE issue_id=? AND file_id=?", (issue_id, file_id))
            # 取消独占关联后自动回到共享资料库，避免留下无法再访问的隐藏附件。
            self._conn.execute(
                "UPDATE files SET exclusive_to=NULL WHERE id=? AND exclusive_to=?",
                (file_id, issue_id),
            )
            self._log_in_transaction(
                operator, "取消关联", f"问题{iss['seq']} ↛ {f['orig_name']}",
                issue_uuid=str(iss.get("issue_uuid") or ""), file_uuid=str(f.get("file_uuid") or ""),
            )

    def files_for_issue(self, issue_id: int) -> list[dict]:
        """底稿的附件列表，附 ref_count（该附件被多少个底稿引用，穿透同单位其他底稿）。"""
        rows = self._conn.execute(
            "SELECT f.*, COALESCE(ref_counts.ref_count, 0) AS ref_count "
            "FROM files f JOIN issue_files l ON l.file_id=f.id "
            "LEFT JOIN (SELECT file_id, COUNT(*) AS ref_count FROM issue_files GROUP BY file_id) "
            "AS ref_counts ON ref_counts.file_id=f.id "
            "WHERE l.issue_id=? ORDER BY f.orig_name",
            (issue_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def files_for_issues(self, issue_ids: list[int]) -> dict[int, list[dict]]:
        """一次读取多条底稿的附件，供导出/归档等批量只读操作使用。

        返回以底稿 ID 分组的字典。空列表不发 SQL，避免拼接空 IN 条件；查询保留
        ``files_for_issue`` 的展示排序与软删除前的既有语义。
        """
        selected_ids = list(dict.fromkeys(issue_ids))
        grouped = {issue_id: [] for issue_id in selected_ids}
        if not selected_ids:
            return grouped
        placeholders = ",".join("?" for _ in selected_ids)
        rows = self._conn.execute(
            "SELECT l.issue_id, f.* FROM issue_files l JOIN files f ON f.id=l.file_id "
            f"WHERE l.issue_id IN ({placeholders}) ORDER BY l.issue_id, f.orig_name",
            selected_ids,
        ).fetchall()
        for row in rows:
            record = dict(row)
            grouped[record.pop("issue_id")].append(record)
        return grouped

    def linked_issue_ids_for_file(self, file_id: int) -> list[int]:
        rows = self._conn.execute("SELECT issue_id FROM issue_files WHERE file_id=?", (file_id,)).fetchall()
        return [r["issue_id"] for r in rows]

    # ───────────────────────── 本地持久任务 ─────────────────────────

    JOB_QUEUED = "queued"
    JOB_RUNNING = "running"
    JOB_DONE = "done"
    JOB_CANCELLED = "cancelled"
    JOB_ERROR = "error"
    JOB_STATUSES: ClassVar[set[str]] = {JOB_QUEUED, JOB_RUNNING, JOB_DONE, JOB_CANCELLED, JOB_ERROR}

    @staticmethod
    def _decode_job(row) -> dict | None:
        if row is None:
            return None
        job = dict(row)
        for key in ("payload", "progress", "result"):
            try:
                job[key] = json.loads(job[key] or "{}")
            except (TypeError, json.JSONDecodeError):
                job[key] = {}
        job["cancel_requested"] = bool(job["cancel_requested"])
        return job

    def create_job(self, job_type: str, payload: dict | None = None) -> dict:
        """持久化一个本地任务；进程重启后状态和结果仍随项目保留。"""
        kind = str(job_type or "").strip()
        if not kind:
            raise ValueError("任务类型不能为空")
        job_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(id, type, status, payload, created_at) VALUES(?,?,?,?,?)",
                (job_id, kind, self.JOB_QUEUED, json.dumps(payload or {}, ensure_ascii=False), _now()),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode_job(row)

    def list_jobs(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall()
        return [self._decode_job(row) for row in rows]

    def start_job(self, job_id: str) -> dict | None:
        """原子领取排队任务；已取消或已领取时不重复执行。"""
        with self._lock, self._conn:
            job = self.get_job(job_id)
            if job is None:
                return None
            if job["status"] != self.JOB_QUEUED:
                return job
            if job["cancel_requested"]:
                self._conn.execute(
                    "UPDATE jobs SET status=?, finished_at=? WHERE id=?",
                    (self.JOB_CANCELLED, _now(), job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status=?, started_at=? WHERE id=?",
                    (self.JOB_RUNNING, _now(), job_id),
                )
        return self.get_job(job_id)

    def update_job_progress(self, job_id: str, progress: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET progress=? WHERE id=? AND status=?",
                (json.dumps(progress, ensure_ascii=False), job_id, self.JOB_RUNNING),
            )

    def finish_job(self, job_id: str, result: dict | None = None, error: str = "") -> dict | None:
        """结束任务。取消优先于成功，避免用户取消后仍显示完成。"""
        with self._lock, self._conn:
            job = self.get_job(job_id)
            if job is None or job["status"] not in {self.JOB_RUNNING, self.JOB_QUEUED}:
                return job
            if job["cancel_requested"]:
                status = self.JOB_CANCELLED
            elif error:
                status = self.JOB_ERROR
            else:
                status = self.JOB_DONE
            self._conn.execute(
                "UPDATE jobs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), str(error or ""), _now(), job_id),
            )
        return self.get_job(job_id)

    def request_job_cancel(self, job_id: str) -> dict | None:
        """取消排队或运行任务；运行中的任务由处理器在安全检查点停止。"""
        with self._lock, self._conn:
            job = self.get_job(job_id)
            if job is None:
                return None
            if job["status"] == self.JOB_QUEUED:
                self._conn.execute(
                    "UPDATE jobs SET cancel_requested=1, status=?, finished_at=? WHERE id=?",
                    (self.JOB_CANCELLED, _now(), job_id),
                )
            elif job["status"] == self.JOB_RUNNING:
                self._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        return self.get_job(job_id)

    def is_job_cancel_requested(self, job_id: str) -> bool:
        row = self._conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    # ───────────────────────── 合并批次与冲突 ─────────────────────────

    def record_merge_batch(self, operator: str, sources: list[str], conflicts: list[dict]) -> str:
        """记录已由负责人确认的合并策略，供归档和永久日志复核来源。"""
        batch_uuid = str(uuid.uuid4())
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO merge_batches(batch_uuid, operator, source_count, created_at) VALUES(?,?,?,?)",
                (batch_uuid, operator, len(sources), now),
            )
            for conflict in conflicts:
                self._conn.execute(
                    "INSERT INTO merge_conflicts(batch_uuid, source_name, conflict_type, message, resolution, "
                    "status, resolved_by, created_at, resolved_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch_uuid,
                        str(conflict.get("source") or ""),
                        str(conflict.get("type") or "unknown"),
                        str(conflict.get("message") or ""),
                        str(conflict.get("resolution") or "默认并存并重新编号"),
                        "resolved",
                        operator,
                        now,
                        now,
                    ),
                )
            self._log_in_transaction(
                operator,
                "记录合并批次",
                f"{len(sources)} 个来源",
                f"批次 {batch_uuid}；负责人已确认 {len(conflicts)} 项冲突处理方式",
            )
        return batch_uuid

    def unresolved_merge_conflicts(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT c.*, b.source_count FROM merge_conflicts c "
            "JOIN merge_batches b ON b.batch_uuid=c.batch_uuid "
            "WHERE c.status!='resolved' ORDER BY c.created_at, c.id"
        ).fetchall()
        return [dict(row) for row in rows]

    # ───────────────────────── 操作日志 ─────────────────────────

    @staticmethod
    def _audit_event_payload(
        *, event_uuid: str, project_uuid: str, issue_uuid: str, file_uuid: str,
        actor_account: str, actor_uid: str, device_id: str, action: str,
        target: str, detail: str, created_at: str, prev_hash: str,
    ) -> str:
        """生成稳定、可复算的日志链载荷；字段顺序不能依赖字典实现。"""
        return json.dumps({
            "event_uuid": event_uuid,
            "project_uuid": project_uuid,
            "issue_uuid": issue_uuid,
            "file_uuid": file_uuid,
            "actor_account": actor_account,
            "actor_uid": actor_uid,
            "device_id": device_id,
            "action": action,
            "target": target,
            "detail": detail,
            "created_at": created_at,
            "prev_hash": prev_hash,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _log_in_transaction(
        self, operator: str, action: str, target: str = "", detail: str = "", *,
        issue_uuid: str = "", file_uuid: str = "", actor_uid: str = "", device_id: str = "",
    ) -> None:
        """向当前事务追加日志。业务写入和日志必须调用本方法后一起提交。"""
        actor_account = str(operator or "未知").strip() or "未知"
        resolved_actor_uid = str(actor_uid or self._actor_uid or "")
        resolved_device_id = str(device_id or self._device_id or "")
        previous_row = self._conn.execute(
            "SELECT event_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = str(previous_row["event_hash"] or "") if previous_row else ""
        created_at = _now()
        event_uuid = str(uuid.uuid4())
        payload = self._audit_event_payload(
            event_uuid=event_uuid,
            project_uuid=self.project_uuid,
            issue_uuid=str(issue_uuid or ""),
            file_uuid=str(file_uuid or ""),
            actor_account=actor_account,
            actor_uid=resolved_actor_uid,
            device_id=resolved_device_id,
            action=str(action or ""),
            target=str(target or ""),
            detail=str(detail or ""),
            created_at=created_at,
            prev_hash=previous,
        )
        event_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._conn.execute(
            "INSERT INTO audit_log("
            "event_uuid, project_uuid, issue_uuid, file_uuid, actor_account, actor_uid, device_id, "
            "operator, action, target, detail, created_at, prev_hash, event_hash"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_uuid, self.project_uuid, str(issue_uuid or ""), str(file_uuid or ""),
             actor_account, resolved_actor_uid, resolved_device_id, actor_account,
             str(action or ""), str(target or ""), str(detail or ""), created_at, previous, event_hash),
        )

    def log(
        self, operator: str, action: str, target: str = "", detail: str = "", *,
        issue_uuid: str = "", file_uuid: str = "", actor_uid: str = "", device_id: str = "",
    ):
        """兼容既有调用的独立日志入口；事务内业务操作应改用 _log_in_transaction。"""
        with self._lock, self._conn:
            self._log_in_transaction(
                operator, action, target, detail, issue_uuid=issue_uuid, file_uuid=file_uuid,
                actor_uid=actor_uid, device_id=device_id,
            )

    def list_logs(
        self, limit: int = 500, *, actor: str = "", action: str = "",
        start_date: str = "", end_date: str = "",
    ) -> list[dict]:
        """读取永久操作日志，可按经办人、动作和自然日范围筛选。"""
        clauses: list[str] = []
        params: list[object] = []
        if actor.strip():
            clauses.append("operator LIKE ?")
            params.append(f"%{actor.strip()}%")
        if action.strip():
            clauses.append("action LIKE ?")
            params.append(f"%{action.strip()}%")
        if start_date:
            clauses.append("created_at >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date:
            clauses.append("created_at <= ?")
            params.append(f"{end_date} 23:59:59")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?", params,
        ).fetchall()
        return [dict(r) for r in rows]

    def verify_audit_log_chain(self) -> dict:
        """逐条复算永久日志链；用于归档前发现数据库被意外修改。"""
        previous = ""
        problems: list[dict] = []
        rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        for row in rows:
            item = dict(row)
            payload = self._audit_event_payload(
                event_uuid=str(item.get("event_uuid") or ""),
                project_uuid=str(item.get("project_uuid") or ""),
                issue_uuid=str(item.get("issue_uuid") or ""),
                file_uuid=str(item.get("file_uuid") or ""),
                actor_account=str(item.get("actor_account") or item.get("operator") or ""),
                actor_uid=str(item.get("actor_uid") or ""),
                device_id=str(item.get("device_id") or ""),
                action=str(item.get("action") or ""),
                target=str(item.get("target") or ""),
                detail=str(item.get("detail") or ""),
                created_at=str(item.get("created_at") or ""),
                prev_hash=previous,
            )
            expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if str(item.get("prev_hash") or "") != previous or str(item.get("event_hash") or "") != expected:
                problems.append({"id": item["id"], "message": f"日志 {item['id']} 的链哈希不连续或内容不匹配"})
            previous = str(item.get("event_hash") or "")
        return {"ok": not problems, "checked": len(rows), "problems": problems}

    def diagnostics_summary(self) -> dict:
        """生成可外发的最小诊断摘要，绝不读取或返回业务正文与文件标识。"""
        table_names = (
            "units", "issues", "files", "issue_versions", "audit_log",
            "issue_drafts", "review_note_events",
        )
        with self._lock:
            counts = {
                table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in table_names
            }
        audit_log_chain = self.verify_audit_log_chain()
        return {
            "format": "audit-trail-diagnostics-v1",
            "generated_at": _now(),
            "schema_version": SCHEMA_VERSION,
            "privacy": {
                "included": ["schema_version", "record_counts", "audit_log_chain_status"],
                "excluded": [
                    "project_name", "unit_names", "operator_names", "issue_content",
                    "attachment_names", "attachment_paths", "attachment_content",
                ],
            },
            "record_counts": counts,
            "audit_log_chain": {
                "ok": audit_log_chain["ok"],
                "checked": audit_log_chain["checked"],
                "problem_ids": [item["id"] for item in audit_log_chain["problems"]],
            },
        }

    # ───────────────────────── 工具 ─────────────────────────

    @staticmethod
    def _sha256(path, chunk_size: int = 1 << 20) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk_size)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    def _cached_sha256(self, path: Path, *, use_cache: bool) -> str:
        """按大小和纳秒级 mtime 复用摘要；全量归档核验始终调用方传 False。"""
        resolved = Path(path).resolve()
        stat = resolved.stat()
        key = str(resolved)
        state = (stat.st_size, stat.st_mtime_ns)
        if use_cache:
            with self._hash_cache_lock:
                cached = self._hash_cache.get(key)
            if cached and cached[:2] == state:
                return cached[2]
        digest = self._sha256(resolved)
        with self._hash_cache_lock:
            self._hash_cache[key] = (*state, digest)
        return digest

    @staticmethod
    def _folder_digest_from_parts(parts: list[str]) -> str:
        return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()

    @staticmethod
    def _copy_file_with_digest(source: Path, target: Path) -> tuple[int, str]:
        """复制文件时同步计算摘要，避免 copy2 后为了目录摘要再次完整读取。"""
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as src, target.open("wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        shutil.copystat(source, target)
        return size, digest.hexdigest()

    def _folder_digest(self, folder: Path, *, use_cache: bool = False) -> str:
        """目录摘要：排序后的 ``相对路径 + 成员 SHA-256`` 再做 SHA-256。

        拒绝符号链接，避免项目目录内的恶意链接将摘要读取带到附件库外。
        """
        root = Path(folder).resolve()
        if not root.is_dir():
            raise ValueError("文件夹实体物理目录不存在")
        parts: list[str] = []
        for member in root.rglob("*"):
            if member.is_symlink():
                raise ValueError("文件夹实体包含不允许的符号链接")
            if not member.is_file():
                continue
            relative = member.relative_to(root).as_posix()
            if any(part in SYSTEM_METADATA_NAMES for part in PurePosixPath(relative).parts):
                continue
            parts.append(f"{relative}\t{self._cached_sha256(member, use_cache=use_cache)}")
        return self._folder_digest_from_parts(parts)
