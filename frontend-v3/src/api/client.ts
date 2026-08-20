export interface ProjectInfo {
  path: string;
  project_name: string;
  units: Unit[];
}

export interface RecentProjectItem {
  path: string;
  name: string;
  time: number;
}

export interface Unit {
  id: number;
  name: string;
  sort_order: number;
  created_at: string;
}

export interface HealthResult {
  ok: boolean;
  checked_at: string;
  counts: Record<string, number>;
  sample: { checked: number; total: number };
  problems: Array<{ type: string; severity: string; message: string }>;
}

export interface SummaryIssue {
  id: number;
  seq: number;
  unit_id: number;
  unit_name: string;
  department: string;
  defect_type: string;
  category: string;
  amount: string;
  amount_minor: number | null;
  currency: string;
  amount_unit: string;
  status: string;
  author: string;
  reviewer: string;
  file_count: number;
}

export interface ProjectSummary {
  total: number;
  by_status: Record<string, number>;
  by_dept: Record<string, number>;
  by_category: Record<string, number>;
  by_unit: Record<string, { issues: number; files: number }>;
  issues: SummaryIssue[];
  dashboard: ProjectDashboard;
}

export type BatchIssueMetadataChanges = Partial<Pick<Issue, "department" | "category" | "author" | "reviewer">>;

export interface BatchIssueMetadataPreflight {
  issue_ids: number[];
  changes: BatchIssueMetadataChanges;
  selected: number;
  affected: number;
  unchanged: number;
  reviewed: number;
  issues: Array<{ id: number; unit_id: number; seq: number; defect_type: string; status: string }>;
  confirmation_token: string;
}

export interface ProjectDashboard {
  overview: {
    units: number; issues: number; files: number; units_with_issues: number;
    departments: number; categories: number;
  };
  evidence: {
    files_total: number; linked_files: number; unlinked_files: number; issues_with_evidence: number;
  };
  units: DashboardUnit[];
  recent_activity: Array<{ operator: string; action: string; target: string; created_at: string }>;
}

export interface DashboardUnit {
  id: number;
  name: string;
  issues: number;
  files: number;
}

export interface SearchResult {
  units: { id: number; name: string }[];
  issues: SummaryIssue[];
  files: { id: number; unit_id: number; unit_name: string; orig_name: string; mime: string; exclusive_to: number | null; rel_path: string }[];
}

export interface AuditLog {
  id: number;
  operator: string;
  action: string;
  target: string;
  detail: string;
  created_at: string;
}

export interface AuditLogFilters {
  actor?: string;
  action?: string;
  start_date?: string;
  end_date?: string;
}

function auditLogFilterParams(filters: AuditLogFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value?.trim()) params.set(key, value);
  }
  return params.toString();
}

export interface ScanStatus {
  scan_id: string;
  status: "queued" | "running" | "done" | "cancelled" | "error";
  phase: string;
  done: number;
  total: number;
  problems: HealthResult["problems"];
  counts: Record<string, number>;
  sample: { checked: number; total: number };
  error: string;
}

export type IssueStatus = "草稿" | "编制完成" | "复核退回" | "已复核" | "已归档";

export interface Issue {
  id: number;
  unit_id: number;
  seq: number;
  department: string;
  category: string;
  defect_type: string;
  defect_desc: string;
  defect_desc_rich: string;
  amount: string;
  amount_minor: number | null;
  currency: string;
  amount_unit: string;
  regulation_basis: string;
  regulation_basis_rich: string;
  suggestion: string;
  suggestion_rich: string;
  author: string;
  reviewer: string;
  status: IssueStatus;
  created_at: string;
  updated_at: string;
  file_count?: number;
}

export interface IssueDraft {
  issue_id: number;
  issue_uuid: string;
  base_version_id: number;
  base_updated_at: string;
  payload: IssuePatch;
  saved_by: string;
  saved_at: string;
  current_version_id: number;
  current_updated_at: string;
  conflicted: boolean;
}

export interface IssueDraftState {
  draft: IssueDraft | null;
  current_version_id: number;
  current_updated_at: string;
}

export type ReviewNoteEventType = "created" | "replied" | "resolved" | "reopened";

export interface ReviewNoteEvent {
  event_uuid: string;
  note_uuid: string;
  issue_id: number;
  issue_uuid: string;
  base_version_id: number;
  anchor_field: string;
  event_seq: number;
  event_type: ReviewNoteEventType;
  body: string;
  created_by: string;
  created_at: string;
}

