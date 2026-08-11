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
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar

DB_FILE = "audit.db"
ATTACH_DIR = "附件库"
OUT_DIR = "输出"
SNAPSHOT_DIR = "快照"
# 数据库 schema 版本（T12 版本兼容检查）
# - v1.1 及更早项目没有 schema_version 键 → 视为 0（兼容，迁移后写当前版本）
# - v2 引入 schema_migrations 和 jobs：迁移前用 SQLite backup API 创建项目内快照
# - 打开时若项目 schema_version > 当前 → 拒绝（项目由更新版本创建，需升级程序）
SCHEMA_VERSION = 3
SCHEMA_VERSION_KEY = "schema_version"

# 底稿内容字段（更新白名单 + 版本快照范围；seq 是单位内序号会重排，不进快照）
ISSUE_FIELDS = [
    "department", "category", "defect_type", "defect_desc", "amount",
    "regulation_basis", "suggestion", "author", "reviewer", "status",
]


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
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ATTACH_DIR).mkdir(exist_ok=True)
        (self.root / OUT_DIR).mkdir(exist_ok=True)
        (self.root / SNAPSHOT_DIR).mkdir(exist_ok=True)
        self.db_path = self.root / DB_FILE
        # FastAPI 多线程会共用连接，check_same_thread=False + 可重入互斥锁保护。
        # 3.0 的任务进度回调会在同一业务操作内更新 jobs，必须允许同线程重入，
        # 但不同线程仍串行访问这一条 SQLite 连接。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
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
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT DEFAULT '')"
            )
            cur = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_VERSION_KEY,)
            ).fetchone()
            try:
                old_version = int(cur["value"]) if cur is not None else 0
            except (TypeError, ValueError):
                old_version = 0
            if old_version > SCHEMA_VERSION:
                raise ValueError(
                    f"项目数据由更新版本（schema v{old_version}）创建，当前程序仅支持 v{SCHEMA_VERSION}。"
                    "请升级审迹后再打开此项目；如需回退，请先备份 .auditbak"
                )
            # 新建项目不需要快照；存在业务表但没有版本号的历史项目需要。
            has_legacy_data = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT IN ('meta', 'sqlite_sequence') LIMIT 1"
            ).fetchone() is not None
            needs_snapshot = old_version < SCHEMA_VERSION and (old_version > 0 or has_legacy_data)
            backup_rel_path = self._create_migration_snapshot(old_version) if needs_snapshot else ""

        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key   TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS units(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS issues(
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    unit_id         INTEGER NOT NULL,
                    seq             INTEGER NOT NULL,
                    department      TEXT DEFAULT '',
                    category        TEXT DEFAULT '',
                    defect_type     TEXT DEFAULT '',
                    defect_desc     TEXT DEFAULT '',
                    amount          TEXT DEFAULT '',
                    regulation_basis TEXT DEFAULT '',
                    suggestion      TEXT DEFAULT '',
                    author          TEXT DEFAULT '',
                    reviewer        TEXT DEFAULT '',
                    status          TEXT DEFAULT '草稿',
                    created_at      TEXT,
                    updated_at      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_issues_unit ON issues(unit_id);

                CREATE TABLE IF NOT EXISTS issue_versions(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id   INTEGER NOT NULL,
                    version_no INTEGER NOT NULL,
                    snapshot   TEXT NOT NULL,      -- 全字段 JSON 快照
                    saved_by   TEXT DEFAULT '',
                    created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_versions_issue ON issue_versions(issue_id);

                CREATE TABLE IF NOT EXISTS files(
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    unit_id     INTEGER NOT NULL,
                    stored_name TEXT NOT NULL,     -- 磁盘名（uuid+ext，防重名）
                    orig_name   TEXT NOT NULL,     -- 原始文件名（展示用）
                    folder_path TEXT NOT NULL DEFAULT '',  -- 所属文件夹相对路径（如 证据包/子目录/），空=根
                    rel_path    TEXT NOT NULL,     -- 附件库/{单位名}/{stored_name}
                    size        INTEGER DEFAULT 0,
                    sha256      TEXT DEFAULT '',
                    mime        TEXT DEFAULT '',          -- 文件类型标记（folder=文件夹实体）
                    exclusive_to INTEGER,          -- 仅关联模式：仅该底稿可见，不进入资料库
                    created_at  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_files_unit ON files(unit_id);

                CREATE TABLE IF NOT EXISTS issue_files(
                    issue_id  INTEGER NOT NULL,
                    file_id   INTEGER NOT NULL,
                    linked_at TEXT,
                    PRIMARY KEY (issue_id, file_id)
                );
                CREATE INDEX IF NOT EXISTS idx_issue_files_file ON issue_files(file_id);

                CREATE TABLE IF NOT EXISTS audit_log(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator   TEXT NOT NULL,
                    action     TEXT NOT NULL,
                    target     TEXT DEFAULT '',
                    detail     TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_log_operator ON audit_log(operator);
                CREATE INDEX IF NOT EXISTS idx_log_created ON audit_log(created_at);

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

            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at, backup_rel_path) VALUES(?,?,?)",
                (SCHEMA_VERSION, _now(), backup_rel_path),
            )

    def _create_migration_snapshot(self, source_version: int) -> str:
        """在迁移前创建 audit.db 的一致性快照，返回相对项目根目录的路径。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"pre_migration_v{source_version}_{stamp}.db"
        target = self.root / SNAPSHOT_DIR / name
        temp_target = target.with_suffix(".tmp")
        backup_conn = sqlite3.connect(temp_target)
        try:
            self._conn.backup(backup_conn)
        finally:
            backup_conn.close()
        os.replace(temp_target, target)
        return str(target.relative_to(self.root).as_posix())

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
        #    忽略隐藏文件/目录（.DS_Store 等 macOS 系统元数据，与备份/打包规则一致）
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
                # 隐藏路径段（文件名或所在目录以 . 开头）一律忽略
                if any(part.startswith(".") for part in phys.relative_to(att).parts):
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

        # 5) 哈希抽查（普通文件才有 sha；文件夹实体 sha 为空跳过）
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
            actual = self._sha256(phys)
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

    def issue_no(self, seq) -> str:
        """底稿编号（唯一识别码）：前缀 + 数字序号 + 后缀。

        规则存 meta（issue_number_prefix / issue_number_suffix，默认空 = 纯数字），
        树/详情/导出 Excel/打包目录统一用本方法计算，保证各流程编号一致。
        """
        prefix = self.get_meta("issue_number_prefix", "")
        suffix = self.get_meta("issue_number_suffix", "")
        return f"{prefix}{seq}{suffix}"

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

    def get_unit(self, unit_id: int):
        r = self._conn.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        return dict(r) if r else None

    def list_units(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM units ORDER BY sort_order, id").fetchall()
        return [dict(r) for r in rows]

    def add_unit(self, name: str, operator: str) -> int:
        name = str(name).strip()
        if not name:
            raise ValueError("单位名称不能为空")
        if self.get_unit_by_name(name):
            raise ValueError(f"单位「{name}」已存在")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO units(name, sort_order) VALUES(?, COALESCE((SELECT MAX(sort_order)+1 FROM units),0))",
                (name,),
            )
            uid = cur.lastrowid
        # 附件目录用稳定 ID（unit_{id}），不随单位显示名变化（审查 F-05 修复）
        (self.root / ATTACH_DIR / self.unit_dir_name(uid)).mkdir(exist_ok=True)
        self.log(operator, "新建单位", name)
        return uid

    def get_unit_by_name(self, name: str):
        r = self._conn.execute("SELECT * FROM units WHERE name=?", (str(name).strip(),)).fetchone()
        return dict(r) if r else None

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
        self.log(operator, "重命名单位", f"{old['name']} → {new_name}")

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
        n_issues = self._conn.execute("SELECT COUNT(*) FROM issues WHERE unit_id=?", (unit_id,)).fetchone()[0]
        n_files = self._conn.execute("SELECT COUNT(*) FROM files WHERE unit_id=?", (unit_id,)).fetchone()[0]
        with self._lock, self._conn:
            # 先删数据库记录（事务），再删物理目录
            # 其他单位的附件若独占关联到本单位底稿，删除单位前先恢复为共享资料。
            self._conn.execute(
                "UPDATE files SET exclusive_to=NULL "
                "WHERE exclusive_to IN (SELECT id FROM issues WHERE unit_id=?)",
                (unit_id,),
            )
            self._conn.execute(
                "DELETE FROM issue_files WHERE issue_id IN (SELECT id FROM issues WHERE unit_id=?)",
                (unit_id,),
            )
            self._conn.execute("DELETE FROM issue_versions WHERE issue_id IN (SELECT id FROM issues WHERE unit_id=?)",
                               (unit_id,))
            self._conn.execute("DELETE FROM issues WHERE unit_id=?", (unit_id,))
            self._conn.execute("DELETE FROM files WHERE unit_id=?", (unit_id,))
            self._conn.execute("DELETE FROM units WHERE id=?", (unit_id,))
        shutil.rmtree(self.root / ATTACH_DIR / self.unit_dir_name(unit_id), ignore_errors=True)
        self.log(operator, "删除单位", f"{unit['name']}（含 {n_issues} 条底稿、{n_files} 个附件）")

    def reset_all(self, operator: str):
        """清空项目全部业务数据并完全初始化（重置项目）。

        删除单位/底稿/版本快照/附件登记/关联/操作日志/异步任务记录，并清空
        附件库与输出目录的物理文件；保留 meta（项目名、schema 版本、版块与
        分类预设）——预设是配置而非数据，重置后重录底稿仍可复用。
        留痕：清空 audit_log 后补一条「重置项目」记录。
        """
        with self._lock, self._conn:
            # 先取消在跑任务（健康检查/扫描有 cancel 检查点），让线程尽快退出，
            # 再清空任务表——运行中的线程 finish_job 时 job 已不存在会静默返回。
            self._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE status=?", (self.JOB_RUNNING,))
            # 顺序：关联表 → 子表 → 主表
            self._conn.execute("DELETE FROM issue_files")
            self._conn.execute("DELETE FROM files")
            self._conn.execute("DELETE FROM issue_versions")
            self._conn.execute("DELETE FROM issues")
            self._conn.execute("DELETE FROM units")
            self._conn.execute("DELETE FROM audit_log")
            self._conn.execute("DELETE FROM jobs")
        # 附件库与输出目录的物理文件清空（保留目录本身）
        for d in (self.root / ATTACH_DIR, self.root / OUT_DIR):
            if d.exists():
                for child in d.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
        self.log(operator, "重置项目", "清空全部数据（单位/底稿/附件/日志），完全初始化")

    # ───────────────────────── 底稿 ─────────────────────────

    def list_issues(self, unit_id: int) -> list[dict]:
        """底稿列表，按单位内序号排序，附附件数。"""
        rows = self._conn.execute(
            """
            SELECT i.*,
                   (SELECT COUNT(*) FROM issue_files f WHERE f.issue_id=i.id) AS file_count
            FROM issues i WHERE i.unit_id=? ORDER BY i.seq
            """,
            (unit_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_issues_by_unit(self) -> dict[int, list[dict]]:
        """一次查询取得全项目底稿树，供 V3 单页双视图使用。

        不能让前端按每个单位重复调用 list_issues()，否则单位数量增加时会产生
        N+1 请求和 N 次 SQLite 查询。结果按单位显示顺序、底稿序号排序。
        """
        rows = self._conn.execute(
            """
            SELECT i.*,
                   (SELECT COUNT(*) FROM issue_files f WHERE f.issue_id=i.id) AS file_count
            FROM issues i
            JOIN units u ON u.id=i.unit_id
            ORDER BY u.sort_order, u.id, i.seq, i.id
            """
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            issue = dict(row)
            grouped.setdefault(issue["unit_id"], []).append(issue)
        return grouped

    def summary(self) -> dict:
        """三维汇总（T8）：按状态 / 按版块 / 按单位 + 问题明细列表。

        返回 {by_status, by_dept, by_unit, total, issues}，数量与明细一致（汇总数=明细数）。
        issues 供问题清单视图展示与跳转。
        """
        # 三组 SQL 聚合替代“每单位再查底稿/附件”的 2N+1 查询。
        with self._lock:
            status_rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(status),''), ?) name, COUNT(*) count "
                "FROM issues GROUP BY COALESCE(NULLIF(TRIM(status),''), ?)",
                (self.STATUS_DRAFT, self.STATUS_DRAFT),
            ).fetchall()
            dept_rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(department),''), '未分版块') name, COUNT(*) count "
                "FROM issues GROUP BY COALESCE(NULLIF(TRIM(department),''), '未分版块')"
            ).fetchall()
            unit_rows = self._conn.execute(
                "SELECT u.name, COUNT(DISTINCT i.id) issues, COUNT(DISTINCT f.id) files "
                "FROM units u LEFT JOIN issues i ON i.unit_id=u.id LEFT JOIN files f ON f.unit_id=u.id "
                "GROUP BY u.id, u.name ORDER BY u.sort_order, u.id"
            ).fetchall()
            issue_rows = self._conn.execute(
                "SELECT i.id, i.seq, i.unit_id, u.name unit_name, i.department, i.defect_type, "
                "i.category, i.amount, i.status, i.author, i.reviewer, "
                "(SELECT COUNT(*) FROM issue_files r WHERE r.issue_id=i.id) file_count "
                "FROM issues i JOIN units u ON u.id=i.unit_id "
                "ORDER BY u.sort_order, u.id, i.seq"
            ).fetchall()
        by_status = {row["name"]: row["count"] for row in status_rows}
        by_dept = {row["name"]: row["count"] for row in dept_rows}
        by_unit = {row["name"]: {"issues": row["issues"], "files": row["files"]} for row in unit_rows}
        total = sum(by_status.values())
        return {
            "by_status": by_status,
            "by_dept": by_dept,
            "by_unit": by_unit,
            "total": total,
            "issues": [dict(row) for row in issue_rows],
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
                "WHERE i.defect_type LIKE ? OR i.department LIKE ? OR i.defect_desc LIKE ? "
                "OR i.regulation_basis LIKE ? OR i.suggestion LIKE ? "
                "ORDER BY i.id DESC LIMIT 20",
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

    def get_issue(self, issue_id: int):
        r = self._conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        return dict(r) if r else None

    def _next_seq(self, unit_id: int) -> int:
        r = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM issues WHERE unit_id=?", (unit_id,)).fetchone()
        return r[0]

    def add_issue(self, unit_id: int, operator: str, **fields) -> int:
        """新建底稿：插入 + 初始版本 v1 + 日志。状态默认'草稿'。"""
        if not self.get_unit(unit_id):
            raise KeyError(f"单位不存在: {unit_id}")
        data = {k: str(fields.get(k, "") or "") for k in ISSUE_FIELDS}
        # 新建底稿必须从草稿开始，禁止调用方通过 POST/导入绕过状态机直接伪造已复核或已归档状态。
        data["status"] = self.STATUS_DRAFT
        now = _now()
        with self._lock, self._conn:
            # 序号计算与插入必须在同一把锁内，避免并发新建得到重复 seq。
            seq = self._next_seq(unit_id)
            cur = self._conn.execute(
                f"INSERT INTO issues(unit_id, seq, {', '.join(ISSUE_FIELDS)}, created_at, updated_at) "
                f"VALUES(?,?,{', '.join('?' * len(ISSUE_FIELDS))},?,?)",
                (unit_id, seq, *[data[k] for k in ISSUE_FIELDS], now, now),
            )
            iid = cur.lastrowid
            # 初始版本 v1
            self._conn.execute(
                "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,1,?,?,?)",
                (iid, json.dumps(data, ensure_ascii=False), operator, now),
            )
        self.log(operator, "新建底稿", self._issue_target(unit_id, seq, data))
        return iid

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
                   if k in ISSUE_FIELDS and k != "status" and v is not None}
        changed = [k for k, v in updates.items() if v != str(old[k] or "")]
        if not changed:
            return False
        # 已复核被编辑 → 自动降回编制完成（DESIGN.md 1.3：避免"改了但显示已复核"假象）
        if (old.get("status") or self.STATUS_DRAFT) == self.STATUS_REVIEWED:
            updates["status"] = self.STATUS_SUBMITTED
            if "status" not in changed:
                changed.append("status")
        now = _now()
        # 快照 = 旧全字段 + 本次更新合并（未提交字段保留旧值）
        data = {k: str(old[k] or "") for k in ISSUE_FIELDS}
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
        self.log(operator, "修改底稿", self._issue_target(old["unit_id"], old["seq"], data),
                 f"修改字段：{'、'.join(changed)}")
        return True

    def delete_issue(self, issue_id: int, operator: str):
        old = self.get_issue(issue_id)
        if not old:
            raise KeyError(f"底稿不存在: {issue_id}")
        unit = self.get_unit(old["unit_id"])
        unit_name = unit["name"] if unit else f"单位{old['unit_id']}"
        with self._lock, self._conn:
            # 独占附件随底稿删除时恢复共享，否则 exclusive_to 悬空后会永久隐藏在资料库之外。
            self._conn.execute("UPDATE files SET exclusive_to=NULL WHERE exclusive_to=?", (issue_id,))
            self._conn.execute("DELETE FROM issue_versions WHERE issue_id=?", (issue_id,))
            self._conn.execute("DELETE FROM issue_files WHERE issue_id=?", (issue_id,))
            self._conn.execute("DELETE FROM issues WHERE id=?", (issue_id,))
            self._renumber(old["unit_id"])
        self.log(operator, "删除底稿", f"{unit_name} · 问题{old['seq']}.{old['defect_type']}")

    def _renumber(self, unit_id: int):
        """删除底稿后序号连续重排。"""
        rows = self._conn.execute("SELECT id FROM issues WHERE unit_id=? ORDER BY seq, id", (unit_id,)).fetchall()
        for i, r in enumerate(rows, 1):
            self._conn.execute("UPDATE issues SET seq=? WHERE id=?", (i, r["id"]))

    def _issue_target(self, unit_id: int, seq: int, data: dict = None) -> str:
        unit = self.get_unit(unit_id)
        u = unit["name"] if unit else f"单位{unit_id}"
        t = (data or {}).get("defect_type", "")
        return f"{u} · 问题{seq}" + (f".{t}" if t else "")

    # ───────────────────────── 状态机（T3） ─────────────────────────

    # 状态枚举（复用 issues.status 字段，零新增列；DESIGN.md 1.1）
    STATUS_DRAFT = "草稿"
    STATUS_SUBMITTED = "编制完成"
    STATUS_REJECTED = "复核退回"
    STATUS_REVIEWED = "已复核"
    STATUS_ARCHIVED = "已归档"
    STATUSES = (STATUS_DRAFT, STATUS_SUBMITTED, STATUS_REJECTED, STATUS_REVIEWED, STATUS_ARCHIVED)

    # 流转矩阵：{旧状态: {允许的新状态}}（DESIGN.md 1.2）
    STATUS_FLOW: ClassVar[dict[str, set[str]]] = {
        STATUS_DRAFT: {STATUS_SUBMITTED},
        STATUS_SUBMITTED: {STATUS_REJECTED, STATUS_REVIEWED},
        STATUS_REJECTED: {STATUS_SUBMITTED},
        STATUS_REVIEWED: {STATUS_REJECTED, STATUS_ARCHIVED},
        # 归档后编辑：唯一合法去向是回到编制完成重新复核（自动开新版本+原因）
        STATUS_ARCHIVED: {STATUS_SUBMITTED},
    }

    # 非法迁移的"可以怎么走"提示（DESIGN.md 1.2：教用户怎么做）
    _STATUS_HINTS: ClassVar[dict[tuple[str, str], str]] = {
        (STATUS_ARCHIVED, STATUS_DRAFT): "已归档底稿如需修改，请使用『归档后编辑』（自动开新版本）",
        (STATUS_ARCHIVED, STATUS_REJECTED): "已归档底稿不能退回，请使用『归档后编辑』后重新复核",
        (STATUS_ARCHIVED, STATUS_REVIEWED): "已归档底稿已复核过，如需改动请使用『归档后编辑』",
    }

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
        old = issue.get("status") or self.STATUS_DRAFT
        new = str(new_status or "").strip()
        if new not in self.STATUSES:
            raise ValueError(f"未知状态：{new_status}。可选：{'、'.join(self.STATUSES)}")
        allowed = self.STATUS_FLOW.get(old, set())
        if new not in allowed:
            hint = self._STATUS_HINTS.get((old, new))
            if not hint:
                hint = f"不能从「{old}」变更为「{new}」。" + (
                    f"可以流转到：{'、'.join(sorted(allowed))}。" if allowed else "该状态不可再流转。"
                )
            raise ValueError(hint)

        # 必填校验（DESIGN.md 1.6，前端 + 后端双校验）
        if new == self.STATUS_SUBMITTED and old in (self.STATUS_DRAFT, self.STATUS_REJECTED):
            missing = [label for key, label in (
                ("defect_desc", "发现描述"), ("department", "版块"), ("defect_type", "定性"),
            ) if not str(issue.get(key) or "").strip()]
            if missing:
                raise ValueError(f"提交复核前请先填写：{'、'.join(missing)}")
        if new == self.STATUS_REVIEWED and not str(issue.get("reviewer") or "").strip():
            raise ValueError("复核通过前请填写审核人（reviewer）")
        if new == self.STATUS_REJECTED and not str(comment or "").strip():
            raise ValueError("复核退回请填写退回意见")
        if old == self.STATUS_ARCHIVED and not str(comment or "").strip():
            raise ValueError("归档后编辑请填写修改原因")

        now = _now()
        with self._lock, self._conn:
            if old == self.STATUS_ARCHIVED:
                # 归档后编辑：自动开新版本，快照内嵌 change_reason
                data = {k: str(issue[k] or "") for k in ISSUE_FIELDS}
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

        detail = f"{old} → {new}"
        if str(comment or "").strip():
            label = "退回意见" if new == self.STATUS_REJECTED else "修改原因"
            detail += f"（{label}：{str(comment).strip()}）"
        self.log(operator, "状态流转", self._issue_target(issue["unit_id"], issue["seq"], issue), detail)
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
        restored = {k: str(snap.get(k, "") or "") for k in ISSUE_FIELDS}
        restored["status"] = self.STATUS_SUBMITTED if current_status == self.STATUS_REVIEWED else current_status
        now = _now()
        with self._lock, self._conn:
            vno = self._conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM issue_versions WHERE issue_id=?", (issue_id,)
            ).fetchone()[0]
            # 恢复前的当前内容留档
            self._conn.execute(
                "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) VALUES(?,?,?,?,?)",
                (issue_id, vno, json.dumps({k: str(cur[k] or "") for k in ISSUE_FIELDS}, ensure_ascii=False),
                 operator, now),
            )
            sets = ", ".join(f"{k}=?" for k in ISSUE_FIELDS)
            self._conn.execute(
                f"UPDATE issues SET {sets}, updated_at=? WHERE id=?",
                (*[restored[k] for k in ISSUE_FIELDS], now, issue_id),
            )
        self.log(operator, "恢复版本", f"问题{cur['seq']}",
                 f"恢复至版本{v['version_no']}（{v['created_at']}，保存人 {v['saved_by']}）")

    # ───────────────────────── 附件 ─────────────────────────

    def add_folder(self, unit_id: int, folder_files: list, folder_name: str, operator: str,
                   sha256: str = "") -> dict:
        """文件夹上传：内容原样复制到 附件库/{单位}/{文件夹名}_{id}/，作为一个附件实体。

        folder_files: [(相对路径, 临时文件路径), ...]——目录内按相对路径还原结构。
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
            for rel, tmp in folder_files:
                target = self._folder_member_path(dest_dir, rel)
                member_key = target.relative_to(dest_dir).as_posix().casefold()
                if member_key in seen_members:
                    raise ValueError(f"文件夹内存在重复路径：{rel}")
                seen_members.add(member_key)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tmp, target)
                total += target.stat().st_size
            rel = f"{ATTACH_DIR}/{self.unit_dir_name(unit_id)}/{dirname}"
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO files(unit_id, stored_name, orig_name, rel_path, size, sha256, mime, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (unit_id, dirname, oname, rel, total, sha256, "folder", _now()),
                )
                fid = cur.lastrowid
        except Exception:
            # 复制或登记失败时清除半成品目录，避免健康检查出现孤儿物理证据。
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        self.log(operator, "导入文件夹", f"{unit['name']} · {oname}", f"{len(folder_files)} 个文件")
        return self.get_file(fid)

    def find_folder_by_fingerprint(self, sha256: str) -> dict | None:
        """文件夹查重：按内容指纹（相对路径+文件内容哈希）找已存在文件夹实体。"""
        if not sha256:
            return None
        row = self._conn.execute(
            "SELECT f.*, u.name AS unit_name FROM files f "
            "JOIN units u ON u.id = f.unit_id WHERE f.mime='folder' AND f.sha256=? "
            "ORDER BY f.id LIMIT 1",
            (sha256,),
        ).fetchone()
        return dict(row) if row else None

    def get_file(self, file_id: int):
        r = self._conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(r) if r else None

    def find_file_by_sha(self, sha256: str) -> dict | None:
        """项目级查重：按内容指纹找已存在文件（同一实体只存一份）。"""
        if not sha256:
            return None
        row = self._conn.execute(
            "SELECT f.*, u.name AS unit_name FROM files f "
            "JOIN units u ON u.id = f.unit_id WHERE f.sha256=? ORDER BY f.id LIMIT 1",
            (sha256,),
        ).fetchone()
        return dict(row) if row else None

    def list_files(self, unit_id: int) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM files WHERE unit_id=? ORDER BY orig_name", (unit_id,)).fetchall()
        return [dict(r) for r in rows]

    def unlinked_files(self, unit_id: int) -> list[dict]:
        """资料库：该单位所有非独占文件（无论是否已关联其他底稿），
        共享模式下其他底稿可继续关联使用。前端自行过滤已关联当前问题的。"""
        rows = self._conn.execute(
            "SELECT * FROM files WHERE unit_id=? AND exclusive_to IS NULL ORDER BY orig_name",
            (unit_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_file(self, unit_id: int, src_path, operator: str, orig_name: str = None,
                 folder_path: str = "") -> dict:
        """复制文件到 附件库/{单位名}/，磁盘名 uuid 防重名。返回文件记录。

        orig_name 可选：上传场景下临时文件名不是真实名，由调用方传入原始文件名。
        folder_path 可选：所属文件夹相对路径（如 证据包/子目录/），空=根目录。
        """
        unit = self.get_unit(unit_id)
        if not unit:
            raise KeyError(f"单位不存在: {unit_id}")
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"文件不存在: {src}")
        oname = str(orig_name or src.name).strip() or src.name
        ext = Path(oname).suffix.lower() or src.suffix.lower()
        stored = f"{uuid.uuid4().hex}{ext}"
        dest_dir = self.root / ATTACH_DIR / self.unit_dir_name(unit_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / stored
        try:
            shutil.copy2(src, dest)
            rel = f"{ATTACH_DIR}/{self.unit_dir_name(unit_id)}/{stored}"
            sha = self._sha256(dest)
            # folder_path 仅作展示元数据，但仍拒绝绝对路径和 ..，避免后续导出误用。
            folder_parts = PurePosixPath((folder_path or "").strip().replace("\\", "/")).parts
            if any(part in {".", ".."} for part in folder_parts):
                raise ValueError("附件所属文件夹包含非法相对路径")
            fpath = "/".join(folder_parts).lstrip("/")
            if fpath and not fpath.endswith("/"):
                fpath += "/"
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO files(unit_id, stored_name, orig_name, folder_path, rel_path, size, sha256, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (unit_id, stored, oname, fpath, rel, dest.stat().st_size, sha, _now()),
                )
                fid = cur.lastrowid
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        self.log(operator, "导入附件", f"{unit['name']} · {oname}", f"{dest.stat().st_size} 字节")
        return self.get_file(fid)

    def remove_file(self, file_id: int, operator: str):
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
        phys = self.attachment_path(f["rel_path"])
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM issue_files WHERE file_id=?", (file_id,))
            self._conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        if f.get("mime") == "folder" and phys.is_dir():
            # 文件夹实体：删除整个目录
            shutil.rmtree(phys, ignore_errors=True)
        else:
            phys.unlink(missing_ok=True)
        self.log(operator, "删除附件", f"{unit_name} · {f['orig_name']}")

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
        self.log(operator, "重命名附件", f"{f['orig_name']} → {new_name}")

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
            shutil.move(str(src), str(dest))
        else:
            stored = f.get("stored_name") or src.name
            dest = new_dir / stored
            if dest.exists():
                raise ValueError(f"目标单位已存在同名文件：{stored}")
            shutil.move(str(src), str(dest))
        rel = f"{ATTACH_DIR}/{self.unit_dir_name(target_unit_id)}/{dest.name}"
        old_unit = self.get_unit(f["unit_id"])
        old_name = old_unit["name"] if old_unit else f"单位{f['unit_id']}"
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE files SET unit_id=?, rel_path=?, stored_name=? WHERE id=?",
                (target_unit_id, rel, dest.name, file_id),
            )
        self.log(operator, "移动附件", f"{old_name} → {target['name']} · {f['orig_name']}")
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
        self.log(operator, "关联附件", f"问题{iss['seq']} ↔ {f['orig_name']}")

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
        self.log(operator, "仅关联附件", f"问题{iss['seq']} ↔ {f['orig_name']}", detail)

    def clear_file_exclusive(self, file_id: int, operator: str):
        """恢复共享：附件回到资料库，其他底稿可继续关联使用。"""
        f = self.get_file(file_id)
        if not f:
            raise KeyError(f"附件不存在: {file_id}")
        with self._lock, self._conn:
            self._conn.execute("UPDATE files SET exclusive_to=NULL WHERE id=?", (file_id,))
        self.log(operator, "恢复共享附件", f"{f['orig_name']}（回到资料库）")

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
        self.log(operator, "取消关联", f"问题{iss['seq']} ↛ {f['orig_name']}")

    def files_for_issue(self, issue_id: int) -> list[dict]:
        """底稿的附件列表，附 ref_count（该附件被多少个底稿引用，穿透同单位其他底稿）。"""
        rows = self._conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM issue_files r WHERE r.file_id = f.id) AS ref_count "
            "FROM files f JOIN issue_files l ON l.file_id=f.id "
            "WHERE l.issue_id=? ORDER BY f.orig_name",
            (issue_id,),
        ).fetchall()
        return [dict(r) for r in rows]

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

    # ───────────────────────── 操作日志 ─────────────────────────

    def log(self, operator: str, action: str, target: str = "", detail: str = ""):
        operator = str(operator or "未知").strip()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_log(operator, action, target, detail) VALUES(?,?,?,?)",
                (operator, action, target, detail),
            )

    def list_logs(self, limit: int = 500) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

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
