<script setup lang="ts">
import { FolderOpened, SwitchButton } from "@element-plus/icons-vue";
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useRouter } from "vue-router";

import { api, type HealthResult, type ProjectInfo, type Unit } from "./api/client";
import { useRuntimeStore } from "./app/runtimeStore";
import { APP_VERSION_LABEL } from "./version";

// 项目列表/登录页不需要工作区和低频操作面板，打开项目时才加载，缩短首次启动等待。
const IssueWorkspace = defineAsyncComponent(() => import("./components/IssueWorkspace.vue"));
const ProjectOperations = defineAsyncComponent(() => import("./components/ProjectOperations.vue"));
const router = useRouter();
const runtimeStore = useRuntimeStore();

type RecentProject = { path: string; name: string; time: number };
type AutoSaveMode = "realtime" | "5m" | "20m";
const RECENT_KEY = "audit_recent_projects";
const AUTO_SAVE_KEY = "audit_auto_save_mode_v3";
const TAB_LEASE_KEY = "audit_single_tab_lease_v1";
const TAB_LEASE_MS = 30_000;
const TAB_RENEW_MS = 10_000;
type TabLease = { tabId: string; expiresAt: number };
const tabId = typeof crypto.randomUUID === "function"
  ? crypto.randomUUID()
  : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

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
const healthDialogVisible = ref(false);
const busy = ref(false);
const recentProjects = ref<RecentProject[]>(loadRecentLocal());
type Theme = "dark" | "light" | "green" | "paper";
const storedTheme = localStorage.getItem("audit_theme");
const theme = ref<Theme>(
  storedTheme === "light" || storedTheme === "green" || storedTheme === "dark" || storedTheme === "paper"
    ? storedTheme
    : "dark",
);
const storedAutoSaveMode = localStorage.getItem(AUTO_SAVE_KEY);
const autoSaveMode = ref<AutoSaveMode>(
  storedAutoSaveMode === "realtime" || storedAutoSaveMode === "20m" || storedAutoSaveMode === "5m"
    ? storedAutoSaveMode
    : "5m",
);
const workspace = ref<{ confirmCurrentLeave: () => Promise<boolean>; hasUnsavedChanges: () => boolean; selectIssueById: (issueId: number) => Promise<void>; selectUnit: (unitId: number) => void; openExchange: () => Promise<void>; openTemplateDialog: () => Promise<void>; openShortcutSettings: () => void } | null>(null);
const tabBlocked = ref(false);
let tabRenewTimer: ReturnType<typeof window.setInterval> | undefined;
let sessionHeartbeatTimer: ReturnType<typeof window.setInterval> | undefined;

const loggedIn = computed(() => Boolean(operator.value));

// 路由只表达工作台层级，不承载项目正文或附件数据；刷新 /workspace 时也不会
// 擅自恢复项目连接，避免浏览器状态与本机数据库会话出现错配。
watch(project, (current) => {
  const destination = current ? "workspace" : "home";
  runtimeStore.setScreen(destination);
  if (router.currentRoute.value.name !== destination) void router.replace({ name: destination });
}, { immediate: true });

function readTabLease(): TabLease | null {
  try {
    const value = JSON.parse(localStorage.getItem(TAB_LEASE_KEY) ?? "null") as Partial<TabLease> | null;
    return value && typeof value.tabId === "string" && typeof value.expiresAt === "number"
      ? { tabId: value.tabId, expiresAt: value.expiresAt }
      : null;
  } catch {
    return null;
  }
}

function claimTabLease(): boolean {
  const existing = readTabLease();
  if (existing && existing.tabId !== tabId && existing.expiresAt > Date.now()) {
    tabBlocked.value = true;
    return false;
  }
  const mine: TabLease = { tabId, expiresAt: Date.now() + TAB_LEASE_MS };
  localStorage.setItem(TAB_LEASE_KEY, JSON.stringify(mine));
  const verified = readTabLease();
  tabBlocked.value = verified?.tabId !== tabId;
  return !tabBlocked.value;
}

function renewTabLease(): void {
  if (tabBlocked.value) return;
  const existing = readTabLease();
  if (existing && existing.tabId !== tabId && existing.expiresAt > Date.now()) {
    tabBlocked.value = true;
    resetSession(false);
    return;
  }
  localStorage.setItem(TAB_LEASE_KEY, JSON.stringify({ tabId, expiresAt: Date.now() + TAB_LEASE_MS } satisfies TabLease));
}

function releaseTabLease(): void {
  if (readTabLease()?.tabId === tabId) localStorage.removeItem(TAB_LEASE_KEY);
}

function retryTabLease(): void {
  if (claimTabLease()) {
    // F13：防御性清理，避免阻断页重试后叠加定时器
    window.clearInterval(tabRenewTimer);
    window.clearInterval(sessionHeartbeatTimer);
    tabRenewTimer = window.setInterval(renewTabLease, TAB_RENEW_MS);
    sessionHeartbeatTimer = window.setInterval(() => {
      if (loggedIn.value && !tabBlocked.value) void validateStoredSession();
    }, TAB_RENEW_MS);
    void validateStoredSession();
    void refreshRecent();
  }
}

