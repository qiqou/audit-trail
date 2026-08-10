<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type AuditLog, type ImportResult, type MergeResult, type ProjectInfo, type ProjectSummary, type ScanStatus, type Unit } from "../api/client";

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
}>();

type Panel = "" | "import" | "export" | "package" | "backup" | "merge" | "restore" | "settings" | "summary" | "logs" | "scan" | "rename";

const activePanel = ref<Panel>("");
const working = ref(false);
const importPicker = ref<HTMLInputElement | null>(null);
const mergePicker = ref<HTMLInputElement | null>(null);
const restorePicker = ref<HTMLInputElement | null>(null);
const importFile = ref<File | null>(null);
const mergeFiles = ref<File[]>([]);
const restoreFile = ref<File | null>(null);
const restoreTarget = ref("");
const importResult = ref<ImportResult | null>(null);
const mergeResult = ref<MergeResult | null>(null);
const exportScope = ref<"project" | "unit">("project");
const exportUnitId = ref<number | null>(null);
const packageScope = ref<"all" | "selected">("all");
const packageUnitIds = ref<number[]>([]);
const groupByDepartment = ref(false);
const departmentName = ref("");
const departmentDraft = ref<string[]>([]);
const categoryName = ref("");
const categoryDraft = ref<string[]>([]);
const issuePrefix = ref("");
const issueSuffix = ref("");
const summary = ref<ProjectSummary | null>(null);
const logs = ref<AuditLog[]>([]);
const scan = ref<ScanStatus | null>(null);
const projectNameDraft = ref("");
let scanTimer: ReturnType<typeof window.setTimeout> | undefined;

const selectedPackageCount = computed(() => packageScope.value === "all" ? props.units.length : packageUnitIds.value.length);
const dialogTitles: Partial<Record<Panel, string>> = {
  import: "导入问题汇总（Excel）",
  export: "导出问题汇总表（Excel）",
  package: "一键归档打包（ZIP）",
  backup: "创建项目备份",
  merge: "合并导入（.auditbak）",
  restore: "导入备份（恢复项目）",
  settings: "编制与预设设置",
  summary: "项目汇总视图",
  logs: "操作日志（随项目保存）",
  scan: "附件完整性扫描",
  rename: "重命名项目",
};
const dialogVisible = computed({
  get: () => Boolean(activePanel.value),
  set: (visible: boolean) => { if (!visible) activePanel.value = ""; },
});
const dialogTitle = computed(() => dialogTitles[activePanel.value] ?? "项目操作");

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
}

