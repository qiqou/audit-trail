<script setup lang="ts">
import { FolderOpened, SwitchButton } from "@element-plus/icons-vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type HealthResult, type ProjectInfo, type Unit } from "./api/client";
import IssueWorkspace from "./components/IssueWorkspace.vue";
import ProjectOperations from "./components/ProjectOperations.vue";

type RecentProject = { path: string; name: string; time: number };
type AutoSaveMode = "realtime" | "5m" | "20m";
const RECENT_KEY = "audit_recent_projects";
const AUTO_SAVE_KEY = "audit_auto_save_mode_v3";

function loadRecentLocal(): RecentProject[] {
  // localStorage 仅作降级兜底（后端不可用时的静态读取；正式记录走后端 /api/recent）
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(value)
      ? value.filter((item): item is RecentProject => Boolean(item?.path && item?.name)).slice(0, 20)
      : [];
  } catch {
    return [];
  }
}

async function refreshRecent(): Promise<void> {
  try {
    const items = (await api.recent()).items;
    recentProjects.value = items
      .filter((item): item is RecentProject => Boolean(item?.path && item?.name))
      .slice(0, 20);
  } catch {
    recentProjects.value = loadRecentLocal();
  }
}

const operator = ref(sessionStorage.getItem("audit_operator") ?? "");
const loginName = ref(operator.value);
const project = ref<ProjectInfo | null>(null);
const units = ref<Unit[]>([]);
const departments = ref<string[]>([]);
const categories = ref<string[]>([]);
const issueNumberRule = ref<{ prefix: string; suffix: string }>({ prefix: "", suffix: "" });
const opening = ref(false);
const creating = ref(false);
const projectPath = ref("");
const projectName = ref("");
const health = ref<HealthResult | null>(null);
const busy = ref(false);
const recentProjects = ref<RecentProject[]>(loadRecentLocal());
type Theme = "dark" | "light" | "green";
const storedTheme = localStorage.getItem("audit_theme");
const theme = ref<Theme>(storedTheme === "light" || storedTheme === "green" || storedTheme === "dark" ? storedTheme : "dark");
const storedAutoSaveMode = localStorage.getItem(AUTO_SAVE_KEY);
const autoSaveMode = ref<AutoSaveMode>(
  storedAutoSaveMode === "realtime" || storedAutoSaveMode === "20m" || storedAutoSaveMode === "5m"
    ? storedAutoSaveMode
    : "5m",
);
const workspace = ref<{ confirmCurrentLeave: () => Promise<boolean>; selectIssueById: (issueId: number) => Promise<void>; selectUnit: (unitId: number) => void } | null>(null);

const loggedIn = computed(() => Boolean(operator.value));

function applyTheme(value: Theme): void {
  theme.value = value;
  document.documentElement.dataset.theme = value;
  document.documentElement.classList.toggle("dark", value === "dark");
  localStorage.setItem("audit_theme", value);
}

applyTheme(theme.value);

function report(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : "操作失败，请重试");
}

async function forgetRecent(path: string): Promise<void> {
  try {
    await api.forgetRecent(path);
    recentProjects.value = recentProjects.value.filter((project) => project.path !== path);
  } catch (error) {
    report(error);
  }
}

async function deleteProject(recent: RecentProject): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `项目文件夹（数据库、附件、输出）将一并删除且不可恢复。项目目录已隐藏，删除后无法从文件管理器找回。确定删除「${recent.name}」？`,
      "删除项目（不可恢复）",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return; // 用户取消
  }
  try {
    await api.deleteProject(recent.path);
    await refreshRecent(); // 后端删除项目时已自动移除最近记录
    if (project.value?.path === recent.path) {
      backToProjectList(true); // 项目已删除，强制返回项目列表（跳过未保存确认）
    }
    ElMessage.success("项目已删除");
  } catch (error) {
    report(error);
  }
}

function formatRecent(time: number): string {
  return new Date(time).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" });
}