export interface ReviewNote {
  note_uuid: string;
  issue_id: number;
  issue_uuid: string;
  base_version_id: number;
  anchor_field: string;
  created_by: string;
  created_at: string;
  body: string;
  status: "open" | "resolved";
  is_stale: boolean;
  events: ReviewNoteEvent[];
}

export interface WorkpaperTemplate {
  id: number;
  template_uuid: string;
  name: string;
  data: Pick<Issue, "department" | "category" | "defect_type" | "defect_desc" | "amount" | "currency" | "amount_unit" | "regulation_basis" | "suggestion">;
  created_by: string;
  created_at: string;
  updated_by: string;
  updated_at: string;
}

export type ExchangeRevisionStatus = "proposed" | "accepted" | "rejected" | "withdrawn";
export type ExchangeRequestStatus = "open" | "provided" | "verified" | "withdrawn";

export interface ExchangeRevision {
  revision_uuid: string;
  session_uuid: string;
  version_id: number | null;
  field_name: string;
  old_value: string;
  new_value: string;
  reason: string;
  status: ExchangeRevisionStatus;
  proposed_by: string;
  proposed_at: string;
  decided_by: string;
  decided_at: string | null;
  applied_by: string;
  applied_at: string | null;
}

export interface ExchangeComment {
  comment_uuid: string;
  session_uuid: string;
  revision_uuid: string | null;
  anchor_field: string;
  body: string;
  created_by: string;
  created_at: string;
}

export interface ExchangeRequest {
  request_uuid: string;
  session_uuid: string;
  content: string;
  status: ExchangeRequestStatus;
  provided_file_id: number | null;
  provided_file_name: string | null;
  note: string;
  created_by: string;
  created_at: string;
  updated_by: string;
  updated_at: string | null;
}

export interface ExchangeSession {
  session_uuid: string;
  issue_id: number | null;
  issue_uuid: string;
  base_version_id: number | null;
  base_snapshot: Record<string, string | number | null>;
  status: "open" | "closed";
  opened_by: string;
  opened_at: string;
  closed_by: string;
  closed_at: string | null;
  close_note: string;
  issue: Issue | null;
  files: EvidenceFile[];
  revisions: ExchangeRevision[];
  comments: ExchangeComment[];
  requests: ExchangeRequest[];
  /** 仅交流轮次固化生成的版本（普通编辑保存的版本不在此列） */
  round_versions: IssueVersion[];
}

export interface EvidenceFile {
  id: number;
  unit_id: number;
  stored_name: string;
  orig_name: string;
  size: number;
  mime: string;
  sha256: string;
  exclusive_to: number | null;
  ref_count?: number;
}

export interface IssueVersion {
  id: number;
  issue_id: number;
  version_no: number;
  snapshot: Record<string, string>;
  saved_by: string;
  created_at: string;
}

export interface FileReference {
  id: number;
  seq: number;
  defect_type: string;
  department: string;
  defect_desc: string;
  unit_id: number;
  unit_name: string;
}

export interface ImportResult {
  imported: number;
  skipped: number;
  new_units: number;
  files?: number;
  errors: string[];
}

export interface ExcelImportPreflight extends ImportResult {
  confirmation_token: string;
  expires_in_seconds: number;
}

export interface ExportResult {
  filename: string;
  count: number;
  download_url: string;
}

export interface PackageResult {
  filename: string;
  units: number;
  issues: number;
  files: number;
  download_url: string;
}

export interface ArchivePreflight {
  ok: boolean;
  blockers: Array<{ code: string; message: string }>;
  warnings: Array<{ code: string; message: string }>;
  counts: { units: number; issues: number; files: number; non_archived: number };
  health: { checked: { checked: number; total: number }; problems: number };
  confirmation_token: string;
}

export interface BackupResult {
  filename: string;
  download_url: string;
}

export interface BackupSettings {
  enabled: boolean;
  target_dir: string;
  interval_minutes: number;
  retention_days: number;
  max_bytes: number;
  last_success_at: string;
  last_error: string;
}

export interface AmountSettings {
  currency: string;
  amount_unit: string;
  allowed_units: string[];
}

export interface RecoveryPoint {
  id: string;
  created_at: string;
  attachments: number;
  size: number;
  logical_bytes: number;
  health: string;
}

