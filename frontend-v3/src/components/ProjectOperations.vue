<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type AmountSettings, type ArchivePreflight, type AuditLog, type BackupSettings, type ImportResult, type MergePreflight, type MergeResult, type ProjectInfo, type ProjectSummary, type RecycledFile, type RecycledIssue, type RecycledIssuePreview, type RecycledUnit, type RecoveryPoint, type ScanStatus, type SearchResult, type SummaryIssue, type Unit } from "../api/client";
import { formatIssueNo } from "../format";

type AutoSaveMode = "realtime" | "5m" | "20m";

const props = defineProps<{
  units: Unit[];
  departments: string[];
  categories: string[];
  projectName: string;
  autoSaveMode: AutoSaveMode;
  issueNumberRule: { prefix: string; suffix: string };
}>();
const emit = defineEmits<{
  dataChanged: [];
  restored: [path: string];
  healthCheck: [];
  departmentsChanged: [departments: string[]];
  categoriesChanged: [categories: string[]];
  autoSaveModeChanged: [mode: AutoSaveMode];
  projectRenamed: [project: ProjectInfo];
  issueNumberChanged: [rule: { prefix: string; suffix: string }];
  openIssue: [issueId: number];
  selectUnit: [unitId: number];
  openExchange: [];
}>();

type Panel = "" | "import" | "export" | "package" | "backup" | "merge" | "restore" | "settings" | "summary" | "logs" | "scan" | "rename" | "search" | "recycle";

const activePanel = ref<Panel>("");
const working = ref(false);
const importPicker = ref<HTMLInputElement | null>(null);
const mergePicker = ref<HTMLInputElement | null>(null);
const restorePicker = ref<HTMLInputElement | null>(null);
const importFile = ref<File | null>(null);
const mergeFiles = ref<File[]>([]);
const mergeLocalPaths = ref("");
const mergePreflight = ref<MergePreflight | null>(null);
const restoreFile = ref<File | null>(null);
const restoreLocalPath = ref("");
const restoreTarget = ref("");
const importResult = ref<ImportResult | null>(null);
const mergeResult = ref<MergeResult | null>(null);
const exportScope = ref<"project" | "unit">("project");
const exportUnitId = ref<number | null>(null);
const packageScope = ref<"all" | "selected">("all");
const packageUnitIds = ref<number[]>([]);
const groupByDepartment = ref(false);
const archivePreflight = ref<ArchivePreflight | null>(null);
const departmentName = ref("");
const departmentDraft = ref<string[]>([]);
const categoryName = ref("");
const categoryDraft = ref<string[]>([]);
const issuePrefix = ref("");
const issueSuffix = ref("");
const summary = ref<ProjectSummary | null>(null);
const logs = ref<AuditLog[]>([]);
const recycledIssues = ref<RecycledIssue[]>([]);
const recycledUnits = ref<RecycledUnit[]>([]);
const recycledFiles = ref<RecycledFile[]>([]);
const selectedRecycleIds = ref<number[]>([]);
const recycledPreview = ref<RecycledIssuePreview | null>(null);
const recyclePreviewVisible = ref(false);
const backupSettings = ref<BackupSettings | null>(null);
const amountSettings = ref<AmountSettings | null>(null);
const amountDefaultCurrency = ref("CNY");
const amountDefaultUnit = ref("元");
const backupEnabled = ref(false);
const backupTargetDir = ref("");
const backupIntervalMinutes = ref(360);
const backupRetentionDays = ref(7);
const backupMaxGiB = ref(100);
const recoveryPoints = ref<RecoveryPoint[]>([]);
const selectedRecoveryPointId = ref("");
const scan = ref<ScanStatus | null>(null);
const projectNameDraft = ref("");
let scanTimer: ReturnType<typeof window.setTimeout> | undefined;

// ── 全局搜索状态 ──
const searchQuery = ref("");
const searchResult = ref<SearchResult | null>(null);
const searchLoading = ref(false);
let searchTimer: ReturnType<typeof window.setTimeout> | undefined;

// ── 问题清单筛选状态（summary 面板）──
const summaryUnitFilter = ref<number | null>(null);
const summaryStatusFilter = ref("");
const summaryDepartmentFilter = ref("");

function openSearch(): void {
  show("search");
  if (searchQuery.value.trim()) void runSearch();
}

async function runSearch(): Promise<void> {
  const q = searchQuery.value.trim();
  if (!q) {
    searchResult.value = null;
    return;
  }
  searchLoading.value = true;
  try {
    searchResult.value = await api.search(q);
  } catch (error) {
    report(error);
  } finally {
    searchLoading.value = false;
  }
}

function onSearchInput(): void {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => { void runSearch(); }, 300);
}

function formatAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN") : String(value);
}

// F9 修复：金额单元格保留两位小数（"120.00" 不再显示为 "120"）；字节数仍用 formatAmount
function formatMoney(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n)
    ? n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : String(value);
}

function issueLabel(issue: SummaryIssue): string {
  return `${formatIssueNo(issue.seq, props.issueNumberRule)} ${issue.defect_type || "未命名底稿"}`;
}

function goIssue(issueId: number): void {
  activePanel.value = "";
  emit("openIssue", issueId);
}

function goUnit(unitId: number): void {
  activePanel.value = "";
  emit("selectUnit", unitId);
}

const summaryIssues = computed(() => {
  if (!summary.value) return [];
  return summary.value.issues.filter((issue) => {
    if (summaryUnitFilter.value !== null && issue.unit_id !== summaryUnitFilter.value) return false;
    if (summaryStatusFilter.value && issue.status !== summaryStatusFilter.value) return false;
    if (summaryDepartmentFilter.value !== "" && (issue.department || "") !== summaryDepartmentFilter.value) return false;
    return true;
  });
});

const summaryAmountGroups = computed(() => {
  const groups = new Map<string, bigint>();
  let unstructured = 0;
  for (const issue of summaryIssues.value) {
    const minor = issue.amount_minor;
    if (minor === null || !Number.isSafeInteger(minor)) {
      if (issue.amount.trim()) unstructured += 1;
      continue;
    }
    const key = `${issue.currency || "CNY"} ${issue.amount_unit || "元"}`;
    groups.set(key, (groups.get(key) ?? 0n) + BigInt(minor));
  }
  const formatMinor = (value: bigint): string => {
    const sign = value < 0n ? "-" : "";
    const absolute = value < 0n ? -value : value;
    return `${sign}${absolute / 100n}.${(absolute % 100n).toString().padStart(2, "0")}`;
  };
  return { groups: [...groups.entries()].map(([label, value]) => `${label} ${formatMinor(value)}`), unstructured };
});

const summaryUnitsCount = computed(() => {
  return new Set(summaryIssues.value.map((issue) => issue.unit_id)).size;
});

const summaryStatuses = computed(() => {
  if (!summary.value) return [];
  return Array.from(new Set(summary.value.issues.map((issue) => issue.status || "草稿")));
});

const summaryDepartments = computed(() => {
  if (!summary.value) return [];
  return Array.from(new Set(summary.value.issues.map((issue) => issue.department || "")));
});

const selectedPackageCount = computed(() => packageScope.value === "all" ? props.units.length : packageUnitIds.value.length);
const dialogTitles: Partial<Record<Panel, string>> = {
  import: "导入问题汇总（Excel）",
  export: "导出问题汇总表（Excel）",
  package: "一键归档打包（ZIP）",
  backup: "创建项目备份",
  merge: "合并导入（.auditbak）",
  restore: "导入备份（恢复项目）",
  settings: "编制与预设设置",
  summary: "问题清单视图",
  logs: "操作日志（随项目保存）",
  recycle: "问题回收站",
  scan: "附件完整性扫描",
  rename: "重命名项目",
  search: "全局搜索",
};
const dialogVisible = computed({
  get: () => Boolean(activePanel.value),
  set: (visible: boolean) => { if (!visible) activePanel.value = ""; },
});
const dialogTitle = computed(() => activePanel.value ? dialogTitles[activePanel.value] ?? "操作" : "");
const dialogWidth = computed(() => activePanel.value === "summary"
  ? "min(980px, calc(100vw - 32px))"
  : activePanel.value === "search"
    ? "min(720px, calc(100vw - 32px))"
    : "min(560px, calc(100vw - 32px))");
