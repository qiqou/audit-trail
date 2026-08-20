import type { HealthResult, ProjectInfo, ProjectSummary, RecentProjectItem } from "../client";

export type ApiRequest = <T>(path: string, init?: RequestInit) => Promise<T>;

/**
 * 项目生命周期与项目汇总接口。
 *
 * 这里不直接创建 HTTP 客户端，调用方注入已附带会话身份的 request，避免领域拆分后
 * 出现会话令牌不一致。
 */
export function createProjectApi(request: ApiRequest) {
  return {
    chooseFolder: (): Promise<{ path: string; warning?: string }> => (
      request("/api/system/choose-folder", { method: "POST" })
    ),
    openProject: (path: string): Promise<ProjectInfo> => (
      request("/api/project/open", { method: "POST", body: JSON.stringify({ path }) })
    ),
    createProject: (path: string, name: string): Promise<ProjectInfo> => (
      request("/api/project/create", { method: "POST", body: JSON.stringify({ path, name }) })
    ),
    deleteProject: (path: string): Promise<{ deleted: string }> => (
      request("/api/project/delete", { method: "POST", body: JSON.stringify({ path }) })
    ),
    renameProject: (name: string): Promise<ProjectInfo> => (
      request("/api/project/rename", { method: "POST", body: JSON.stringify({ name }) })
    ),
    recent: (): Promise<{ items: RecentProjectItem[] }> => request("/api/recent"),
    forgetRecent: (path: string): Promise<{ ok: boolean }> => (
      request(`/api/recent?path=${encodeURIComponent(path)}`, { method: "DELETE" })
    ),
    health: (): Promise<HealthResult> => request("/api/project/health?sample_size=20"),
    summary: (): Promise<ProjectSummary> => request("/api/project/summary"),
  };
}