async function login(): Promise<void> {
  if (!loginName.value.trim()) {
    ElMessage.warning("请输入使用人姓名");
    return;
  }
  busy.value = true;
  try {
    operator.value = (await api.login(loginName.value.trim())).operator;
    await refreshRecent();
    ElMessage.success(`欢迎，${operator.value}`);
  } catch (error) {
    report(error);
  } finally {
    busy.value = false;
  }
}

async function chooseProjectFolder(): Promise<void> {
  try {
    projectPath.value = (await api.chooseFolder()).path;
  } catch (error) {
    report(error);
  }
}

// 初始界面「从备份恢复项目」：选 .auditbak + 目标目录 → 恢复并打开（复用 openRestoredProject）
const restorePicker = ref<HTMLInputElement | null>(null);
const restoreFile = ref<File | null>(null);
const restoreTarget = ref("");
const restoring = ref(false);

async function chooseRestoreTarget(): Promise<void> {
  try {
    restoreTarget.value = (await api.chooseFolder()).path;
  } catch (error) {
    report(error);
  }
}

function inputRestoreFile(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0];
  restoreFile.value = file ?? null;
}

async function restoreFromHub(): Promise<void> {
  if (!restoreFile.value) {
    ElMessage.warning("请先选择 .auditbak 备份文件");
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
    return; // 用户取消
  }
  restoring.value = true;
  try {
    const result = await api.restoreBackup(restoreFile.value, restoreTarget.value.trim());
    await openRestoredProject(result.path);
    ElMessage.success("备份已恢复，正在打开恢复后的项目");
  } catch (error) {
    report(error);
  } finally {
    restoring.value = false;
  }
}

async function setProject(action: "open" | "create"): Promise<void> {
  if (!projectPath.value.trim()) {
    ElMessage.warning("请选择或输入项目文件夹");
    return;
  }
  opening.value = action === "open";
  creating.value = action === "create";
  try {
    project.value = action === "open"
      ? await api.openProject(projectPath.value.trim())
      : await api.createProject(projectPath.value.trim(), projectName.value.trim());
    await Promise.all([refreshUnits(), refreshDepartments(), refreshCategories(), refreshIssueNumber()]);
    await refreshRecent(); // 后端打开/创建时已自动记录
    health.value = null;
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
    project.value = await api.openProject(recent.path);
    await Promise.all([refreshUnits(), refreshDepartments(), refreshCategories(), refreshIssueNumber()]);
    await refreshRecent(); // 后端打开时已自动更新记录时间
    health.value = null;
    ElMessage.success(`已打开“${project.value.project_name}”`);
  } catch (error) {
    report(error);
  } finally {
    opening.value = false;
  }
}

async function refreshUnits(): Promise<void> {
  try {
    units.value = await api.units();
  } catch (error) {
    report(error);
  }
}

async function refreshDepartments(): Promise<void> {
  try {
    departments.value = await api.departments();
  } catch (error) {
    report(error);
  }
}

async function refreshCategories(): Promise<void> {
  try {
    categories.value = await api.categories();
  } catch (error) {
    report(error);
  }
}

async function refreshIssueNumber(): Promise<void> {
  try {
    issueNumberRule.value = await api.issueNumber();
  } catch (error) {
    report(error);
  }
}

function departmentsSaved(values: string[]): void {
  departments.value = [...values];
}

function categoriesSaved(values: string[]): void {
  categories.value = [...values];
}

function issueNumberChanged(rule: { prefix: string; suffix: string }): void {
  issueNumberRule.value = { prefix: rule.prefix, suffix: rule.suffix };
}

async function runHealthCheck(): Promise<void> {
  busy.value = true;
  try {
    health.value = await api.health();
    ElMessage.success(health.value.ok ? "项目健康检查通过" : "发现待处理问题");
  } catch (error) {
    report(error);
  } finally {
    busy.value = false;
  }
}

async function openRestoredProject(path: string): Promise<void> {
  try {
    project.value = await api.openProject(path);
    await Promise.all([refreshUnits(), refreshDepartments(), refreshCategories(), refreshIssueNumber()]);
    await refreshRecent(); // 后端打开时已自动记录
    health.value = null;
  } catch (error) {
    report(error);
  }
}