function command(value: string): void {
  if (value === "health") {
    emit("healthCheck");
    return;
  }
  if (value === "settings") { openSettings(); return; }
  if (value === "summary") { void openSummary(); return; }
  if (value === "logs") { void openLogs(); return; }
  if (value === "scan") { void startScan(); return; }
  if (value === "rename") { openRename(); return; }
  if (value === "restart") { void restartProgram(); return; }
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

function scanPercent(): number {
  if (!scan.value?.total) return scan.value?.phase === "hash" ? 0 : 10;
  return Math.min(99, Math.round((scan.value.done / scan.value.total) * 100));
}

function scanPhaseText(): string {
  if (!scan.value) return "准备扫描…";
  if (scan.value.phase === "phys") return `扫描附件库文件… ${scan.value.done}/${scan.value.total}`;
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
  if (packageScope.value === "selected" && !packageUnitIds.value.length) {
    ElMessage.warning("请至少勾选一个被审计单位");
    return;
  }
  working.value = true;
  try {
    const result = await api.packageProject(packageScope.value === "selected" ? packageUnitIds.value : [], groupByDepartment.value);
    await api.downloadUrl(result.download_url, result.filename);
    ElMessage.success(`归档包已生成：${result.units} 个单位、${result.issues} 条底稿`);
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
  if (!mergeFiles.value.length) {
    ElMessage.warning("请选择一个或多个 .auditbak 备份文件");
    return;
  }
  try {
    await ElMessageBox.confirm("合并会向当前项目写入单位、底稿和附件。请先创建备份，再继续。", "合并备份", {
      type: "warning", confirmButtonText: "继续合并", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  working.value = true;
  try {
    const result = await api.mergeBackups(mergeFiles.value);
    mergeResult.value = result;
    emit("dataChanged");
    ElMessage.success(`合并完成：${result.issues} 条底稿、${result.files} 个附件${result.conflicts.length ? `，${result.conflicts.length} 项冲突提示` : ""}`);
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
  if (!restoreFile.value) {
    ElMessage.warning("请选择 .auditbak 备份文件");
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
  working.value = true;
  try {
    const result = await api.restoreBackup(restoreFile.value, restoreTarget.value.trim());
    emit("restored", result.path);
    activePanel.value = "";
    ElMessage.success("备份已恢复，正在打开恢复后的项目");
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
    <el-dropdown trigger="click" @command="command">
      <el-button size="small">项目菜单 ▾</el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="health">🩺 项目健康检查</el-dropdown-item>
          <el-dropdown-item command="scan">🔍 附件完整性扫描</el-dropdown-item>
          <el-dropdown-item command="logs">📋 操作日志</el-dropdown-item>
          <el-dropdown-item command="settings">⚙️ 编制与预设设置</el-dropdown-item>
          <el-dropdown-item command="rename">✏️ 重命名项目</el-dropdown-item>
          <el-dropdown-item divided command="import">📥 导入问题汇总（Excel）</el-dropdown-item>
          <el-dropdown-item command="export">📤 导出问题汇总（Excel）</el-dropdown-item>
          <el-dropdown-item command="package">📦 一键归档打包（ZIP）</el-dropdown-item>
          <el-dropdown-item command="backup" divided>💾 创建项目备份</el-dropdown-item>
          <el-dropdown-item command="merge">🔄 合并导入多个备份</el-dropdown-item>
          <el-dropdown-item command="restore">♻️ 导入备份（恢复项目）</el-dropdown-item>
          <el-dropdown-item command="restart" divided>🔄 重启程序</el-dropdown-item>
          <el-dropdown-item command="reset" class="danger-item">🗑 重置项目（清空全部数据）</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="min(560px, calc(100vw - 32px))" append-to-body>
      <div v-if="activePanel === 'settings'" class="operation-panel">
        <section class="preset-section"><h3>版本历史与自动保存</h3><p>仅当底稿内容发生变化时才保存新版本；没有变化不会重复留版。</p><div class="tool-options"><label><input type="radio" name="auto-save-mode" :checked="autoSaveMode === 'realtime'" @change="changeAutoSaveMode('realtime')" /> 输入停止后实时保存</label><label><input type="radio" name="auto-save-mode" :checked="autoSaveMode === '5m'" @change="changeAutoSaveMode('5m')" /> 每 5 分钟保存（默认）</label><label><input type="radio" name="auto-save-mode" :checked="autoSaveMode === '20m'" @change="changeAutoSaveMode('20m')" /> 每 20 分钟保存</label></div></section>
        <section class="preset-section"><h3>底稿编号规则</h3><p>界面、台账与归档目录中的底稿编号 = 前缀 + 数字序号 + 后缀，作为唯一识别码全程一致；默认仅数字序号。</p><div class="tool-actions"><el-input v-model="issuePrefix" placeholder="前缀（可空，如 A-）" style="width: 150px" /><el-input v-model="issueSuffix" placeholder="后缀（可空，如 号）" style="width: 150px" /><el-button type="primary" :loading="working" @click="saveIssueNumber">保存</el-button></div><p class="version-hint">预览：{{ issuePrefix || "（空）" }}123{{ issueSuffix || "（空）" }}</p></section>
        <p>预设用于底稿编辑时快速选择；两类预设均不会修改已有底稿。</p>
        <section class="preset-section"><h3>所属版块</h3><div class="tool-actions"><el-input v-model="departmentName" placeholder="例如：营销管理" @keyup.enter="addDepartment" /><el-button type="primary" @click="addDepartment">添加版块</el-button></div><div v-if="departmentDraft.length" class="department-list"><div v-for="department in departmentDraft" :key="department" class="department-row"><span>📂 {{ department }}</span><el-button text type="danger" size="small" @click="removeDepartment(department)">移除</el-button></div></div><el-empty v-else description="暂无版块预设，可直接添加" :image-size="50" /></section>
        <section class="preset-section"><h3>问题分类（可选）</h3><div class="tool-actions"><el-input v-model="categoryName" aria-label="新增问题分类预设" placeholder="例如：经营管理" @keyup.enter="addCategory" /><el-button type="primary" @click="addCategory">添加分类</el-button></div><div v-if="categoryDraft.length" class="department-list"><div v-for="category in categoryDraft" :key="category" class="department-row"><span>🏷 {{ category }}</span><el-button text type="danger" size="small" @click="removeCategory(category)">移除</el-button></div></div><el-empty v-else description="暂无问题分类预设，可直接添加" :image-size="50" /></section>
      </div>

      <div v-else-if="activePanel === 'summary'" class="operation-panel">
        <div class="panel-head"><p>汇总数与问题列表一致；单位列显示“底稿数 / 附件数”。</p><el-button size="small" @click="openSummary">刷新</el-button></div>
        <el-empty v-if="!summary" description="正在读取项目汇总…" :image-size="58" />
        <template v-else><div class="summary-total">共 {{ summary.total }} 条底稿</div><div class="summary-grid"><section><h3>按状态</h3><div v-if="Object.keys(summary.by_status).length" class="summary-items"><div v-for="(count, name) in summary.by_status" :key="name"><span>{{ name }}</span><strong>{{ count }}</strong></div></div><p v-else class="summary-empty">暂无数据</p></section><section><h3>按版块</h3><div v-if="Object.keys(summary.by_dept).length" class="summary-items"><div v-for="(count, name) in summary.by_dept" :key="name"><span>{{ name }}</span><strong>{{ count }}</strong></div></div><p v-else class="summary-empty">暂无数据</p></section><section><h3>按单位（底稿 / 附件）</h3><div v-if="Object.keys(summary.by_unit).length" class="summary-items"><div v-for="(value, name) in summary.by_unit" :key="name"><span>{{ name }}</span><strong>{{ value.issues }} / {{ value.files }}</strong></div></div><p v-else class="summary-empty">暂无数据</p></section></div></template>
      </div>

      <div v-else-if="activePanel === 'logs'" class="operation-panel">
        <div class="panel-head"><p>记录本项目内的新增、修改、导入、导出与状态流转操作。</p><el-button size="small" @click="openLogs">刷新</el-button></div>
        <el-empty v-if="!logs.length" description="暂无操作日志" :image-size="58" />
        <div v-else class="log-list"><div v-for="entry in logs" :key="entry.id" class="log-row"><time>{{ entry.created_at }}</time><strong>{{ entry.operator }}</strong><span>{{ entry.action }}</span><span>{{ entry.target }}</span><small>{{ entry.detail }}</small></div></div>
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
      <p>归档包包含问题汇总、按底稿整理的附件及 SHA-256 完整性清单。</p>
      <div class="tool-options"><label><input v-model="packageScope" type="radio" value="all" /> 全部单位（{{ units.length }}）</label><label><input v-model="packageScope" type="radio" value="selected" /> 勾选单位</label><label><input v-model="groupByDepartment" type="checkbox" /> 按版块建立三级目录</label></div>
      <div v-if="packageScope === 'selected'" class="unit-checks"><label v-for="unit in units" :key="unit.id"><input type="checkbox" :checked="packageUnitIds.includes(unit.id)" @change="togglePackageUnit(unit.id, ($event.target as HTMLInputElement).checked)" /> {{ unit.name }}</label></div>
      <div class="tool-actions"><span>将打包 {{ selectedPackageCount }} 个单位</span><el-button type="primary" :loading="working" @click="packageProject">生成并下载归档包</el-button></div>
    </div>

      <div v-else-if="activePanel === 'backup'" class="operation-panel">
      <p>备份保存数据库和附件库的完整一致性快照，适合项目交接、重大操作前留存和跨电脑迁移。备份仅保存一份至项目上级目录。</p>
      <div class="tool-actions"><el-button type="primary" :loading="working" @click="backupProject">创建 .auditbak 备份</el-button></div>
    </div>

      <div v-else-if="activePanel === 'merge'" class="operation-panel">
      <p>用于审计经理汇总多个成员提交的 .auditbak。单位、底稿、附件和版块预设将写入当前项目。</p>
      <div class="tool-actions"><el-button :loading="working" @click="mergePicker?.click()">选择 .auditbak 文件</el-button><el-button type="primary" :loading="working" @click="mergeBackups">确认合并</el-button></div>
      <input ref="mergePicker" class="hidden-input" type="file" accept=".auditbak" multiple @change="inputMergeFiles" />
      <span v-if="mergeFiles.length" class="selected-file">已选 {{ mergeFiles.length }} 个备份：{{ mergeFiles.map((file) => file.name).join('、') }}</span>
      <div v-if="mergeResult" class="operation-result"><strong>合并结果：</strong>新增单位 {{ mergeResult.units }} 个、底稿 {{ mergeResult.issues }} 条、迁移版本 {{ mergeResult.versions }} 个、附件 {{ mergeResult.files }} 个、文件夹 {{ mergeResult.folders }} 个。<ul v-if="mergeResult.conflicts.length"><li v-for="item in mergeResult.conflicts.slice(0, 10)" :key="`${item.type}-${item.message}`">[{{ item.type }}] {{ item.message }}</li></ul><ul v-if="mergeResult.errors.length"><li v-for="error in mergeResult.errors.slice(0, 10)" :key="error">{{ error }}</li></ul><el-button text size="small" @click="downloadMergeReport">下载完整合并报告</el-button></div>
    </div>

      <div v-else-if="activePanel === 'restore'" class="operation-panel">
      <p>恢复只允许写入空目录或新目录，不会覆盖当前打开项目。恢复后目录自动加 <code>.auditproj</code> 后缀并隐藏，与新建项目一致。</p>
      <div class="tool-actions"><el-button :loading="working" @click="restorePicker?.click()">选择 .auditbak 文件</el-button><el-button :loading="working" @click="chooseRestoreTarget">选择恢复文件夹</el-button></div>
      <input ref="restorePicker" class="hidden-input" type="file" accept=".auditbak" @change="inputFile($event, 'restore')" />
      <el-input v-model="restoreTarget" placeholder="恢复目标目录（必须为空或不存在）" />
      <span v-if="restoreFile" class="selected-file">备份文件：{{ restoreFile.name }}</span>
      <div class="tool-actions"><el-button type="primary" :loading="working" @click="restoreBackup">恢复并打开项目</el-button></div>
      </div>
    </el-dialog>
  </div>
</template>
