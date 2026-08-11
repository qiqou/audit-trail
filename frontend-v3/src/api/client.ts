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
  status: string;
  author: string;
  reviewer: string;
  file_count: number;
}

export interface ProjectSummary {
  total: number;
  by_status: Record<string, number>;
  by_dept: Record<string, number>;
  by_unit: Record<string, { issues: number; files: number }>;
  issues: SummaryIssue[];
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
  amount: string;
  regulation_basis: string;
  suggestion: string;
  author: string;
  reviewer: string;
  status: IssueStatus;
  created_at: string;
  updated_at: string;
  file_count?: number;
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

export interface BackupResult {
  filename: string;
  download_url: string;
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

export interface BatchRenameResult {
  renamed: number;
  conflicts: Array<{ id: number; name: string; reason: string }>;
}

export interface FolderUploadItem {
  file: File;
  relativePath: string;
}

export type IssueChanges = Pick<Issue,
  "department" | "category" | "defect_type" | "defect_desc" | "amount" | "regulation_basis" | "suggestion" | "author" | "reviewer">;

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

  async login(operator: string): Promise<{ token: string; operator: string }> {
    const result = await this.request<{ token: string; operator: string }>("/api/session", {
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

  currentSession(): Promise<{ operator: string }> {
    return this.request("/api/session");
  }

  logout(): Promise<{ ok: boolean }> {
    return this.request("/api/session", { method: "DELETE" });
  }

  chooseFolder(): Promise<{ path: string }> {
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

  deleteUnit(unitId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/units/${unitId}`, { method: "DELETE" });
  }

  health(): Promise<HealthResult> {
    return this.request("/api/project/health?sample_size=20");
  }

  summary(): Promise<ProjectSummary> {
    return this.request("/api/project/summary");
  }

  search(q: string): Promise<SearchResult> {
    return this.request(`/api/search?q=${encodeURIComponent(q)}`);
  }

  logs(): Promise<AuditLog[]> {
    return this.request("/api/logs");
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

  mergeBackups(files: File[]): Promise<MergeResult> {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    return this.request("/api/import/merge", { method: "POST", body: form });
  }

  exportExcel(scope: "project" | "unit", unitId?: number): Promise<ExportResult> {
    return this.request("/api/export/excel", {
      method: "POST",
      body: JSON.stringify({ scope, ...(scope === "unit" ? { unit_id: unitId } : {}) }),
    });
  }

  packageProject(unitIds: number[], groupByDepartment: boolean): Promise<PackageResult> {
    const selected = unitIds.length > 0;
    return this.request("/api/export/package", {
      method: "POST",
      body: JSON.stringify({
        scope: selected ? "selected" : "all",
        unit_ids: unitIds,
        group_by_dept: groupByDepartment,
      }),
    });
  }

  createBackup(): Promise<BackupResult> {
    return this.request("/api/backup/create", { method: "POST" });
  }

  restoreBackup(file: File, targetDir: string): Promise<{ path: string }> {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("target_dir", targetDir);
    return this.request("/api/backup/restore", { method: "POST", body: form });
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

  issue(issueId: number): Promise<Issue> {
    return this.request(`/api/issues/${issueId}`);
  }

  updateIssue(issueId: number, values: IssueChanges): Promise<{ changed: boolean; issue: Issue }> {
    return this.request(`/api/issues/${issueId}`, { method: "PATCH", body: JSON.stringify(values) });
  }

  deleteIssue(issueId: number): Promise<{ ok: boolean }> {
    return this.request(`/api/issues/${issueId}`, { method: "DELETE" });
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
