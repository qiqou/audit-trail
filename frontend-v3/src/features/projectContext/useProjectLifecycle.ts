import { ref, type Ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type ProjectInfo } from "../../api/client";

export type RecentProject = { path: string; name: string; time: number };

type ProjectLifecycleOptions = {
  project: Ref<ProjectInfo | null>;
  clearJobs: () => void;
  refreshReferenceData: () => Promise<void>;
  refreshRecent: () => Promise<void>;
  resetHealth: () => void;
  report: (error: unknown) => void;
  canLeaveCurrentWorkspace: () => Promise<boolean>;
};

/** 项目选择、打开、新建和从备份恢复的编排；根组件只负责把结果投影到页面。 */
export function useProjectLifecycle({
  project,
  clearJobs,
  refreshReferenceData,
  refreshRecent,
  resetHealth,
  report,
  canLeaveCurrentWorkspace,
}: ProjectLifecycleOptions) {
  const opening = ref(false);
  const creating = ref(false);
  const projectPath = ref("");
  const projectName = ref("");
  const restorePicker = ref<HTMLInputElement | null>(null);
  const restoreFile = ref<File | null>(null);
  const restoreLocalPath = ref("");
  const restoreTarget = ref("");
  const restoring = ref(false);

  async function chooseProjectFolder(): Promise<void> {
    try {
      const result = await api.projects.chooseFolder();
      if (result.path) projectPath.value = result.path;
      if (result.warning) ElMessage.warning(result.warning);
    } catch (error) {
      report(error);
    }
  }

  async function chooseRestoreTarget(): Promise<void> {
    try {
      const result = await api.projects.chooseFolder();
      if (result.path) restoreTarget.value = result.path;
      if (result.warning) ElMessage.warning(result.warning);
    } catch (error) {
      report(error);
    }
  }

  function inputRestoreFile(event: Event): void {
    restoreFile.value = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  async function applyOpenedProject(value: ProjectInfo): Promise<void> {
    project.value = value;
    clearJobs();
    await refreshReferenceData();
    await refreshRecent();
    resetHealth();
  }

  async function setProject(action: "open" | "create"): Promise<void> {
    if (!projectPath.value.trim()) {
      ElMessage.warning("请选择或输入项目文件夹");
      return;
    }
    opening.value = action === "open";
    creating.value = action === "create";
    try {
      await applyOpenedProject(action === "open"
        ? await api.projects.openProject(projectPath.value.trim())
        : await api.projects.createProject(projectPath.value.trim(), projectName.value.trim()));
      ElMessage.success(action === "open" ? "项目已打开" : "项目已创建");
    } catch (error) {
      report(error);
    } finally {
      opening.value = false;
      creating.value = false;
    }
  }

  async function openRecent(recent: RecentProject): Promise<void> {
    opening.value = true;
    try {
      await applyOpenedProject(await api.projects.openProject(recent.path));
      ElMessage.success(`已打开“${project.value?.project_name ?? recent.name}”`);
    } catch (error) {
      report(error);
    } finally {
      opening.value = false;
    }
  }

  async function openRestoredProject(path: string): Promise<boolean> {
    if (!(await canLeaveCurrentWorkspace())) return false;
    try {
      await applyOpenedProject(await api.projects.openProject(path));
      return true;
    } catch (error) {
      report(error);
      return false;
    }
  }

  async function restoreFromHub(): Promise<void> {
    if (restoring.value) return;
    const localPath = restoreLocalPath.value.trim();
    if (!restoreFile.value && !localPath) {
      ElMessage.warning("请选择 .auditbak 文件，或输入本机备份文件完整路径");
      return;
    }
    if (!restoreTarget.value.trim()) {
      ElMessage.warning("请选择或输入恢复目标目录");
      return;
    }
    try {
      await ElMessageBox.confirm(
        "恢复会写入目标文件夹；目标必须为空或不存在。恢复后目录自动加 .auditproj 后缀并隐藏。",
        "恢复项目备份",
        { type: "warning", confirmButtonText: "恢复并打开", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
    if (restoring.value) return;
    restoring.value = true;
    try {
      const result = localPath
        ? await api.restoreLocalBackup(localPath, restoreTarget.value.trim())
        : await api.restoreBackup(restoreFile.value!, restoreTarget.value.trim());
      if (await openRestoredProject(result.path)) {
        ElMessage.success("备份已恢复并打开项目");
      } else {
        ElMessage.warning(`备份已恢复至「${result.path}」，但未能自动打开；请从最近项目中再次打开该项目。`);
      }
    } catch (error) {
      report(error);
    } finally {
      restoring.value = false;
    }
  }

  function clearProjectInput(): void {
    projectPath.value = "";
    projectName.value = "";
  }

  return {
    chooseProjectFolder,
    chooseRestoreTarget,
    clearProjectInput,
    creating,
    inputRestoreFile,
    openRecent,
    openRestoredProject,
    opening,
    projectName,
    projectPath,
    restoreFile,
    restoreFromHub,
    restoreLocalPath,
    restorePicker,
    restoreTarget,
    restoring,
    setProject,
  };
}