function projectRenamed(value: ProjectInfo): void {
  project.value = value;
  void refreshRecent(); // 后端重命名时已自动更新记录名称
}

function handleOpenIssue(issueId: number): void {
  void workspace.value?.selectIssueById(issueId);
}

function handleSelectUnit(unitId: number): void {
  workspace.value?.selectUnit(unitId);
}

async function backToProjectList(force = false): Promise<void> {
  if (!force && workspace.value && !(await workspace.value.confirmCurrentLeave())) return;
  project.value = null;
  units.value = [];
  departments.value = [];
  categories.value = [];
  health.value = null;
  projectPath.value = "";
  projectName.value = "";
}

function autoSaveModeChanged(mode: AutoSaveMode): void {
  autoSaveMode.value = mode;
  localStorage.setItem(AUTO_SAVE_KEY, mode);
}

function resetSession(showExpiredMessage = false): void {
  const previousOperator = operator.value;
  api.clearSession();
  operator.value = "";
  if (previousOperator) loginName.value = previousOperator;
  project.value = null;
  units.value = [];
  departments.value = [];
  categories.value = [];
  health.value = null;
  if (showExpiredMessage) ElMessage.warning("本地服务已重启，使用人会话已失效，请重新进入工作台");
}

async function switchOperator(): Promise<void> {
  if (workspace.value && !(await workspace.value.confirmCurrentLeave())) return;
  try {
    await api.logout();
  } catch {
    // 服务已停止或会话已过期时也应允许回到使用人入口。
  } finally {
    resetSession(false);
  }
}

function handleSessionExpired(): void {
  resetSession(true);
}

async function validateStoredSession(): Promise<void> {
  if (!operator.value) return;
  try {
    operator.value = (await api.currentSession()).operator;
  } catch {
    // 无效会话由 API 客户端触发 audit-session-expired；连接失败则保留页面并在后续操作提示。
  }
}

onMounted(() => {
  window.addEventListener("audit-session-expired", handleSessionExpired);
  void validateStoredSession();
  void refreshRecent(); // 会话恢复场景：登录态存在时拉取后端最近列表
});
onBeforeUnmount(() => window.removeEventListener("audit-session-expired", handleSessionExpired));
</script>