export interface RecycledIssue {
  recycle_id: number;
  deleted_at: string;
  deleted_by: string;
  id: number;
  issue_uuid: string;
  unit_id: number;
  unit_name: string;
  seq: number;
  department: string;
  defect_type: string;
  status: string;
}

export interface RecycledIssuePreview {
  recycle_id: number;
  deleted_at: string;
  deleted_by: string;
  unit_name: string;
  issue: Issue & { issue_uuid?: string; deleted_at?: string; deleted_by?: string };
  version_count: number;
  attachment_total: number;
  attachments: Array<{ id: number; orig_name: string; mime: string; size: number; sha256: string }>;
  attachments_truncated: boolean;
}

export interface RecycledUnit {
  recycle_id: number;
  deleted_at: string;
  deleted_by: string;
  id: number;
  unit_uuid: string;
  name: string;
  issue_count: number;
  file_count: number;
}

export interface RecycledFile {
  recycle_id: number;
  deleted_at: string;
  deleted_by: string;
  id: number;
  file_uuid: string;
  unit_id: number;
  unit_name: string | null;
  orig_name: string;
  mime: string;
  size: number;
}

export interface MergeResult {
  units: number;
  issues: number;
  versions: number;
  files: number;
  folders: number;
  depts: number;
  errors: string[];
  conflicts: Array<{ type: string; message: string }>;
}

export interface MergePreflight {
  ok: boolean;
  blockers: Array<{ source: string; code: string; message: string }>;
  conflicts: Array<{ source: string; type: string; resolution: string; message: string }>;
  sources: Array<{ name: string; project_uuid: string; units: number; issues: number; attachments: number }>;
  confirmation_token: string;
}

export interface BatchRenameResult {
  renamed: number;
  conflicts: Array<{ id: number; name: string; reason: string }>;
}

export interface FolderUploadItem {
  file: File;
  relativePath: string;
}

export type IssueChanges = Pick<Issue,
  "department" | "category" | "defect_type" | "defect_desc" | "defect_desc_rich" | "amount" | "regulation_basis" | "regulation_basis_rich" | "suggestion" | "suggestion_rich" | "author" | "reviewer"> & {
  currency?: string;
  amount_unit?: string;
};

// PATCH 请求允许只提交实际编辑的字段。富文本编辑器临时下线期间，长文本字段
// 只提交纯文本列；后端会据此清除对应的旧富文本视图，避免旧排版覆盖新输入。
export type IssuePatch = Partial<IssueChanges>;

interface ApiErrorBody {
  detail?: string;
}