function handleTabLeaseChanged(event: StorageEvent): void {
  if (event.key !== TAB_LEASE_KEY || tabBlocked.value) return;
  const lease = readTabLease();
  if (lease && lease.tabId !== tabId && lease.expiresAt > Date.now()) {
    tabBlocked.value = true;
    resetSession(false);
  }
}

function handlePageHide(event: PageTransitionEvent): void {
  // 进入浏览器后退缓存时页面仍可能恢复，不能提前把写入权交给另一个标签页。
  if (!event.persisted) releaseTabLease();
}

function handleBeforeUnload(event: BeforeUnloadEvent): void {
  // 浏览器关闭/刷新无法等待异步保存确认，只能用原生离开提示保护当前脏底稿。
  if (!workspace.value?.hasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = "";
}

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
  if (tabBlocked.value) return;
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
    const result = await api.chooseFolder();
    if (result.path) projectPath.value = result.path;
    if (result.warning) ElMessage.warning(result.warning);
  } catch (error) {
    report(error);
  }
}

// 初始界面「从备份恢复项目」：选 .auditbak + 目标目录 → 恢复并打开（复用 openRestoredProject）
const restorePicker = ref<HTMLInputElement | null>(null);
const restoreFile = ref<File | null>(null);
const restoreLocalPath = ref("");
const restoreTarget = ref("");
const restoring = ref(false);

async function chooseRestoreTarget(): Promise<void> {
  try {
    const result = await api.chooseFolder();
    if (result.path) restoreTarget.value = result.path;
    if (result.warning) ElMessage.warning(result.warning);
  } catch (error) {
    report(error);
  }
}

function inputRestoreFile(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0];
  restoreFile.value = file ?? null;
}

async function restoreFromHub(): Promise<void> {
  // Element Plus 的 loading 状态需要一次渲染才会反映到按钮；这里先在逻辑层
  // 拦截，避免双击或重复确认发出两次恢复请求。第二次请求会在首个恢复已落盘后
  // 被后端正确地当作“同名项目已存在”，但对使用人来说会误以为本次恢复失败。
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
    return; // 用户取消
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
    healthDialogVisible.value = true;
    ElMessage.success(health.value.ok ? "项目健康检查通过" : `发现 ${health.value.problems.length} 项待处理问题`);
  } catch (error) {
    report(error);
  } finally {
    busy.value = false;
  }
}