<template>
  <main class="app-shell">
    <section v-if="!loggedIn" class="login-card">
      <p class="eyebrow">AUDIT TRAIL 1.1</p>
      <h1>审迹</h1>
      <p>离线专项审计底稿与证据归档工具</p>
      <el-input v-model="loginName" size="large" placeholder="使用人姓名" @keyup.enter="login" />
      <el-button type="primary" size="large" :loading="busy" @click="login">进入工作台</el-button>
      <small>姓名仅用于项目操作留痕，不作为身份认证。</small>
    </section>

    <template v-else>
      <header class="topbar">
        <div><p class="eyebrow">AUDIT TRAIL 1.1</p><h1>{{ project?.project_name || '审计工作台' }}</h1><p v-if="project" class="topbar-path">{{ project.path }}</p></div>
        <div class="operator">
          <el-button v-if="project" size="small" @click="backToProjectList">◀ 返回项目列表</el-button>
          <ProjectOperations v-if="project" :units="units" :departments="departments" :categories="categories" :project-name="project.project_name" :auto-save-mode="autoSaveMode" :issue-number-rule="issueNumberRule" @health-check="runHealthCheck" @data-changed="refreshUnits" @departments-changed="departmentsSaved" @categories-changed="categoriesSaved" @auto-save-mode-changed="autoSaveModeChanged" @project-renamed="projectRenamed" @restored="openRestoredProject" @issue-number-changed="issueNumberChanged" @open-issue="handleOpenIssue" @select-unit="handleSelectUnit" />
          <span class="theme-switch" aria-label="界面主题">
            <button class="theme-dot" :class="{ active: theme === 'dark' }" title="深色" @click="applyTheme('dark')">🌙</button>
            <button class="theme-dot" :class="{ active: theme === 'light' }" title="浅色" @click="applyTheme('light')">☀️</button>
            <button class="theme-dot" :class="{ active: theme === 'green' }" title="护眼绿" @click="applyTheme('green')">🌿</button>
          </span>
          <span>使用人：{{ operator }}</span><el-button text :icon="SwitchButton" @click="switchOperator">切换</el-button>
        </div>
      </header>

      <section v-if="!project" class="project-hub">
        <div class="hub-left">
          <article class="project-card">
            <h2>打开或新建项目</h2>
            <p>项目文件夹内保存数据库、附件、输出和迁移快照；项目整体拷贝即可迁移。新建项目自动加 <code>.auditproj</code> 后缀并隐藏，防人员误删改。</p>
            <el-input v-model="projectPath" placeholder="项目文件夹完整路径">
              <template #append><el-button :icon="FolderOpened" @click="chooseProjectFolder">选择</el-button></template>
            </el-input>
            <el-input v-model="projectName" placeholder="新建项目名称（打开已有项目时可留空）" />
            <div class="actions">
              <el-button :loading="opening" @click="setProject('open')">打开已有项目</el-button>
              <el-button type="primary" :loading="creating" @click="setProject('create')">新建项目</el-button>
            </div>
          </article>
          <article class="project-card">
            <h2>从备份恢复项目</h2>
            <p>选择 <code>.auditbak</code> 备份文件与恢复目标文件夹（必须为空或不存在）。恢复后自动加 <code>.auditproj</code> 后缀并隐藏，与新建项目一致。</p>
            <div class="actions">
              <el-button :loading="restoring" @click="restorePicker?.click()">选择备份文件</el-button>
              <el-button :icon="FolderOpened" :loading="restoring" @click="chooseRestoreTarget">选择恢复文件夹</el-button>
            </div>
            <input ref="restorePicker" class="hidden-input" type="file" accept=".auditbak" @change="inputRestoreFile" />
            <el-input v-model="restoreTarget" placeholder="恢复目标目录（必须为空或不存在）" />
            <span v-if="restoreFile" class="selected-file">备份文件：{{ restoreFile.name }}</span>
            <div class="actions">
              <el-button type="primary" :loading="restoring" @click="restoreFromHub">恢复并打开项目</el-button>
            </div>
          </article>
        </div>
        <aside class="recent-projects panel">
          <div class="panel-head"><div><p class="eyebrow">本机快捷入口</p><h2>最近项目</h2></div><span>{{ recentProjects.length }}/20</span></div>
          <el-empty v-if="!recentProjects.length" description="暂无最近项目" :image-size="62" />
          <div v-for="recent in recentProjects" v-else :key="recent.path" class="recent-project-row"><button class="recent-project-open" :disabled="opening" @click="openRecent(recent)"><span><strong>📁 {{ recent.name }}</strong><small>{{ recent.path }}</small><small>最近打开：{{ formatRecent(recent.time) }}</small></span></button><el-button text type="danger" size="small" title="删除项目文件夹（不可恢复）" @click="deleteProject(recent)">删除</el-button><el-button text size="small" title="仅移除本机快捷记录，不删除项目文件" @click="forgetRecent(recent.path)">移除</el-button></div>
        </aside>
        <p class="migration-note">一页三栏工作方式：问题列表、底稿详情、附件列表；支持按单位/按版块切换，并提供版本回溯、附件全生命周期、Excel 导入导出和项目归档备份。</p>
      </section>

      <template v-else>
        <el-alert v-if="health" class="health-result" :title="health.ok ? '项目健康检查通过' : `健康检查发现 ${health.problems.length} 项待处理问题`" :type="health.ok ? 'success' : 'warning'" :closable="false" show-icon />
        <IssueWorkspace ref="workspace" :units="units" :departments="departments" :categories="categories" :operator="operator" :auto-save-mode="autoSaveMode" :issue-number-rule="issueNumberRule" @units-changed="refreshUnits" />
      </template>
    </template>
  </main>
</template>