class ApiClient {
  private token = sessionStorage.getItem("audit_token") ?? "";

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (this.token) headers.set("X-Session", this.token);
    if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    let response: Response;
    try {
      response = await fetch(path, { ...init, headers });
    } catch {
      throw new Error("无法连接本地服务，请确认审迹已启动");
    }
    const body = (await response.json().catch(() => null)) as T | ApiErrorBody | null;
    if (!response.ok) {
      const message = (body as ApiErrorBody | null)?.detail ?? `请求失败（${response.status}）`;
      if (message.includes("使用人会话无效")) {
        this.clearSession();
        window.dispatchEvent(new CustomEvent("audit-session-expired"));
      }
      throw new Error(message);
    }
    return body as T;
  }

  async login(operator: string): Promise<{ token: string; operator: string; account_id: string; device_id: string }> {
    const result = await this.request<{ token: string; operator: string; account_id: string; device_id: string }>("/api/session", {
      method: "POST",
      body: JSON.stringify({ operator }),
    });
    this.token = result.token;
    sessionStorage.setItem("audit_token", result.token);
    sessionStorage.setItem("audit_operator", result.operator);
    return result;
  }

  clearSession(): void {
    this.token = "";
    sessionStorage.removeItem("audit_token");
    sessionStorage.removeItem("audit_operator");
  }

  currentSession(): Promise<{ operator: string; account_id: string; device_id: string; project_preempted?: boolean }> {
    return this.request("/api/session");
  }

  logout(): Promise<{ ok: boolean }> {
    return this.request("/api/session", { method: "DELETE" });
  }

  exportAuditLogs(filters: AuditLogFilters = {}): Promise<{ filename: string; abs_path: string; count: number; download_url: string }> {
    return this.request(`/api/logs/export?${auditLogFilterParams(filters)}`, { method: "POST" });
  }

  exportDiagnosticsSupportPackage(): Promise<{ filename: string; download_url: string }> {
    return this.request("/api/diagnostics/support-package", { method: "POST" });
  }

  chooseFolder(): Promise<{ path: string; warning?: string }> {
    return this.request("/api/system/choose-folder", { method: "POST" });
  }

  openProject(path: string): Promise<ProjectInfo> {
    return this.request("/api/project/open", { method: "POST", body: JSON.stringify({ path }) });
  }

  createProject(path: string, name: string): Promise<ProjectInfo> {
    return this.request("/api/project/create", { method: "POST", body: JSON.stringify({ path, name }) });
  }

  deleteProject(path: string): Promise<{ deleted: string }> {
    return this.request("/api/project/delete", { method: "POST", body: JSON.stringify({ path }) });
  }

  renameProject(name: string): Promise<ProjectInfo> {
    return this.request("/api/project/rename", { method: "POST", body: JSON.stringify({ name }) });
  }

  recent(): Promise<{ items: RecentProjectItem[] }> {
    return this.request("/api/recent");
  }

  forgetRecent(path: string): Promise<{ ok: boolean }> {
    return this.request(`/api/recent?path=${encodeURIComponent(path)}`, { method: "DELETE" });
  }

  units(): Promise<Unit[]> {
    return this.request("/api/units");
  }

  addUnit(name: string): Promise<{ id: number }> {
    return this.request("/api/units", { method: "POST", body: JSON.stringify({ name }) });
  }

  renameUnit(unitId: number, name: string): Promise<{ ok: boolean }> {
    return this.request(`/api/units/${unitId}`, { method: "PATCH", body: JSON.stringify({ name }) });
  }

  reorderUnits(ids: number[]): Promise<{ changed: boolean }> {
    return this.request("/api/units/order", { method: "PUT", body: JSON.stringify({ ids }) });
  }

  deleteUnit(unitId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/units/${unitId}`, { method: "DELETE" });
  }

  health(): Promise<HealthResult> {
    return this.request("/api/project/health?sample_size=20");
  }

  summary(): Promise<ProjectSummary> {
    return this.request("/api/project/summary");
  }

  batchIssueMetadataPreflight(issueIds: number[], changes: BatchIssueMetadataChanges): Promise<BatchIssueMetadataPreflight> {
    return this.request("/api/issues/batch-metadata/preflight", {
      method: "POST", body: JSON.stringify({ issue_ids: issueIds, changes }),
    });
  }

  batchIssueMetadata(issueIds: number[], changes: BatchIssueMetadataChanges, confirmationToken: string): Promise<{ updated: number; unchanged: number; issue_ids: number[] }> {
    return this.request("/api/issues/batch-metadata", {
      method: "POST", body: JSON.stringify({ issue_ids: issueIds, changes, confirmation_token: confirmationToken }),
    });
  }

  search(q: string): Promise<SearchResult> {
    return this.request(`/api/search?q=${encodeURIComponent(q)}`);
  }

  logs(filters: AuditLogFilters = {}): Promise<AuditLog[]> {
    return this.request(`/api/logs?${auditLogFilterParams(filters)}`);
  }

  departments(): Promise<string[]> {
    return this.request("/api/settings/departments");
  }

  saveDepartments(departments: string[]): Promise<string[]> {
    return this.request("/api/settings/departments", {
      method: "POST", body: JSON.stringify({ departments }),
    });
  }

  async categories(): Promise<string[]> {
    try {
      return await this.request<string[]>("/api/settings/categories");
    } catch (error) {
      // V2 项目打开时可与尚未重启的旧本地服务短暂共存；分类是可选预设，
      // 缺少该新接口不应阻塞项目打开，也不应向用户弹出无上下文的 Not Found。
      if (error instanceof Error && error.message === "Not Found") return [];
      throw error;
    }
  }

  issueNumber(): Promise<{ prefix: string; suffix: string }> {
    return this.request("/api/settings/issue-number");
  }

  saveIssueNumber(prefix: string, suffix: string): Promise<{ prefix: string; suffix: string }> {
    return this.request("/api/settings/issue-number", {
      method: "POST",
      body: JSON.stringify({ prefix, suffix }),
    });
  }

  async saveCategories(categories: string[]): Promise<string[]> {
    try {
      return await this.request<string[]>("/api/settings/categories", {
        method: "POST", body: JSON.stringify({ categories }),
      });
    } catch (error) {
      // 旧服务进程没有此接口时，给出可执行的提示，避免仅显示无上下文的 405/404。
      if (error instanceof Error && ["Method Not Allowed", "Not Found"].includes(error.message)) {
        throw new Error("当前运行的本地服务版本过旧，尚未加载“问题分类预设”接口。请完全退出审迹后重新启动，再重试。");
      }
      throw error;
    }
  }

  startFullScan(): Promise<{ scan_id: string }> {
    return this.request("/api/project/scan", { method: "POST" });
  }

  scanStatus(scanId: string): Promise<ScanStatus> {
    return this.request(`/api/project/scan/${scanId}`);
  }

  cancelScan(scanId: string): Promise<{ ok: boolean; status: string }> {
    return this.request(`/api/project/scan/${scanId}/cancel`, { method: "POST" });
  }

  importTemplate(): Promise<void> {
    return this.downloadUrl("/api/import/template", "问题导入模板.xlsx");
  }

  importExcel(file: File): Promise<ImportResult> {
    const form = new FormData();
    form.append("file", file, file.name);
    return this.request("/api/import/excel", { method: "POST", body: form });
  }

  preflightExcelImport(file: File): Promise<ExcelImportPreflight> {
    const form = new FormData();
    form.append("file", file, file.name);
    return this.request("/api/import/excel/preflight", { method: "POST", body: form });
  }

  commitExcelImport(file: File, confirmationToken: string): Promise<ImportResult> {
    const form = new FormData();
    form.append("file", file, file.name);
    return this.request(`/api/import/excel/commit?confirmation_token=${encodeURIComponent(confirmationToken)}`, {
      method: "POST", body: form,
    });
  }

  mergeBackups(files: File[]): Promise<MergeResult> {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    return this.request("/api/import/merge", { method: "POST", body: form });
  }

  mergeLocalBackups(backupPaths: string[]): Promise<MergeResult> {
    return this.mergeLocalBackupsConfirmed(backupPaths, "");
  }

  mergeLocalBackupsConfirmed(backupPaths: string[], confirmationToken: string): Promise<MergeResult> {
    return this.request("/api/import/merge-local", {
      method: "POST",
      body: JSON.stringify({ backup_paths: backupPaths, confirmation_token: confirmationToken }),
    });
  }

  mergeLocalPreflight(backupPaths: string[]): Promise<MergePreflight> {
    return this.request("/api/import/merge-local/preflight", {
      method: "POST",
      body: JSON.stringify({ backup_paths: backupPaths }),
    });
  }

  exportExcel(scope: "project" | "unit", unitId?: number): Promise<ExportResult> {
    return this.request("/api/export/excel", {
      method: "POST",
      body: JSON.stringify({ scope, ...(scope === "unit" ? { unit_id: unitId } : {}) }),
    });
  }

  packagePreflight(unitIds: number[], groupByDepartment: boolean): Promise<ArchivePreflight> {
    const selected = unitIds.length > 0;
    return this.request("/api/export/package/preflight", {
      method: "POST",
      body: JSON.stringify({
        scope: selected ? "selected" : "all",
        unit_ids: unitIds,
        group_by_dept: groupByDepartment,
      }),
    });
  }

  packageProject(unitIds: number[], groupByDepartment: boolean, confirmationToken: string): Promise<PackageResult> {
    const selected = unitIds.length > 0;
    return this.request("/api/export/package", {
      method: "POST",
      body: JSON.stringify({
        scope: selected ? "selected" : "all",
        unit_ids: unitIds,
        group_by_dept: groupByDepartment,
        confirmation_token: confirmationToken,
      }),
    });
  }

  createBackup(): Promise<BackupResult> {
    return this.request("/api/backup/create", { method: "POST" });
  }

  backupSettings(): Promise<BackupSettings> {
    return this.request("/api/backup/settings");
  }

  saveBackupSettings(values: Pick<BackupSettings, "enabled" | "target_dir" | "interval_minutes" | "retention_days" | "max_bytes">): Promise<BackupSettings> {
    return this.request("/api/backup/settings", { method: "POST", body: JSON.stringify(values) });
  }

  createRecoveryPoint(): Promise<{ job_id: string; status: string }> {
    return this.request("/api/backup/recovery-point", { method: "POST" });
  }

  recoveryPoints(): Promise<RecoveryPoint[]> {
    return this.request("/api/backup/recovery-points");
  }

  restoreRecoveryPoint(recoveryPointId: string, targetDir: string): Promise<{ path: string }> {
    return this.request("/api/backup/recovery-points/restore", {
      method: "POST",
      body: JSON.stringify({ recovery_point_id: recoveryPointId, target_dir: targetDir }),
    });
  }

  restoreBackup(file: File, targetDir: string): Promise<{ path: string }> {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("target_dir", targetDir);
    return this.request("/api/backup/restore", { method: "POST", body: form });
  }

  restoreLocalBackup(backupPath: string, targetDir: string): Promise<{ path: string }> {
    return this.request("/api/backup/restore-local", {
      method: "POST",
      body: JSON.stringify({ backup_path: backupPath, target_dir: targetDir }),
    });
  }

  resetProject(confirmText: string): Promise<{ ok: boolean }> {
    return this.request("/api/project/reset", {
      method: "POST",
      body: JSON.stringify({ confirm_text: confirmText }),
    });
  }

  restartProgram(): Promise<{ ok: boolean }> {
    return this.request("/api/system/restart", { method: "POST" });
  }

  quitProgram(): Promise<{ ok: boolean }> {
    return this.request("/api/system/quit", { method: "POST" });
  }

  issues(unitId: number): Promise<Issue[]> {
    return this.request(`/api/units/${unitId}/issues`);
  }

  issueTree(): Promise<Record<string, Issue[]>> {
    return this.request("/api/issues/tree");
  }

  createIssue(unitId: number, values: Partial<IssueChanges> = {}): Promise<{ id: number }> {
    return this.request(`/api/units/${unitId}/issues`, {
      method: "POST",
      body: JSON.stringify(values),
    });
  }

  reorderIssues(unitId: number, ids: number[]): Promise<{ changed: boolean }> {
    return this.request(`/api/units/${unitId}/issues/order`, {
      method: "PUT", body: JSON.stringify({ ids }),
    });
  }

  issue(issueId: number): Promise<Issue> {
    return this.request(`/api/issues/${issueId}`);
  }

  duplicateIssue(issueId: number, unitId?: number | null): Promise<Issue> {
    return this.request(`/api/issues/${issueId}/duplicate`, {
      method: "POST", body: JSON.stringify(unitId !== undefined && unitId !== null ? { unit_id: unitId } : {}),
    });
  }

  workpaperTemplates(): Promise<WorkpaperTemplate[]> {
    return this.request("/api/workpaper-templates");
  }

  createWorkpaperTemplate(name: string, issueId: number): Promise<WorkpaperTemplate> {
    return this.request("/api/workpaper-templates", {
      method: "POST", body: JSON.stringify({ name, issue_id: issueId }),
    });
  }

  applyWorkpaperTemplate(templateId: number, unitId: number): Promise<Issue> {
    return this.request(`/api/workpaper-templates/${templateId}/apply`, {
      method: "POST", body: JSON.stringify({ unit_id: unitId }),
    });
  }

  deleteWorkpaperTemplate(templateId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/workpaper-templates/${templateId}`, { method: "DELETE" });
  }

  updateIssue(issueId: number, values: IssuePatch): Promise<{ changed: boolean; issue: Issue }> {
    return this.request(`/api/issues/${issueId}`, { method: "PATCH", body: JSON.stringify(values) });
  }

  issueDraft(issueId: number): Promise<IssueDraftState> {
    return this.request(`/api/issues/${issueId}/draft`);
  }

  saveIssueDraft(
    issueId: number, payload: IssuePatch, baseVersionId: number, baseUpdatedAt: string,
  ): Promise<IssueDraftState> {
    return this.request(`/api/issues/${issueId}/draft`, {
      method: "PUT",
      body: JSON.stringify({
        payload,
        base_version_id: baseVersionId,
        base_updated_at: baseUpdatedAt,
      }),
    });
  }

  discardIssueDraft(issueId: number): Promise<{ discarded: boolean }> {
    return this.request(`/api/issues/${issueId}/draft`, { method: "DELETE" });
  }

  reviewNotes(issueId: number): Promise<ReviewNote[]> {
    return this.request(`/api/issues/${issueId}/review-notes`);
  }

  createReviewNote(
    issueId: number, body: string, baseVersionId: number, anchorField = "",
  ): Promise<ReviewNote> {
    return this.request(`/api/issues/${issueId}/review-notes`, {
      method: "POST",
      body: JSON.stringify({ body, base_version_id: baseVersionId, anchor_field: anchorField }),
    });
  }

  appendReviewNoteEvent(
    noteUuid: string, eventType: Exclude<ReviewNoteEventType, "created">, body = "",
  ): Promise<ReviewNote> {
    return this.request(`/api/review-notes/${noteUuid}/${eventType === "replied" ? "reply" : eventType === "resolved" ? "resolve" : "reopen"}`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
  }

  startExchange(issueId: number): Promise<ExchangeSession> {
    return this.request(`/api/issues/${issueId}/exchange`, { method: "POST" });
  }

  exchange(sessionUuid: string): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}`);
  }

  proposeExchangeRevision(sessionUuid: string, fieldName: string, newValue: string, reason: string): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}/revisions`, {
      method: "POST", body: JSON.stringify({ field_name: fieldName, new_value: newValue, reason }),
    });
  }

  decideExchangeRevision(sessionUuid: string, revisionUuid: string, decision: ExchangeRevisionStatus): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}/revisions/${revisionUuid}/decision`, {
      method: "POST", body: JSON.stringify({ decision }),
    });
  }

  addExchangeComment(sessionUuid: string, body: string, anchorField = "", revisionUuid = ""): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}/comments`, {
      method: "POST", body: JSON.stringify({ body, anchor_field: anchorField, revision_uuid: revisionUuid }),
    });
  }

  createExchangeRequest(sessionUuid: string, content: string): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}/requests`, { method: "POST", body: JSON.stringify({ content }) });
  }

  updateExchangeRequest(sessionUuid: string, requestUuid: string, status: ExchangeRequestStatus,
                        providedFileId: number | null, note: string): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}/requests/${requestUuid}`, {
      method: "PATCH", body: JSON.stringify({ status, provided_file_id: providedFileId, note }),
    });
  }

  applyExchangeRevisions(sessionUuid: string): Promise<{ session: ExchangeSession; issue: Issue }> {
    return this.request(`/api/exchanges/${sessionUuid}/apply`, { method: "POST" });
  }

  closeExchange(sessionUuid: string, note = ""): Promise<ExchangeSession> {
    return this.request(`/api/exchanges/${sessionUuid}/close`, { method: "POST", body: JSON.stringify({ note }) });
  }

  deleteIssue(issueId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/issues/${issueId}`, { method: "DELETE" });
  }

  recycledIssues(): Promise<RecycledIssue[]> {
    return this.request("/api/recycle/issues");
  }

  recycledIssuePreview(recycleId: number): Promise<RecycledIssuePreview> {
    return this.request(`/api/recycle/issues/${recycleId}`);
  }

  restoreRecycledIssue(recycleId: number): Promise<Issue> {
    return this.request(`/api/recycle/issues/${recycleId}/restore`, { method: "POST" });
  }

  purgeRecycledIssue(recycleId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/recycle/issues/${recycleId}`, { method: "DELETE" });
  }

  recycledUnits(): Promise<RecycledUnit[]> {
    return this.request("/api/recycle/units");
  }

  restoreRecycledUnit(recycleId: number): Promise<Unit> {
    return this.request(`/api/recycle/units/${recycleId}/restore`, { method: "POST" });
  }

  purgeRecycledUnit(recycleId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/recycle/units/${recycleId}`, { method: "DELETE" });
  }

  recycledFiles(): Promise<RecycledFile[]> {
    return this.request("/api/recycle/files");
  }

  restoreRecycledFile(recycleId: number): Promise<EvidenceFile> {
    return this.request(`/api/recycle/files/${recycleId}/restore`, { method: "POST" });
  }

  purgeRecycledFile(recycleId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/recycle/files/${recycleId}`, { method: "DELETE" });
  }

  amountSettings(): Promise<AmountSettings> {
    return this.request("/api/settings/amount");
  }

  saveAmountSettings(values: Pick<AmountSettings, "currency" | "amount_unit">): Promise<AmountSettings> {
    return this.request("/api/settings/amount", { method: "PUT", body: JSON.stringify(values) });
  }

  transitionIssue(issueId: number, status: IssueStatus, comment = ""): Promise<Issue> {
    return this.request(`/api/issues/${issueId}/status`, {
      method: "POST",
      body: JSON.stringify({ status, comment }),
    });
  }

  versions(issueId: number): Promise<IssueVersion[]> {
    return this.request(`/api/issues/${issueId}/versions`);
  }

  exportIssueConfirmationDocx(issueId: number): Promise<{ filename: string; download_url: string }> {
    return this.request(`/api/issues/${issueId}/confirmation-docx`, { method: "POST" });
  }

  restoreVersion(issueId: number, versionId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/issues/${issueId}/versions/${versionId}/restore`, { method: "POST" });
  }

  issueFiles(issueId: number): Promise<EvidenceFile[]> {
    return this.request(`/api/issues/${issueId}/files`);
  }

  libraryFiles(unitId: number): Promise<EvidenceFile[]> {
    return this.request(`/api/units/${unitId}/files/unlinked`);
  }

  uploadFile(unitId: number, file: File): Promise<EvidenceFile | { duplicated: true; file: EvidenceFile; message: string }> {
    const form = new FormData();
    form.append("file", file, file.name);
    return this.request(`/api/units/${unitId}/files`, { method: "POST", body: form });
  }

  uploadFolder(unitId: number, folderName: string, files: Array<File | FolderUploadItem>): Promise<EvidenceFile | { duplicated: true; file: EvidenceFile; message: string }> {
    const form = new FormData();
    form.append("folder_name", folderName);
    for (const item of files) {
      const file = item instanceof File ? item : item.file;
      const relativePath = item instanceof File
        ? (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
        : item.relativePath;
      form.append("files", file, relativePath);
    }
    return this.request(`/api/units/${unitId}/folder-upload`, { method: "POST", body: form });
  }

  linkFile(issueId: number, fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/issues/${issueId}/files/${fileId}/link`, { method: "POST" });
  }

  unlinkFile(issueId: number, fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/issues/${issueId}/files/${fileId}/link`, { method: "DELETE" });
  }

  linkFileExclusive(issueId: number, fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/issues/${issueId}/files/${fileId}/link-exclusive`, { method: "POST" });
  }

  makeFileShared(fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/files/${fileId}/shared`, { method: "POST" });
  }

  renameFile(fileId: number, name: string): Promise<{ ok: boolean }> {
    return this.request(`/api/files/${fileId}`, { method: "PATCH", body: JSON.stringify({ name }) });
  }

  batchRenameFiles(renames: Array<{ id: number; name: string }>): Promise<BatchRenameResult> {
    return this.request("/api/files/batch-rename", { method: "POST", body: JSON.stringify({ renames }) });
  }

  moveFile(fileId: number, unitId: number): Promise<EvidenceFile> {
    return this.request(`/api/files/${fileId}/move`, { method: "POST", body: JSON.stringify({ unit_id: unitId }) });
  }

  deleteFile(fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/files/${fileId}`, { method: "DELETE" });
  }

  fileReferences(fileId: number): Promise<FileReference[]> {
    return this.request(`/api/files/${fileId}/issues`);
  }

  async openUnitAttachmentDirectory(unitId: number): Promise<{ ok: boolean }> {
    const path = `/api/units/${unitId}/attachments/open`;
    try {
      return await this.request<{ ok: boolean }>(path, { method: "POST" });
    } catch (error) {
      // 兼容先前界面错误发出的 GET；服务端同时保留 GET/POST，避免再出现 405。
      if (error instanceof Error && error.message === "Method Not Allowed") {
        return this.request<{ ok: boolean }>(path);
      }
      throw error;
    }
  }

  openEvidenceFolder(fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/files/${fileId}/directory/open`, { method: "POST" });
  }

  openFile(fileId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/files/${fileId}/open`, { method: "POST" });
  }

  async downloadFile(fileId: number, filename: string): Promise<void> {
    await this.downloadUrl(`/api/files/${fileId}/download`, filename);
  }

  async downloadUrl(path: string, filename: string): Promise<void> {
    const headers = new Headers();
    if (this.token) headers.set("X-Session", this.token);
    let response: Response;
    try {
      response = await fetch(path, { headers });
    } catch {
      throw new Error("无法连接本地服务，请确认审迹已启动");
    }
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
      const message = body?.detail ?? `下载失败（${response.status}）`;
      if (message.includes("使用人会话无效")) {
        this.clearSession();
        window.dispatchEvent(new CustomEvent("audit-session-expired"));
      }
      throw new Error(message);
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
}

export const api = new ApiClient();