function report(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : "项目操作失败，请重试");
}

function show(panel: Panel | string): void {
  const target = panel as Panel;
  activePanel.value = target;
  if (target === "import") importResult.value = null;
  if (target === "merge") mergeResult.value = null;
  if (target === "export" && !exportUnitId.value) exportUnitId.value = props.units[0]?.id ?? null;
  if (target === "package" && !packageUnitIds.value.length) packageUnitIds.value = props.units.map((unit) => unit.id);
  if (target === "restore") void loadRecoveryPoints();
}

function command(value: string): void {
  if (value === "health") {
    emit("healthCheck");
    return;
  }
  if (value === "settings") { openSettings(); return; }
  if (value === "summary") { void openSummary(); return; }
  if (value === "logs") { void openLogs(); return; }
  if (value === "recycle") { void openRecycle(); return; }
  if (value === "scan") { void startScan(); return; }
  if (value === "rename") { openRename(); return; }
  if (value === "restart") { void restartProgram(); return; }
  if (value === "quit") { void quitProgram(); return; }
  if (value === "reset") { void resetProject(); return; }
  show(value);
}

function openSettings(): void {
  departmentDraft.value = [...props.departments];
  categoryDraft.value = [...props.categories];
  departmentName.value = "";
  categoryName.value = "";
  issuePrefix.value = props.issueNumberRule.prefix;
  issueSuffix.value = props.issueNumberRule.suffix;
  show("settings");
  void loadBackupSettings();
  void loadAmountSettings();
}

async function loadAmountSettings(): Promise<void> {
  try {
    amountSettings.value = await api.amountSettings();
    amountDefaultCurrency.value = amountSettings.value.currency;
    amountDefaultUnit.value = amountSettings.value.amount_unit;
  } catch (error) {
    report(error);
  }
}