async function openRestoredProject(path: string): Promise<boolean> {
  // 恢复已产生新项目，但切换打开前仍须保护当前编辑中的底稿。
  if (workspace.value && !(await workspace.value.confirmCurrentLeave())) return false;
  try {
    project.value = await api.openProject(path);
    await Promise.all([refreshUnits(), refreshDepartments(), refreshCategories(), refreshIssueNumber()]);
    await refreshRecent(); // 后端打开时已自动记录
    health.value = null;
    return true;
  } catch (error) {
    report(error);
    return false;
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

function handleOpenExchange(): void {
  void workspace.value?.openExchange();
}

function handleWorkspaceTool(tool: "templates" | "shortcuts"): void {
  if (tool === "templates") {
    void workspace.value?.openTemplateDialog();
    return;
  }
  workspace.value?.openShortcutSettings();
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
    const session = await api.currentSession();
    operator.value = session.operator;
    // 项目租约被其他窗口接管（体验优化：强制切换）：提示并回到项目列表。
    // 旧窗口的项目连接已被后端吊销，未保存内容无法再落盘，故跳过未保存确认。
    if (session.project_preempted) {
      ElMessage.warning("项目已在其他窗口打开，本窗口已切换到项目列表，请重新打开所需项目");
      await backToProjectList(true);
    }
  } catch {
    // 无效会话由 API 客户端触发 audit-session-expired；连接失败则保留页面并在后续操作提示。
  }
}

onMounted(() => {
  window.addEventListener("audit-session-expired", handleSessionExpired);
  window.addEventListener("storage", handleTabLeaseChanged);
  window.addEventListener("pagehide", handlePageHide);
  window.addEventListener("beforeunload", handleBeforeUnload);
  if (claimTabLease()) {
    tabRenewTimer = window.setInterval(renewTabLease, TAB_RENEW_MS);
    sessionHeartbeatTimer = window.setInterval(() => {
      if (loggedIn.value && !tabBlocked.value) void validateStoredSession();
    }, TAB_RENEW_MS);
    void validateStoredSession();
    void refreshRecent(); // 会话恢复场景：登录态存在时拉取后端最近列表
  }
});
onBeforeUnmount(() => {
  window.removeEventListener("audit-session-expired", handleSessionExpired);
  window.removeEventListener("storage", handleTabLeaseChanged);
  window.removeEventListener("pagehide", handlePageHide);
  window.removeEventListener("beforeunload", handleBeforeUnload);
  if (tabRenewTimer) window.clearInterval(tabRenewTimer);
  if (sessionHeartbeatTimer) window.clearInterval(sessionHeartbeatTimer);
  releaseTabLease();
});
</script>

<template>
  <main class="app-shell">
    <section v-if="tabBlocked" class="login-card">
      <p class="eyebrow">AUDIT TRAIL {{ APP_VERSION_LABEL }}</p>
      <h1>工作台已打开</h1>
      <p>为避免同一项目发生并发写入，本工具一次只允许一个标签页工作。</p>
      <el-button type="primary" size="large" @click="retryTabLease">重新检查</el-button>
      <small>请返回已打开的审迹页面；关闭该页面后，等待几秒再重新检查。</small>
    </section>

    <section v-else-if="!loggedIn" class="login-card">
      <p class="eyebrow">AUDIT TRAIL {{ APP_VERSION_LABEL }}</p>
      <h1>审迹</h1>
      <p>离线专项审计底稿与证据归档工具</p>
      <el-input v-model="loginName" size="large" placeholder="使用人姓名" @keyup.enter="login" />
      <el-button type="primary" size="large" :loading="busy" @click="login">进入工作台</el-button>
      <small>姓名仅用于项目操作留痕，不作为身份认证。</small>
    </section>

    <template v-else>
      <header class="topbar">
        <div><p class="eyebrow">AUDIT TRAIL {{ APP_VERSION_LABEL }}</p><h1>{{ project?.project_name || '审迹' }}</h1><p v-if="project" class="topbar-path">{{ project.path }}</p></div>
        <div class="operator">
          <el-button v-if="project" size="small" @click="backToProjectList">◀ 返回项目列表</el-button>
          <ProjectOperations v-if="project" :units="units" :departments="departments" :categories="categories" :project-name="project.project_name" :auto-save-mode="autoSaveMode" :issue-number-rule="issueNumberRule" @health-check="runHealthCheck" @data-changed="refreshUnits" @departments-changed="departmentsSaved" @categories-changed="categoriesSaved" @auto-save-mode-changed="autoSaveModeChanged" @project-renamed="projectRenamed" @restored="openRestoredProject" @issue-number-changed="issueNumberChanged" @open-issue="handleOpenIssue" @select-unit="handleSelectUnit" @open-exchange="handleOpenExchange" @open-workspace-tool="handleWorkspaceTool" />
          <span class="theme-switch" aria-label="界面主题">
            <button class="theme-dot" :class="{ active: theme === 'dark' }" title="深色" @click="applyTheme('dark')">🌙</button>
            <button class="theme-dot" :class="{ active: theme === 'light' }" title="浅色" @click="applyTheme('light')">☀️</button>
            <button class="theme-dot" :class="{ active: theme === 'green' }" title="护眼绿" @click="applyTheme('green')">🌿</button>
            <button class="theme-dot" :class="{ active: theme === 'paper' }" title="纸质书" @click="applyTheme('paper')">📖</button>
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
            <el-input v-model="restoreLocalPath" placeholder="或输入本机 .auditbak 文件完整路径（大于 800MB 时请用此方式）" />
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
        <el-alert v-if="health" class="health-result health-result-clickable" :title="health.ok ? '项目健康检查通过（点击查看详情）' : `健康检查发现 ${health.problems.length} 项待处理问题（点击查看详情）`" :type="health.ok ? 'success' : 'warning'" :closable="false" show-icon @click="healthDialogVisible = true" />
        <IssueWorkspace ref="workspace" :units="units" :departments="departments" :categories="categories" :operator="operator" :auto-save-mode="autoSaveMode" :issue-number-rule="issueNumberRule" @units-changed="refreshUnits" />
      </template>
    </template>

    <el-dialog v-model="healthDialogVisible" :title="health?.ok ? '项目健康检查：通过' : '项目健康检查：待处理问题'" width="min(820px, calc(100vw - 32px))" append-to-body>
      <template v-if="health">
        <p class="health-dialog-summary">检查时间：{{ health.checked_at }}。单位 {{ health.counts.units ?? 0 }} 个、底稿 {{ health.counts.issues ?? 0 }} 条、附件 {{ health.counts.files ?? 0 }} 个；普通附件哈希核验 {{ health.sample?.checked ?? 0 }}/{{ health.sample?.total ?? 0 }}。</p>
        <el-empty v-if="health.ok" description="未发现数据或证据完整性问题，可继续正常工作。" :image-size="62" />
        <div v-else class="health-problem-list"><article v-for="(problem, index) in health.problems" :key="`${problem.type}-${problem.message}-${index}`" class="health-problem" :class="problem.severity === 'P0' ? 'p0' : 'p1'"><el-tag size="small" :type="problem.severity === 'P0' ? 'danger' : 'warning'">{{ problem.severity }}</el-tag><div><strong>{{ problem.type }}</strong><p>{{ problem.message }}</p></div></article></div>
        <p v-if="!health.ok" class="health-dialog-hint">P0 表示数据或证据完整性风险：请先恢复缺失附件、核实篡改或处理异常关联，再进行归档、交接或合并。</p>
      </template>
    </el-dialog>
  </main>
</template>