async function saveAmountSettings(): Promise<void> {
  working.value = true;
  try {
    amountSettings.value = await api.saveAmountSettings({
      currency: amountDefaultCurrency.value,
      amount_unit: amountDefaultUnit.value,
    });
    amountDefaultCurrency.value = amountSettings.value.currency;
    amountDefaultUnit.value = amountSettings.value.amount_unit;
    ElMessage.success("新建底稿的默认金额口径已保存；历史金额不会被改写");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function loadBackupSettings(): Promise<void> {
  try {
    const settings = await api.backupSettings();
    backupSettings.value = settings;
    backupEnabled.value = settings.enabled;
    backupTargetDir.value = settings.target_dir;
    backupIntervalMinutes.value = settings.interval_minutes;
    backupRetentionDays.value = settings.retention_days;
    backupMaxGiB.value = Math.max(1, Math.round(settings.max_bytes / 1024 / 1024 / 1024));
    await loadRecoveryPoints();
  } catch (error) {
    report(error);
  }
}

async function loadRecoveryPoints(): Promise<void> {
  try {
    recoveryPoints.value = await api.recoveryPoints();
    if (!recoveryPoints.value.some((point) => point.id === selectedRecoveryPointId.value)) {
      selectedRecoveryPointId.value = recoveryPoints.value[0]?.id ?? "";
    }
  } catch (error) {
    report(error);
  }
}

async function chooseBackupTarget(): Promise<void> {
  try {
    const result = await api.chooseFolder();
    if (result.path) backupTargetDir.value = result.path;
    if (result.warning) ElMessage.warning(result.warning);
  } catch (error) {
    report(error);
  }
}

async function onBackupEnabledChanged(): Promise<void> {
  // 开启即请求目录：若系统选择器不可用，输入框仍保留供用户粘贴完整路径。
  if (!backupEnabled.value || backupTargetDir.value.trim()) return;
  await chooseBackupTarget();
  if (!backupTargetDir.value.trim()) {
    ElMessage.info("请在下方输入自动备份目标目录的完整路径后再保存");
  }
}

async function saveBackupPolicy(): Promise<void> {
  working.value = true;
  try {
    backupSettings.value = await api.saveBackupSettings({
      enabled: backupEnabled.value,
      target_dir: backupTargetDir.value.trim(),
      interval_minutes: backupIntervalMinutes.value,
      retention_days: backupRetentionDays.value,
      max_bytes: backupMaxGiB.value * 1024 * 1024 * 1024,
    });
    if (!backupEnabled.value) recoveryPoints.value = [];
    ElMessage.success(backupEnabled.value ? "自动备份策略已保存" : "自动备份已关闭");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function createRecoveryPoint(): Promise<void> {
  working.value = true;
  try {
    const job = await api.createRecoveryPoint();
    ElMessage.success(`已开始创建增量恢复点（任务 ${job.job_id.slice(0, 8)}），完成后刷新恢复点列表查看`);
    await loadBackupSettings();
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

function changeAutoSaveMode(mode: AutoSaveMode): void {
  emit("autoSaveModeChanged", mode);
  const label = mode === "realtime" ? "输入停止后实时保存" : mode === "20m" ? "每 20 分钟保存" : "每 5 分钟保存";
  ElMessage.success(`自动保存已设为“${label}”`);
}

async function saveDepartments(): Promise<void> {
  departmentDraft.value = await api.saveDepartments(departmentDraft.value);
  emit("departmentsChanged", departmentDraft.value);
}

async function addDepartment(): Promise<void> {
  const name = departmentName.value.trim();
  if (!name) { ElMessage.warning("请输入版块名称"); return; }
  if (departmentDraft.value.includes(name)) { ElMessage.warning("版块已存在"); return; }
  const previous = [...departmentDraft.value];
  departmentDraft.value = [...previous, name];
  try {
    await saveDepartments();
    departmentName.value = "";
    ElMessage.success(`已添加版块“${name}”`);
  } catch (error) {
    departmentDraft.value = previous;
    report(error);
  }
}

async function removeDepartment(name: string): Promise<void> {
  try {
    await ElMessageBox.confirm(`从预设中移除“${name}”？已有底稿不会被修改。`, "移除版块预设", {
      type: "warning", confirmButtonText: "移除", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  const previous = [...departmentDraft.value];
  departmentDraft.value = previous.filter((item) => item !== name);
  try {
    await saveDepartments();
    ElMessage.success("版块预设已移除");
  } catch (error) {
    departmentDraft.value = previous;
    report(error);
  }
}

async function saveCategories(): Promise<void> {
  categoryDraft.value = await api.saveCategories(categoryDraft.value);
  emit("categoriesChanged", categoryDraft.value);
}

async function addCategory(): Promise<void> {
  const name = categoryName.value.trim();
  if (!name) { ElMessage.warning("请输入问题分类"); return; }
  if (categoryDraft.value.includes(name)) { ElMessage.warning("问题分类已存在"); return; }
  const previous = [...categoryDraft.value];
  categoryDraft.value = [...previous, name];
  try {
    await saveCategories();
    categoryName.value = "";
    ElMessage.success(`已添加问题分类“${name}”`);
  } catch (error) {
    categoryDraft.value = previous;
    report(error);
  }
}

async function removeCategory(name: string): Promise<void> {
  try {
    await ElMessageBox.confirm(`从预设中移除“${name}”？已有底稿不会被修改。`, "移除问题分类预设", {
      type: "warning", confirmButtonText: "移除", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  const previous = [...categoryDraft.value];
  categoryDraft.value = previous.filter((item) => item !== name);
  try {
    await saveCategories();
    ElMessage.success("问题分类预设已移除");
  } catch (error) {
    categoryDraft.value = previous;
    report(error);
  }
}

async function saveIssueNumber(): Promise<void> {
  const prefix = issuePrefix.value.trim();
  const suffix = issueSuffix.value.trim();
  working.value = true;
  try {
    const saved = await api.saveIssueNumber(prefix, suffix);
    emit("issueNumberChanged", saved);
    ElMessage.success(`底稿编号规则已保存：${saved.prefix || "（无前缀）"}序号${saved.suffix || "（无后缀）"}`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function openSummary(): Promise<void> {
  show("summary");
  summary.value = null;
  try {
    summary.value = await api.summary();
  } catch (error) {
    report(error);
  }
}

async function openLogs(): Promise<void> {
  show("logs");
  logs.value = [];
  try {
    logs.value = await api.logs();
  } catch (error) {
    report(error);
  }
}

async function openRecycle(): Promise<void> {
  show("recycle");
  recycledIssues.value = [];
  recycledUnits.value = [];
  recycledFiles.value = [];
  selectedRecycleIds.value = [];
  try {
    const [issues, units, files] = await Promise.all([api.recycledIssues(), api.recycledUnits(), api.recycledFiles()]);
    recycledIssues.value = issues;
    recycledUnits.value = units;
    recycledFiles.value = files;
  } catch (error) {
    report(error);
  }
}

const selectedRecycledIssues = computed(() => recycledIssues.value.filter((item) => selectedRecycleIds.value.includes(item.recycle_id)));
const recycleAllSelected = computed(() => recycledIssues.value.length > 0 && selectedRecycleIds.value.length === recycledIssues.value.length);

function toggleAllRecycledIssues(checked: boolean): void {
  selectedRecycleIds.value = checked ? recycledIssues.value.map((item) => item.recycle_id) : [];
}

function toggleRecycledIssue(recycleId: number, checked: boolean): void {
  selectedRecycleIds.value = checked
    ? [...new Set([...selectedRecycleIds.value, recycleId])]
    : selectedRecycleIds.value.filter((id) => id !== recycleId);
}

async function restoreRecycledIssues(): Promise<void> {
  const selected = [...selectedRecycledIssues.value];
  if (!selected.length) {
    ElMessage.warning("请先勾选需要恢复的底稿");
    return;
  }
  working.value = true;
  try {
    const results = await Promise.allSettled(selected.map((issue) => api.restoreRecycledIssue(issue.recycle_id)));
    const succeeded = results.filter((item) => item.status === "fulfilled").length;
    const failed = results.length - succeeded;
    await openRecycle();
    emit("dataChanged");
    if (failed) ElMessage.warning(`已恢复 ${succeeded} 条，${failed} 条失败，请刷新后重试`);
    else ElMessage.success(`已恢复 ${succeeded} 条；如原编号已被复用，系统已自动换号`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function previewRecycledIssue(issue: RecycledIssue): Promise<void> {
  try {
    recycledPreview.value = await api.recycledIssuePreview(issue.recycle_id);
    recyclePreviewVisible.value = true;
  } catch (error) {
    report(error);
  }
}

async function purgeRecycledIssues(): Promise<void> {
  const selected = [...selectedRecycledIssues.value];
  if (!selected.length) {
    ElMessage.warning("请先勾选需要物理删除的底稿");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `将物理删除已勾选的 ${selected.length} 条底稿及其版本和关联记录，不能恢复；永久操作日志会保留。`,
      "清空回收站（不可恢复）",
      { type: "warning", confirmButtonText: "物理删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  working.value = true;
  try {
    const results = await Promise.allSettled(selected.map((issue) => api.purgeRecycledIssue(issue.recycle_id)));
    const succeeded = results.filter((item) => item.status === "fulfilled").length;
    const failed = results.length - succeeded;
    await openRecycle();
    if (failed) ElMessage.warning(`已物理删除 ${succeeded} 条，${failed} 条失败，请刷新后重试`);
    else ElMessage.success(`已物理删除 ${succeeded} 条，操作日志已保留`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function restoreRecycledUnit(item: RecycledUnit): Promise<void> {
  working.value = true;
  try {
    await api.restoreRecycledUnit(item.recycle_id);
    await openRecycle();
    emit("dataChanged");
    ElMessage.success(`已恢复单位“${item.name}”`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function purgeRecycledUnit(item: RecycledUnit): Promise<void> {
  try {
    await ElMessageBox.confirm(`将物理删除单位“${item.name}”及其底稿、版本和附件，不能恢复。`, "清空回收站（不可恢复）", { type: "warning", confirmButtonText: "物理删除", cancelButtonText: "取消" });
  } catch {
    return;
  }
  working.value = true;
  try {
    await api.purgeRecycledUnit(item.recycle_id);
    await openRecycle();
    ElMessage.success(`已物理删除单位“${item.name}”`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function restoreRecycledFile(item: RecycledFile): Promise<void> {
  working.value = true;
  try {
    await api.restoreRecycledFile(item.recycle_id);
    await openRecycle();
    emit("dataChanged");
    ElMessage.success(`已恢复附件“${item.orig_name}”`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function purgeRecycledFile(item: RecycledFile): Promise<void> {
  try {
    await ElMessageBox.confirm(`将物理删除附件“${item.orig_name}”，不能恢复。`, "清空回收站（不可恢复）", { type: "warning", confirmButtonText: "物理删除", cancelButtonText: "取消" });
  } catch {
    return;
  }
  working.value = true;
  try {
    await api.purgeRecycledFile(item.recycle_id);
    await openRecycle();
    ElMessage.success(`已物理删除附件“${item.orig_name}”`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

function scanPercent(): number {
  if (!scan.value?.total) return scan.value?.phase === "hash" ? 0 : 10;
  return Math.min(99, Math.round((scan.value.done / scan.value.total) * 100));
}

function scanPhaseText(): string {
  if (!scan.value) return "准备扫描…";
  if (scan.value.phase === "phys") return `扫描附件库文件… ${scan.value.done}/${scan.value.total}`;
  if (scan.value.phase === "folder_hash") return `核对文件夹证据摘要… ${scan.value.done}/${scan.value.total}`;
  if (scan.value.phase === "hash") return `核对文件哈希… ${scan.value.done}/${scan.value.total}`;
  return "检查数据完整性…";
}

async function pollScan(): Promise<void> {
  if (!scan.value?.scan_id) return;
  try {
    scan.value = await api.scanStatus(scan.value.scan_id);
    if (scan.value.status === "queued" || scan.value.status === "running") {
      scanTimer = window.setTimeout(() => { void pollScan(); }, 800);
    }
  } catch (error) {
    scan.value = scan.value ? { ...scan.value, status: "error", error: error instanceof Error ? error.message : "读取扫描进度失败" } : null;
  }
}

async function startScan(): Promise<void> {
  window.clearTimeout(scanTimer);
  show("scan");
  scan.value = { scan_id: "", status: "queued", phase: "db", done: 0, total: 0, problems: [], counts: {}, sample: { checked: 0, total: 0 }, error: "" };
  try {
    const job = await api.startFullScan();
    scan.value = { ...scan.value, scan_id: job.scan_id, status: "running" };
    await pollScan();
  } catch (error) {
    scan.value = scan.value ? { ...scan.value, status: "error", error: error instanceof Error ? error.message : "启动扫描失败" } : null;
  }
}

async function cancelScan(): Promise<void> {
  if (!scan.value?.scan_id) return;
  try {
    await api.cancelScan(scan.value.scan_id);
    ElMessage.info("正在取消扫描…");
  } catch (error) {
    report(error);
  }
}

function openRename(): void {
  projectNameDraft.value = props.projectName;
  show("rename");
}

async function renameProject(): Promise<void> {
  const name = projectNameDraft.value.trim();
  if (!name) { ElMessage.warning("项目名称不能为空"); return; }
  working.value = true;
  try {
    emit("projectRenamed", await api.renameProject(name));
    activePanel.value = "";
    ElMessage.success("项目已重命名");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

onBeforeUnmount(() => window.clearTimeout(scanTimer));

function inputFile(event: Event, target: "import" | "restore"): void {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null;
  if (target === "import") importFile.value = file;
  else restoreFile.value = file;
}

function inputMergeFiles(event: Event): void {
  mergeFiles.value = Array.from((event.target as HTMLInputElement).files ?? []);
}

function downloadText(filename: string, content: string): void {
  const objectUrl = URL.createObjectURL(new Blob(["\uFEFF", content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

function reportStamp(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function downloadImportReport(): void {
  if (!importResult.value) return;
  const result = importResult.value;
  const lines = [
    "审迹 Excel 导入报告",
    `成功导入：${result.imported}`, `跳过：${result.skipped}`, `新建单位：${result.new_units}`,
    "", "错误/提示明细：", ...(result.errors.length ? result.errors : ["无"]),
  ];
  downloadText(`Excel导入报告_${reportStamp()}.txt`, lines.join("\n"));
}

function downloadMergeReport(): void {
  if (!mergeResult.value) return;
  const result = mergeResult.value;
  const lines = [
    "审迹 备份合并报告",
    `新增单位：${result.units}`, `新增底稿：${result.issues}`, `迁移版本：${result.versions}`, `新增附件：${result.files}`, `新增文件夹：${result.folders}`, `新增版块预设：${result.depts}`,
    "", "冲突/处理提示：", ...(result.conflicts.length ? result.conflicts.map((item) => `[${item.type}] ${item.message}`) : ["无"]),
    "", "错误：", ...(result.errors.length ? result.errors : ["无"]),
  ];
  downloadText(`备份合并报告_${reportStamp()}.txt`, lines.join("\n"));
}

function togglePackageUnit(id: number, checked: boolean): void {
  packageUnitIds.value = checked
    ? [...new Set([...packageUnitIds.value, id])]
    : packageUnitIds.value.filter((item) => item !== id);
}

async function downloadTemplate(): Promise<void> {
  working.value = true;
  try {
    await api.importTemplate();
    ElMessage.success("导入模板已下载，请填写后再导入");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function importExcel(): Promise<void> {
  if (!importFile.value) {
    ElMessage.warning("请选择按模板填写的 .xlsx 文件");
    return;
  }
  working.value = true;
  try {
    importResult.value = await api.importExcel(importFile.value);
    emit("dataChanged");
    ElMessage.success(`导入完成：${importResult.value.imported} 条底稿${importResult.value.skipped ? `，跳过 ${importResult.value.skipped} 条` : ""}`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function exportExcel(): Promise<void> {
  if (exportScope.value === "unit" && !exportUnitId.value) {
    ElMessage.warning("请选择要导出的被审计单位");
    return;
  }
  working.value = true;
  try {
    const result = await api.exportExcel(exportScope.value, exportUnitId.value ?? undefined);
    await api.downloadUrl(result.download_url, result.filename);
    ElMessage.success(`已导出 ${result.count} 条问题汇总`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function packageProject(): Promise<void> {
  if (!archivePreflight.value?.confirmation_token) {
    await prepareArchivePreflight();
    return;
  }
  if (packageScope.value === "selected" && !packageUnitIds.value.length) {
    ElMessage.warning("请至少勾选一个被审计单位");
    return;
  }
  working.value = true;
  try {
    const result = await api.packageProject(
      packageScope.value === "selected" ? packageUnitIds.value : [],
      groupByDepartment.value,
      archivePreflight.value.confirmation_token,
    );
    await api.downloadUrl(result.download_url, result.filename);
    archivePreflight.value = null;
    ElMessage.success(`归档包已生成：${result.units} 个单位、${result.issues} 条底稿`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

function clearArchivePreflight(): void {
  archivePreflight.value = null;
}

async function prepareArchivePreflight(): Promise<void> {
  if (packageScope.value === "selected" && !packageUnitIds.value.length) {
    ElMessage.warning("请至少勾选一个被审计单位");
    return;
  }
  working.value = true;
  try {
    archivePreflight.value = await api.packagePreflight(
      packageScope.value === "selected" ? packageUnitIds.value : [], groupByDepartment.value,
    );
    if (archivePreflight.value.blockers.length) {
      ElMessage.error(`归档已阻止：${archivePreflight.value.blockers.length} 项问题需要处理`);
    } else {
      ElMessage.success("归档核对完成，请确认清单后生成归档包");
    }
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function backupProject(): Promise<void> {
  try {
    await ElMessageBox.confirm("将创建数据库与附件库的一致性备份（.auditbak），保存到项目文件夹的上级目录。", "创建项目备份", {
      type: "warning", confirmButtonText: "创建备份", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  working.value = true;
  try {
    const result = await api.createBackup();
    ElMessage.success(`项目备份已创建：${result.filename}`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function mergeBackups(): Promise<void> {
  const localPaths = mergeLocalPaths.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (!localPaths.length) {
    ElMessage.warning("请逐行输入本机 .auditbak 备份完整路径");
    return;
  }
  if (!mergePreflight.value?.confirmation_token) {
    await prepareMergePreflight();
    return;
  }
  try {
    await ElMessageBox.confirm("冲突将按清单所示“并存保留”处理，合并会向当前项目写入单位、底稿和附件。请先创建完整备份。", "确认合并", {
      type: "warning", confirmButtonText: "确认并合并", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  working.value = true;
  try {
    const result = await api.mergeLocalBackupsConfirmed(localPaths, mergePreflight.value.confirmation_token);
    mergeResult.value = result;
    mergePreflight.value = null;
    emit("dataChanged");
    ElMessage.success(`合并完成：${result.issues} 条底稿、${result.files} 个附件${result.conflicts.length ? `，${result.conflicts.length} 项冲突提示` : ""}`);
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

function clearMergePreflight(): void {
  mergePreflight.value = null;
}

async function prepareMergePreflight(): Promise<void> {
  const localPaths = mergeLocalPaths.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (!localPaths.length) {
    ElMessage.warning("请逐行输入本机 .auditbak 备份完整路径");
    return;
  }
  working.value = true;
  try {
    mergePreflight.value = await api.mergeLocalPreflight(localPaths);
    if (mergePreflight.value.blockers.length) {
      ElMessage.error(`合并已阻止：${mergePreflight.value.blockers.length} 个来源需要处理`);
    } else if (mergePreflight.value.conflicts.length) {
      ElMessage.warning(`发现 ${mergePreflight.value.conflicts.length} 项冲突，请核对并确认并存处理方式`);
    } else {
      ElMessage.success("合并预检通过，可确认执行合并");
    }
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function chooseRestoreTarget(): Promise<void> {
  try {
    const result = await api.chooseFolder();
    if (result.path) restoreTarget.value = result.path;
  } catch (error) {
    report(error);
  }
}

async function restoreBackup(): Promise<void> {
  // 在 DOM 来得及将 loading 写回按钮前先阻断重复触发，防止首次恢复已成功、
  // 第二个请求却因为同名 .auditproj 已存在而向使用人报错。
  if (working.value) return;
  const localPath = restoreLocalPath.value.trim();
  if (!restoreFile.value && !localPath) {
    ElMessage.warning("请选择 .auditbak 文件，或输入本机备份文件完整路径");
    return;
  }
  if (!restoreTarget.value.trim()) {
    ElMessage.warning("请选择空的目标文件夹，或输入一个新的项目路径");
    return;
  }
  try {
    await ElMessageBox.confirm("恢复会写入目标文件夹；目标必须为空或不存在。当前项目不会被覆盖。", "恢复项目备份", {
      type: "warning", confirmButtonText: "恢复并打开", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  if (working.value) return;
  working.value = true;
  try {
    const result = localPath
      ? await api.restoreLocalBackup(localPath, restoreTarget.value.trim())
      : await api.restoreBackup(restoreFile.value!, restoreTarget.value.trim());
    emit("restored", result.path);
    activePanel.value = "";
    ElMessage.success("备份已恢复，正在打开恢复后的项目");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function restoreRecoveryPoint(): Promise<void> {
  if (!selectedRecoveryPointId.value) {
    ElMessage.warning("暂无可恢复的自动备份恢复点");
    return;
  }
  if (!restoreTarget.value.trim()) {
    ElMessage.warning("请选择空的目标文件夹，或输入一个新的项目路径");
    return;
  }
  try {
    await ElMessageBox.confirm(
      "恢复点会写入新的项目目录，不会覆盖当前项目。请确认目标目录为空或不存在。",
      "恢复自动备份", { type: "warning", confirmButtonText: "恢复并打开", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  working.value = true;
  try {
    const result = await api.restoreRecoveryPoint(selectedRecoveryPointId.value, restoreTarget.value.trim());
    emit("restored", result.path);
    activePanel.value = "";
    ElMessage.success("自动备份恢复点已恢复，正在打开恢复后的项目");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function restartProgram(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "程序将结束当前进程并重新启动，浏览器会自动打开新页面。底稿内容已自动保存，界面上的未确认输入可能丢失。是否继续？",
      "重启程序",
      { type: "warning", confirmButtonText: "重启程序", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  working.value = true;
  try {
    await api.restartProgram();
    activePanel.value = "";
    ElMessage.success("程序正在重启，浏览器会自动打开新页面…");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function quitProgram(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "程序将退出，浏览器页面将无法访问。底稿内容已自动保存，退出前会安全关闭数据库。是否退出？",
      "退出程序",
      { type: "warning", confirmButtonText: "退出程序", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  working.value = true;
  try {
    await api.quitProgram();
    activePanel.value = "";
    ElMessage.success("程序正在退出…");
  } catch (error) {
    report(error);
  } finally {
    working.value = false;
  }
}

async function resetProject(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      `输入项目名称「${props.projectName}」以确认清空全部数据。单位、底稿、附件、日志将全部删除且不可恢复。`,
      "重置项目（不可恢复）",
      {
        type: "warning",
        confirmButtonText: "确认重置",
        cancelButtonText: "取消",
        inputPlaceholder: props.projectName,
        inputValidator: (input: string) =>
          input.trim() === props.projectName || "输入内容与项目名称不一致",
      },
    );
    working.value = true;
    await api.resetProject(value.trim());
    activePanel.value = "";
    emit("dataChanged");
    ElMessage.success("项目已重置，全部数据已清空");
  } catch (error) {
    if (error !== "cancel") report(error);
  } finally {
    working.value = false;
  }
}
</script>

<template>
  <div class="project-operations">
    <el-button size="small" @click="openSummary">📊 项目汇总</el-button>
    <el-button size="small" @click="emit('openExchange')">💬 交流修订</el-button>
    <el-button size="small" @click="openSearch">🔍 搜索</el-button>
    <el-dropdown trigger="click" @command="command">
      <el-button size="small">项目菜单 ▾</el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="health">🩺 项目健康检查</el-dropdown-item>
          <el-dropdown-item command="scan">🔍 附件完整性扫描</el-dropdown-item>
          <el-dropdown-item command="logs">📋 操作日志</el-dropdown-item>
          <el-dropdown-item command="recycle">♻️ 问题回收站</el-dropdown-item>
          <el-dropdown-item command="settings">⚙️ 编制与预设设置</el-dropdown-item>
          <el-dropdown-item command="rename">✏️ 重命名项目</el-dropdown-item>
          <el-dropdown-item divided command="import">📥 导入问题汇总（Excel）</el-dropdown-item>
          <el-dropdown-item command="export">📤 导出问题汇总（Excel）</el-dropdown-item>
          <el-dropdown-item command="package">📦 一键归档打包（ZIP）</el-dropdown-item>
          <el-dropdown-item command="backup" divided>💾 创建项目备份</el-dropdown-item>
          <el-dropdown-item command="merge">🔄 合并导入多个备份</el-dropdown-item>
          <el-dropdown-item command="restore">♻️ 导入备份（恢复项目）</el-dropdown-item>
          <el-dropdown-item command="restart" divided>🔄 重启程序</el-dropdown-item>
          <el-dropdown-item command="quit">⏻ 退出程序</el-dropdown-item>
          <el-dropdown-item command="reset" class="danger-item">🗑 重置项目（清空全部数据）</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" :width="dialogWidth" append-to-body>
      <div v-if="activePanel === 'settings'" class="operation-panel">
        <section class="preset-section"><h3>版本历史与自动保存</h3><p>仅当底稿内容发生变化时才保存新版本；没有变化不会重复留版。</p><div class="tool-options"><label><input type="radio" name="auto-save-mode" :checked="autoSaveMode === 'realtime'" @change="changeAutoSaveMode('realtime')" /> 输入停止后实时保存</label><label><input type="radio" name="auto-save-mode" :checked="autoSaveMode === '5m'" @change="changeAutoSaveMode('5m')" /> 每 5 分钟保存（默认）</label><label><input type="radio" name="auto-save-mode" :checked="autoSaveMode === '20m'" @change="changeAutoSaveMode('20m')" /> 每 20 分钟保存</label></div></section>
        <section class="preset-section">
          <h3>自动备份</h3>
          <p>默认关闭。开启后每次仅复制新增或变化的附件对象；可设置恢复点保留天数。完整、可携带的 <code>.auditbak</code> 仍请按周或里程碑手工创建。</p>
          <label class="backup-enabled"><input v-model="backupEnabled" type="checkbox" @change="onBackupEnabledChanged" /> 开启自动备份</label>
          <div class="backup-target-row"><input v-model="backupTargetDir" class="backup-native-input" type="text" placeholder="自动备份目标目录（项目目录之外）" /><button class="backup-native-button" type="button" @click="chooseBackupTarget">选择目录</button></div>
          <div class="backup-policy-grid"><label>间隔（分钟）<input v-model.number="backupIntervalMinutes" class="backup-native-input" type="number" min="30" step="30" /></label><label>保留天数<input v-model.number="backupRetentionDays" class="backup-native-input" type="number" min="1" max="3650" step="1" /></label><label>最大保留空间（GiB）<input v-model.number="backupMaxGiB" class="backup-native-input" type="number" min="1" step="10" /></label></div>
          <p v-if="!backupEnabled" class="version-hint">可先填写并保存策略；勾选“开启自动备份”后才会按该策略自动执行。</p>
          <p v-if="backupSettings?.last_success_at" class="version-hint">最近成功：{{ backupSettings.last_success_at }}</p>
          <p v-if="backupSettings?.last_error" class="danger-text">最近失败：{{ backupSettings.last_error }}</p>
          <div class="tool-actions"><el-button type="primary" :loading="working" @click="saveBackupPolicy">保存策略</el-button><el-button :loading="working" :disabled="!backupEnabled" @click="createRecoveryPoint">立即创建增量恢复点</el-button><el-button :loading="working" @click="loadRecoveryPoints">刷新恢复点</el-button></div>
          <p v-if="backupEnabled && !recoveryPoints.length" class="version-hint">尚无可用恢复点。创建完成后点击“刷新恢复点”查看。</p>
          <div v-else-if="recoveryPoints.length" class="recovery-point-list">
            <label v-for="point in recoveryPoints" :key="point.id" class="recovery-point-row"><input v-model="selectedRecoveryPointId" type="radio" :value="point.id" /><span>{{ point.created_at || point.id }}</span><small>{{ point.attachments }} 项附件记录 · 本次元数据 {{ formatAmount(point.size) }} 字节 · 逻辑附件 {{ formatAmount(point.logical_bytes) }} 字节 · {{ point.health }}</small></label>
          </div>
        </section>
        <section class="preset-section"><h3>金额默认口径</h3><p>仅用于之后新建底稿的默认值；汇总始终按币种和单位分组，不做未经批准的换算。</p><div class="tool-actions"><el-select v-model="amountDefaultCurrency" aria-label="默认币种" style="width: 120px"><el-option label="CNY" value="CNY" /><el-option label="USD" value="USD" /><el-option label="EUR" value="EUR" /><el-option label="HKD" value="HKD" /></el-select><el-select v-model="amountDefaultUnit" aria-label="默认金额单位" style="width: 120px"><el-option v-for="unit in amountSettings?.allowed_units || ['元', '万元', '亿元']" :key="unit" :label="unit" :value="unit" /></el-select><el-button type="primary" :loading="working" @click="saveAmountSettings">保存默认口径</el-button></div></section>
        <section class="preset-section"><h3>底稿编号规则</h3><p>界面、台账与归档目录显示编号 = 前缀 + 数字序号 + 后缀；永久关联使用系统 UUID。前后缀只影响当前展示，不追溯历史；删除后数字可复用。默认仅数字序号。</p><div class="tool-actions"><el-input v-model="issuePrefix" placeholder="前缀（可空，如 A-）" style="width: 150px" /><el-input v-model="issueSuffix" placeholder="后缀（可空，如 号）" style="width: 150px" /><el-button type="primary" :loading="working" @click="saveIssueNumber">保存</el-button></div><p class="version-hint">预览：{{ issuePrefix || "（空）" }}123{{ issueSuffix || "（空）" }}</p></section>
        <p>预设用于底稿编辑时快速选择；两类预设均不会修改已有底稿。</p>
        <section class="preset-section"><h3>所属版块</h3><div class="tool-actions"><el-input v-model="departmentName" placeholder="例如：营销管理" @keyup.enter="addDepartment" /><el-button type="primary" @click="addDepartment">添加版块</el-button></div><div v-if="departmentDraft.length" class="department-list"><div v-for="department in departmentDraft" :key="department" class="department-row"><span>📂 {{ department }}</span><el-button text type="danger" size="small" @click="removeDepartment(department)">移除</el-button></div></div><el-empty v-else description="暂无版块预设，可直接添加" :image-size="50" /></section>
        <section class="preset-section"><h3>问题分类（可选）</h3><div class="tool-actions"><el-input v-model="categoryName" aria-label="新增问题分类预设" placeholder="例如：经营管理" @keyup.enter="addCategory" /><el-button type="primary" @click="addCategory">添加分类</el-button></div><div v-if="categoryDraft.length" class="department-list"><div v-for="category in categoryDraft" :key="category" class="department-row"><span>🏷 {{ category }}</span><el-button text type="danger" size="small" @click="removeCategory(category)">移除</el-button></div></div><el-empty v-else description="暂无问题分类预设，可直接添加" :image-size="50" /></section>
      </div>

      <div v-else-if="activePanel === 'summary'" class="operation-panel">
        <div class="panel-head"><p>点击问题行可跳转到对应底稿；金额合计随筛选变化。</p><el-button size="small" @click="openSummary">刷新</el-button></div>
        <el-empty v-if="!summary" description="正在读取问题清单…" :image-size="58" />
        <template v-else>
          <div class="summary-total">共 {{ summary.total }} 个底稿 · 当前筛选 {{ summaryIssues.length }} 项 · 涉及 {{ summaryUnitsCount }} 个单位 · 金额汇总 {{ summaryAmountGroups.groups.length ? summaryAmountGroups.groups.join("；") : "无结构化金额" }}<template v-if="summaryAmountGroups.unstructured">；另有 {{ summaryAmountGroups.unstructured }} 条历史自由文本金额未参与汇总</template></div>
          <div class="summary-filters"><select v-model="summaryUnitFilter"><option :value="null">全部单位</option><option v-for="unit in units" :key="unit.id" :value="unit.id">{{ unit.name }}</option></select><select v-model="summaryDepartmentFilter"><option value="">全部版块</option><option v-for="department in summaryDepartments" :key="department" :value="department">{{ department || '未分版块' }}</option></select><select v-model="summaryStatusFilter"><option value="">全部状态</option><option v-for="status in summaryStatuses" :key="status" :value="status">{{ status }}</option></select></div>
          <div class="issue-table-head"><span>底稿</span><span>单位</span><span>版块</span><span>定性</span><span class="num">金额</span><span>状态</span></div>
          <div class="issue-table">
            <div v-for="issue in summaryIssues" :key="issue.id" class="issue-table-row" @click="goIssue(issue.id)">
              <span>{{ formatIssueNo(issue.seq, issueNumberRule) }}</span><span>{{ issue.unit_name }}</span><span>{{ issue.department || '—' }}</span><span class="defect">{{ issue.defect_type }}</span><span class="num">{{ formatMoney(issue.amount) }}</span><span>{{ issue.status }}</span>
            </div>
            <el-empty v-if="!summaryIssues.length" description="无符合筛选条件的问题" :image-size="50" />
          </div>
          <div class="summary-grid"><section><h3>按状态</h3><div v-if="Object.keys(summary.by_status).length" class="summary-items"><div v-for="(count, name) in summary.by_status" :key="name"><span>{{ name }}</span><strong>{{ count }}</strong></div></div><p v-else class="summary-empty">暂无数据</p></section><section><h3>按版块</h3><div v-if="Object.keys(summary.by_dept).length" class="summary-items"><div v-for="(count, name) in summary.by_dept" :key="name"><span>{{ name }}</span><strong>{{ count }}</strong></div></div><p v-else class="summary-empty">暂无数据</p></section><section><h3>按单位（底稿 / 附件）</h3><div v-if="Object.keys(summary.by_unit).length" class="summary-items"><div v-for="(value, name) in summary.by_unit" :key="name"><span>{{ name }}</span><strong>{{ value.issues }} / {{ value.files }}</strong></div></div><p v-else class="summary-empty">暂无数据</p></section></div>
        </template>
      </div>

      <div v-else-if="activePanel === 'search'" class="operation-panel">
        <el-input v-model="searchQuery" placeholder="搜索单位、底稿（定性/版块/描述/依据/建议）、附件文件名…" clearable @input="onSearchInput" @keyup.enter="runSearch" />
        <div class="search-results">
          <p v-if="searchLoading" class="summary-empty">搜索中…</p>
          <template v-else-if="searchResult">
            <section v-if="searchResult.units.length"><h3>单位（{{ searchResult.units.length }}）</h3><div v-for="unit in searchResult.units" :key="unit.id" class="search-row" @click="goUnit(unit.id)"><span>🏢 {{ unit.name }}</span><small>跳转到该单位</small></div></section>
            <section v-if="searchResult.issues.length"><h3>底稿（{{ searchResult.issues.length }}）</h3><div v-for="issue in searchResult.issues" :key="issue.id" class="search-row" @click="goIssue(issue.id)"><span>📄 {{ issueLabel(issue) }}</span><small>{{ issue.unit_name }} · {{ issue.department || '未分版块' }} · 金额 {{ formatAmount(issue.amount) }}</small></div></section>
            <section v-if="searchResult.files.length"><h3>附件（{{ searchResult.files.length }}）</h3><div v-for="file in searchResult.files" :key="file.id" class="search-row" @click="goUnit(file.unit_id)"><span>{{ file.mime === 'folder' ? '📁' : '📎' }} {{ file.orig_name }}</span><small>{{ file.unit_name }} · {{ file.mime === 'folder' ? '文件夹' : '文件' }}</small></div></section>
            <el-empty v-if="!searchResult.units.length && !searchResult.issues.length && !searchResult.files.length" description="未找到匹配内容" :image-size="50" />
          </template>
          <p v-else class="summary-empty">输入关键字开始搜索；支持单位名、底稿定性/描述/制度依据/审计建议、附件文件名。</p>
        </div>
      </div>

      <div v-else-if="activePanel === 'logs'" class="operation-panel">
        <div class="panel-head"><p>记录本项目内的新增、修改、导入、导出与状态流转操作。</p><el-button size="small" @click="openLogs">刷新</el-button></div>
        <el-empty v-if="!logs.length" description="暂无操作日志" :image-size="58" />
        <div v-else class="log-list"><div v-for="entry in logs" :key="entry.id" class="log-row"><time>{{ entry.created_at }}</time><strong>{{ entry.operator }}</strong><span>{{ entry.action }}</span><span>{{ entry.target }}</span><small>{{ entry.detail }}</small></div></div>
      </div>

      <div v-else-if="activePanel === 'recycle'" class="operation-panel">
        <div class="panel-head"><p>默认不自动清空；单位、底稿和附件都可在此恢复，只有明确物理删除才会移除证据。</p><el-button size="small" @click="openRecycle">刷新</el-button></div>
        <el-empty v-if="!recycledIssues.length && !recycledUnits.length && !recycledFiles.length" description="回收站为空" :image-size="58" />
        <template v-else>
          <section v-if="recycledIssues.length"><h3>底稿（{{ recycledIssues.length }}）</h3><div class="recycle-batch-bar"><label class="recycle-check-label"><input class="recycle-check" type="checkbox" :checked="recycleAllSelected" @change="toggleAllRecycledIssues(($event.target as HTMLInputElement).checked)" />全选</label><span>已选 {{ selectedRecycleIds.length }} / {{ recycledIssues.length }} 条</span><el-button size="small" type="primary" :loading="working" :disabled="!selectedRecycleIds.length" @click="restoreRecycledIssues">恢复所选</el-button><el-button size="small" type="danger" plain :loading="working" :disabled="!selectedRecycleIds.length" @click="purgeRecycledIssues">物理删除所选</el-button></div><div class="recycle-list"><div v-for="issue in recycledIssues" :key="issue.recycle_id" class="recycle-row" :class="{ selected: selectedRecycleIds.includes(issue.recycle_id) }"><input class="recycle-check" type="checkbox" :checked="selectedRecycleIds.includes(issue.recycle_id)" :aria-label="`选择问题 ${issue.seq}`" @change="toggleRecycledIssue(issue.recycle_id, ($event.target as HTMLInputElement).checked)" /><button class="recycle-row-main" @click="previewRecycledIssue(issue)"><strong>{{ issue.unit_name || `单位${issue.unit_id}` }} · 问题{{ formatIssueNo(issue.seq, issueNumberRule) }} · {{ issue.defect_type || '未定性' }}</strong><small>{{ issue.department || '未分版块' }} · {{ issue.status }} · {{ issue.deleted_at }} 由 {{ issue.deleted_by }} 移入</small></button></div></div></section>
          <section v-if="recycledUnits.length" class="recycle-extra"><h3>单位（{{ recycledUnits.length }}）</h3><div class="recycle-list"><div v-for="unit in recycledUnits" :key="unit.recycle_id" class="recycle-row"><div class="recycle-row-main"><strong>🏢 {{ unit.name }}</strong><small>{{ unit.issue_count }} 条底稿 · {{ unit.file_count }} 个附件 · {{ unit.deleted_at }} 由 {{ unit.deleted_by }} 移入</small></div><div class="recycle-row-actions"><el-button size="small" type="primary" :loading="working" @click="restoreRecycledUnit(unit)">恢复</el-button><el-button size="small" type="danger" plain :loading="working" @click="purgeRecycledUnit(unit)">物理删除</el-button></div></div></div></section>
          <section v-if="recycledFiles.length" class="recycle-extra"><h3>附件（{{ recycledFiles.length }}）</h3><div class="recycle-list"><div v-for="file in recycledFiles" :key="file.recycle_id" class="recycle-row"><div class="recycle-row-main"><strong>{{ file.mime === 'folder' ? '📁' : '📎' }} {{ file.orig_name }}</strong><small>{{ file.unit_name || `单位${file.unit_id}` }} · {{ file.deleted_at }} 由 {{ file.deleted_by }} 移入</small></div><div class="recycle-row-actions"><el-button size="small" type="primary" :loading="working" @click="restoreRecycledFile(file)">恢复</el-button><el-button size="small" type="danger" plain :loading="working" @click="purgeRecycledFile(file)">物理删除</el-button></div></div></div></section>
        </template>
      </div>

      <div v-else-if="activePanel === 'scan'" class="operation-panel">
        <template v-if="scan?.status === 'queued' || scan?.status === 'running'"><p>全量扫描会核对附件库、底稿关联和文件哈希；项目较大时请保持本窗口打开。</p><div class="scan-meter"><span :style="{ width: `${scanPercent()}%` }"></span></div><strong>{{ scanPercent() }}% · {{ scanPhaseText() }}</strong><div class="tool-actions"><el-button type="danger" @click="cancelScan">取消扫描</el-button></div></template>
        <template v-else-if="scan?.status === 'cancelled'"><p class="scan-state">扫描已取消，未生成完整结果。</p></template>
        <template v-else-if="scan?.status === 'error'"><p class="scan-state danger-text">扫描失败：{{ scan.error }}</p><div class="tool-actions"><el-button type="primary" @click="startScan">重新扫描</el-button></div></template>
        <template v-else-if="scan?.status === 'done'"><p class="scan-state" :class="scan.problems.length ? 'warning-text' : 'success-text'">{{ scan.problems.length ? `发现 ${scan.problems.length} 项待处理问题` : '扫描完成，项目数据完整' }}</p><p>单位 {{ scan.counts.units ?? 0 }} · 底稿 {{ scan.counts.issues ?? 0 }} · 附件 {{ scan.counts.files ?? 0 }} · 哈希核对 {{ scan.sample.checked }}/{{ scan.sample.total }}</p><div v-if="scan.problems.length" class="scan-problems"><div v-for="problem in scan.problems.slice(0, 15)" :key="`${problem.type}-${problem.message}`">· [{{ problem.severity }}] {{ problem.message }}</div></div><div class="tool-actions"><el-button @click="startScan">再次扫描</el-button></div></template>
        <el-empty v-else description="正在准备扫描…" :image-size="58" />
      </div>

      <div v-else-if="activePanel === 'rename'" class="operation-panel"><p>项目名称会出现在导出文件名和归档包目录中，不会改变项目文件夹路径。</p><el-input v-model="projectNameDraft" placeholder="项目名称" @keyup.enter="renameProject" /><div class="tool-actions"><el-button type="primary" :loading="working" @click="renameProject">保存名称</el-button></div></div>

      <div v-else-if="activePanel === 'import'" class="operation-panel">
      <p>先下载模板。带 * 的三列为必填；不存在的被审计单位将自动创建。</p>
      <div class="tool-actions"><el-button :loading="working" @click="downloadTemplate">下载 Excel 模板</el-button><el-button :loading="working" @click="importPicker?.click()">选择 .xlsx 文件</el-button><el-button type="primary" :loading="working" :disabled="!importFile" @click="importExcel">开始导入</el-button></div>
      <input ref="importPicker" class="hidden-input" type="file" accept=".xlsx" @change="inputFile($event, 'import')" />
      <span v-if="importFile" class="selected-file">已选：{{ importFile.name }}</span>
      <div v-if="importResult" class="operation-result"><strong>导入结果：</strong>成功 {{ importResult.imported }} 条，跳过 {{ importResult.skipped }} 条，新建单位 {{ importResult.new_units }} 个。<ul v-if="importResult.errors.length"><li v-for="error in importResult.errors.slice(0, 10)" :key="error">{{ error }}</li></ul><el-button v-if="importResult.errors.length" text size="small" @click="downloadImportReport">下载完整导入报告</el-button></div>
    </div>

      <div v-else-if="activePanel === 'export'" class="operation-panel">
      <p>导出的问题汇总含底稿状态、版本数和证据提示，文件保存至浏览器下载位置。</p>
      <div class="tool-options"><label><input v-model="exportScope" type="radio" value="project" /> 全部单位</label><label><input v-model="exportScope" type="radio" value="unit" /> 单个单位</label><select v-if="exportScope === 'unit'" v-model="exportUnitId"><option v-for="unit in units" :key="unit.id" :value="unit.id">{{ unit.name }}</option></select></div>
      <div class="tool-actions"><el-button type="primary" :loading="working" @click="exportExcel">生成并下载汇总表</el-button></div>
    </div>

      <div v-else-if="activePanel === 'package'" class="operation-panel">
      <p>归档包包含问题汇总、按底稿整理的附件及 SHA-256 完整性清单。生成前必须完成一次全量核对；核对后如数据或附件变化，必须重新核对。</p>
      <div class="tool-options"><label><input v-model="packageScope" type="radio" value="all" @change="clearArchivePreflight" /> 全部单位（{{ units.length }}）</label><label><input v-model="packageScope" type="radio" value="selected" @change="clearArchivePreflight" /> 勾选单位</label><label><input v-model="groupByDepartment" type="checkbox" @change="clearArchivePreflight" /> 按版块建立三级目录</label></div>
      <div v-if="packageScope === 'selected'" class="unit-checks"><label v-for="unit in units" :key="unit.id"><input type="checkbox" :checked="packageUnitIds.includes(unit.id)" @change="togglePackageUnit(unit.id, ($event.target as HTMLInputElement).checked); clearArchivePreflight()" /> {{ unit.name }}</label></div>
      <section v-if="archivePreflight" class="archive-checklist"><h3>{{ archivePreflight.blockers.length ? '归档已阻止' : '归档核对清单' }}</h3><p>范围：{{ archivePreflight.counts.units }} 个单位 · {{ archivePreflight.counts.issues }} 条底稿 · {{ archivePreflight.counts.files }} 个附件；全量哈希核对 {{ archivePreflight.health.checked.checked }}/{{ archivePreflight.health.checked.total }}。</p><div v-if="archivePreflight.blockers.length" class="archive-checklist-items danger"><strong>阻断项（必须处理）</strong><ul><li v-for="item in archivePreflight.blockers" :key="`${item.code}-${item.message}`">{{ item.message }}</li></ul></div><div v-if="archivePreflight.warnings.length" class="archive-checklist-items warning"><strong>警告项（确认后可继续）</strong><ul><li v-for="item in archivePreflight.warnings" :key="`${item.code}-${item.message}`">{{ item.message }}</li></ul></div><p v-if="!archivePreflight.blockers.length && !archivePreflight.warnings.length" class="success-text">未发现阻断项或警告项。</p></section>
      <div class="tool-actions"><span>将打包 {{ selectedPackageCount }} 个单位</span><el-button :loading="working" @click="prepareArchivePreflight">{{ archivePreflight ? '重新核对' : '开始归档核对' }}</el-button><el-button type="primary" :loading="working" :disabled="Boolean(archivePreflight?.blockers.length)" @click="packageProject">{{ archivePreflight?.confirmation_token ? '确认核对并生成归档包' : '生成并下载归档包' }}</el-button></div>
    </div>

      <div v-else-if="activePanel === 'backup'" class="operation-panel">
      <p>备份保存数据库和附件库的完整一致性快照，适合项目交接、重大操作前留存和跨电脑迁移。备份仅保存一份至项目上级目录。</p>
      <div class="tool-actions"><el-button type="primary" :loading="working" @click="backupProject">创建 .auditbak 备份</el-button></div>
    </div>

      <div v-else-if="activePanel === 'merge'" class="operation-panel">
      <p>用于审计经理汇总多个成员提交的 .auditbak。为支持 50GB 附件并防止绕过冲突确认，请逐行输入本机备份完整路径：先预检，负责人确认冲突处理方式后才能写入当前项目。</p>
      <textarea v-model="mergeLocalPaths" class="merge-local-paths" rows="5" placeholder="本机 .auditbak 完整路径：每行一个" @input="clearMergePreflight"></textarea>
      <section v-if="mergePreflight" class="archive-checklist"><h3>{{ mergePreflight.blockers.length ? '合并已阻止' : '合并预检清单' }}</h3><p>已检查 {{ mergePreflight.sources.length }} 个来源。预检只读取备份数据库，不写入当前项目。</p><div v-if="mergePreflight.sources.length" class="merge-source-list"><div v-for="source in mergePreflight.sources" :key="source.name"><strong>{{ source.name }}</strong><small>{{ source.units }} 个单位 · {{ source.issues }} 条底稿 · {{ source.attachments }} 个附件 · {{ source.project_uuid }}</small></div></div><div v-if="mergePreflight.blockers.length" class="archive-checklist-items danger"><strong>阻断项（必须处理）</strong><ul><li v-for="item in mergePreflight.blockers" :key="`${item.source}-${item.code}-${item.message}`">{{ item.source }}：{{ item.message }}</li></ul></div><div v-if="mergePreflight.conflicts.length" class="archive-checklist-items warning"><strong>需负责人确认（默认并存，不静默覆盖）</strong><ul><li v-for="item in mergePreflight.conflicts" :key="`${item.source}-${item.type}-${item.message}`">{{ item.source }}：{{ item.message }}；处理方式：{{ item.resolution }}</li></ul></div><p v-if="!mergePreflight.blockers.length && !mergePreflight.conflicts.length" class="success-text">未发现来源冲突。</p></section>
      <div class="tool-actions"><el-button :loading="working" @click="prepareMergePreflight">{{ mergePreflight ? '重新预检' : '开始合并预检' }}</el-button><el-button type="primary" :loading="working" :disabled="Boolean(mergePreflight?.blockers.length)" @click="mergeBackups">{{ mergePreflight?.confirmation_token ? '确认并合并' : '执行合并' }}</el-button></div>
      <div v-if="mergeResult" class="operation-result"><strong>合并结果：</strong>新增单位 {{ mergeResult.units }} 个、底稿 {{ mergeResult.issues }} 条、迁移版本 {{ mergeResult.versions }} 个、附件 {{ mergeResult.files }} 个、文件夹 {{ mergeResult.folders }} 个。<ul v-if="mergeResult.conflicts.length"><li v-for="item in mergeResult.conflicts.slice(0, 10)" :key="`${item.type}-${item.message}`">[{{ item.type }}] {{ item.message }}</li></ul><ul v-if="mergeResult.errors.length"><li v-for="error in mergeResult.errors.slice(0, 10)" :key="error">{{ error }}</li></ul><el-button text size="small" @click="downloadMergeReport">下载完整合并报告</el-button></div>
    </div>

      <div v-else-if="activePanel === 'restore'" class="operation-panel">
      <p>恢复只允许写入空目录或新目录，不会覆盖当前打开项目。恢复后目录自动加 <code>.auditproj</code> 后缀并隐藏，与新建项目一致。</p>
      <div class="tool-actions"><el-button :loading="working" @click="restorePicker?.click()">选择 .auditbak 文件</el-button><el-button :loading="working" @click="chooseRestoreTarget">选择恢复文件夹</el-button></div>
      <input ref="restorePicker" class="hidden-input" type="file" accept=".auditbak" @change="inputFile($event, 'restore')" />
      <el-input v-model="restoreLocalPath" placeholder="或输入本机 .auditbak 文件完整路径（大于 800MB 时请用此方式）" />
      <el-input v-model="restoreTarget" placeholder="恢复目标目录（必须为空或不存在）" />
      <span v-if="restoreFile" class="selected-file">备份文件：{{ restoreFile.name }}</span>
      <div class="tool-actions"><el-button type="primary" :loading="working" @click="restoreBackup">恢复并打开项目</el-button></div>
      <section class="restore-recovery-points"><h3>自动备份恢复点</h3><p>选择一个恢复点，再指定上方恢复目标目录。恢复点仅在已设置的自动备份目录中可用。</p><div v-if="recoveryPoints.length" class="recovery-point-list"><label v-for="point in recoveryPoints" :key="point.id" class="recovery-point-row"><input v-model="selectedRecoveryPointId" type="radio" :value="point.id" /><span>{{ point.created_at || point.id }}</span><small>{{ point.attachments }} 项附件记录</small></label></div><p v-else class="version-hint">暂无可用恢复点。</p><div class="tool-actions"><el-button :loading="working" @click="loadRecoveryPoints">刷新恢复点</el-button><el-button type="primary" :loading="working" :disabled="!selectedRecoveryPointId" @click="restoreRecoveryPoint">从选中恢复点恢复并打开</el-button></div></section>
      </div>
    </el-dialog>

    <el-dialog v-model="recyclePreviewVisible" title="回收站底稿预览（只读）" width="min(760px, calc(100vw - 32px))" append-to-body>
      <template v-if="recycledPreview">
        <p class="version-hint">{{ recycledPreview.unit_name }} · 问题{{ formatIssueNo(recycledPreview.issue.seq, issueNumberRule) }} · {{ recycledPreview.issue.status }}；{{ recycledPreview.deleted_at }} 由 {{ recycledPreview.deleted_by }} 移入回收站。</p>
        <div class="recycle-preview-grid"><strong>所属版块</strong><span>{{ recycledPreview.issue.department || '—' }}</span><strong>问题分类</strong><span>{{ recycledPreview.issue.category || '—' }}</span><strong>问题金额</strong><span>{{ recycledPreview.issue.amount ? `${recycledPreview.issue.amount} ${recycledPreview.issue.currency || ''} ${recycledPreview.issue.amount_unit || ''}` : '—' }}</span><strong>编制 / 审核</strong><span>{{ recycledPreview.issue.author || '—' }} / {{ recycledPreview.issue.reviewer || '—' }}</span><strong>版本 / 附件</strong><span>{{ recycledPreview.version_count }} 个版本 / {{ recycledPreview.attachment_total }} 个附件</span></div>
        <section class="recycle-preview-section"><h3>问题定性</h3><p>{{ recycledPreview.issue.defect_type || '—' }}</p></section>
        <section class="recycle-preview-section"><h3>问题描述</h3><p>{{ recycledPreview.issue.defect_desc || '—' }}</p></section>
        <section class="recycle-preview-section"><h3>制度依据</h3><p>{{ recycledPreview.issue.regulation_basis || '—' }}</p></section>
        <section class="recycle-preview-section"><h3>审计建议</h3><p>{{ recycledPreview.issue.suggestion || '—' }}</p></section>
        <section class="recycle-preview-section"><h3>附件</h3><p v-if="!recycledPreview.attachments.length">无关联附件</p><ul v-else class="recycle-preview-files"><li v-for="file in recycledPreview.attachments" :key="file.id">{{ file.mime === 'folder' ? '📁' : '📎' }} {{ file.orig_name }} <small>（{{ formatAmount(file.size) }} 字节）</small></li></ul><p v-if="recycledPreview.attachments_truncated" class="version-hint">仅显示前 100 个附件；恢复底稿后可在附件面板查看全部。</p></section>
      </template>
    </el-dialog>
  </div>
</template>
