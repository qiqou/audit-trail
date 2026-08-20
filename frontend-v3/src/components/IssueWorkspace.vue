<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type Issue, type Unit, type WorkpaperTemplate } from "../api/client";
import { formatIssueNo } from "../format";
import { moveIdBefore } from "../order";
import {
  DEFAULT_WORKSPACE_SHORTCUTS,
  WORKSPACE_SHORTCUT_STORAGE_KEY,
  formatShortcut,
  normalizeWorkspaceShortcuts,
  resolveWorkspaceShortcut,
  workspaceShortcutLabels,
  type WorkspaceShortcut,
  type WorkspaceShortcuts,
} from "../shortcuts";
import EvidencePanel from "./EvidencePanel.vue";
import ExchangeWorkbench from "./ExchangeWorkbench.vue";
import IssueEditor from "./IssueEditor.vue";

type AutoSaveMode = "realtime" | "5m" | "20m";

const props = defineProps<{
  units: Unit[];
  departments: string[];
  categories: string[];
  operator: string;
  autoSaveMode: AutoSaveMode;
  issueNumberRule: { prefix: string; suffix: string };
}>();
const emit = defineEmits<{ unitsChanged: [] }>();

type TreeView = "unit" | "department";
type DepartmentGroup = { name: string; issues: Array<Issue & { unit_name: string }> };
type CopyScope = "project" | "unit" | "issue";
type CopyField = "seq" | "unit_name" | "department" | "category" | "defect_type" | "defect_desc" | "amount" | "regulation_basis" | "suggestion" | "author" | "reviewer" | "status" | "file_count";

const treeView = ref<TreeView>((localStorage.getItem("audit_tree_view_v3") as TreeView) || "unit");
const selectedUnitId = ref<number | null>(null);
const issuesByUnit = ref<Record<string, Issue[]>>({});
const current = ref<Issue | null>(null);
const newlyCreatedIssueId = ref<number | null>(null);
const loading = ref(false);
const editor = ref<{
  confirmLeave: () => Promise<boolean>;
  hasUnsavedChanges: () => boolean;
  openDuplicateDialog: () => Promise<void>;
  openVersionHistory: () => Promise<void>;
  saveAsTemplate: () => Promise<void>;
} | null>(null);
const departmentCreateVisible = ref(false);
const departmentForCreate = ref("");
const departmentUnitId = ref<number | null>(null);
const copyVisible = ref(false);
const shortcutSettingsVisible = ref(false);
const workspaceShortcuts = ref<WorkspaceShortcuts>(readWorkspaceShortcuts());
const shortcutDraft = ref<WorkspaceShortcuts>(cloneWorkspaceShortcuts(workspaceShortcuts.value));
const templateVisible = ref(false);
const templates = ref<WorkpaperTemplate[]>([]);
const templateTargetUnitId = ref<number | null>(null);
const templateWorking = ref(false);
const copyScope = ref<CopyScope>("project");
const copyFields = ref<CopyField[]>(["seq", "unit_name", "department", "category", "defect_type", "defect_desc", "amount", "suggestion", "status"]);
const collapsedUnits = ref<number[]>(readStoredArray<number>("audit_collapsed_units_v3"));
const collapsedDepartments = ref<string[]>(readStoredArray<string>("audit_collapsed_departments_v3"));
const issueListHidden = ref(localStorage.getItem("audit_issue_list_hidden_v3") === "1");
const leftWidth = ref(readStoredWidth("audit_col_left_v3", 300));
const rightWidth = ref(readStoredWidth("audit_col_right_v3", 360));
const viewportWidth = ref(window.innerWidth);
const exchangeVisible = ref(false);
const evidenceRefreshKey = ref(0);
const draggedUnitId = ref<number | null>(null);
const draggedIssue = ref<{ unitId: number; issueId: number } | null>(null);
const unitDropTargetId = ref<number | null>(null);
const issueDropTargetId = ref<number | null>(null);
const orderSaving = ref(false);
let stopResize: (() => void) | undefined;

function cloneWorkspaceShortcuts(source: WorkspaceShortcuts): WorkspaceShortcuts {
  return normalizeWorkspaceShortcuts(source);
}

function readWorkspaceShortcuts(): WorkspaceShortcuts {
  try {
    const stored = localStorage.getItem(WORKSPACE_SHORTCUT_STORAGE_KEY);
    return normalizeWorkspaceShortcuts(stored ? JSON.parse(stored) : DEFAULT_WORKSPACE_SHORTCUTS);
  } catch {
    return cloneWorkspaceShortcuts(DEFAULT_WORKSPACE_SHORTCUTS);
  }
}

const allUnitRows = computed(() => props.units.map((unit) => ({
  unit,
  issues: issuesByUnit.value[String(unit.id)] ?? [],
})));
const departmentGroups = computed<DepartmentGroup[]>(() => {
  const groups = new Map<string, DepartmentGroup>();
  for (const { unit, issues } of allUnitRows.value) {
    for (const issue of issues) {
      const name = issue.department.trim() || "未分版块";
      const group = groups.get(name) ?? { name, issues: [] };
      group.issues.push({ ...issue, unit_name: unit.name });
      groups.set(name, group);
    }
  }
  const presetOrder = new Map(props.departments.map((name, index) => [name, index]));
  return [...groups.values()].sort((a, b) => {
    const orderA = presetOrder.get(a.name) ?? Number.MAX_SAFE_INTEGER;
    const orderB = presetOrder.get(b.name) ?? Number.MAX_SAFE_INTEGER;
    return orderA - orderB || a.name.localeCompare(b.name, "zh-CN");
  });
});
const selectedUnit = computed(() => props.units.find((unit) => unit.id === selectedUnitId.value) ?? null);
const totalIssueCount = computed(() => allUnitRows.value.reduce((total, row) => total + row.issues.length, 0));
const exchangeIssueItems = computed(() => allUnitRows.value.flatMap(({ unit, issues }) => issues.map((issue) => ({
  ...issue, unit_name: unit.name,
}))));
const listSummary = computed(() => {
  return treeView.value === "unit"
    ? `${props.units.length} 个单位 · ${totalIssueCount.value} 个底稿`
    : `${departmentGroups.value.length} 个版块 · ${totalIssueCount.value} 个底稿`;
});
const compactLayout = computed(() => viewportWidth.value < 1280);
const workspaceStyle = computed(() => {
  if (issueListHidden.value) {
    return { gridTemplateColumns: compactLayout.value ? "minmax(0, 1fr)" : `minmax(420px, 1fr) ${rightWidth.value}px` };
  }
  return compactLayout.value ? {} : {
    gridTemplateColumns: `${leftWidth.value}px minmax(420px, 1fr) ${rightWidth.value}px`,
  };
});
const copyFieldOptions: Array<{ value: CopyField; label: string }> = [
  { value: "seq", label: "序号" }, { value: "unit_name", label: "被审计单位" },
  { value: "department", label: "所属版块" }, { value: "category", label: "问题分类" },
  { value: "defect_type", label: "缺陷定性" }, { value: "defect_desc", label: "缺陷描述" },
  { value: "amount", label: "问题金额" }, { value: "regulation_basis", label: "制度依据" },
  { value: "suggestion", label: "审计建议" }, { value: "author", label: "编制人" },
  { value: "reviewer", label: "审核人" }, { value: "status", label: "底稿状态" },
  { value: "file_count", label: "附件数" },
];
const copyRows = computed(() => {
  if (copyScope.value === "unit") {
    return allUnitRows.value.filter((row) => row.unit.id === selectedUnitId.value);
  }
  if (copyScope.value === "issue" && current.value) {
    return allUnitRows.value
      .filter((row) => row.unit.id === current.value?.unit_id)
      .map((row) => ({ ...row, issues: row.issues.filter((issue) => issue.id === current.value?.id) }));
  }
  return allUnitRows.value;
});
const copyText = computed(() => {
  const options = copyFieldOptions.filter((option) => copyFields.value.includes(option.value));
  if (!options.length) return "";
  const escapeCell = (value: unknown) => String(value ?? "").replace(/[\t\r\n]+/g, " ");
  const lines = [options.map((option) => option.label).join("\t")];
  for (const row of copyRows.value) {
    for (const issue of row.issues) {
      const values: Record<CopyField, unknown> = {
        seq: formatNo(issue.seq), unit_name: row.unit.name, department: issue.department, category: issue.category,
        defect_type: issue.defect_type, defect_desc: issue.defect_desc, amount: issue.amount,
        regulation_basis: issue.regulation_basis, suggestion: issue.suggestion, author: issue.author,
        reviewer: issue.reviewer, status: issue.status, file_count: issue.file_count ?? 0,
      };
      lines.push(options.map((option) => escapeCell(values[option.value])).join("\t"));
    }
  }
  return lines.join("\n");
});

function readStoredArray<T>(key: string): T[] {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function readStoredWidth(key: string, fallback: number): number {
  const value = Number(localStorage.getItem(key));
  return Number.isFinite(value) && value >= 220 && value <= 520 ? value : fallback;
}

function persistCollapsed(): void {
  localStorage.setItem("audit_collapsed_units_v3", JSON.stringify(collapsedUnits.value));
  localStorage.setItem("audit_collapsed_departments_v3", JSON.stringify(collapsedDepartments.value));
}

function toggleIssueList(): void {
  issueListHidden.value = !issueListHidden.value;
  localStorage.setItem("audit_issue_list_hidden_v3", issueListHidden.value ? "1" : "0");
}

function updateViewport(): void {
  viewportWidth.value = window.innerWidth;
}

function toggleUnit(unitId: number): void {
  collapsedUnits.value = collapsedUnits.value.includes(unitId)
    ? collapsedUnits.value.filter((id) => id !== unitId)
    : [...collapsedUnits.value, unitId];
  persistCollapsed();
}

function toggleDepartment(department: string): void {
  collapsedDepartments.value = collapsedDepartments.value.includes(department)
    ? collapsedDepartments.value.filter((name) => name !== department)
    : [...collapsedDepartments.value, department];
  persistCollapsed();
}

function startResize(side: "left" | "right", event: MouseEvent): void {
  if (compactLayout.value) return;
  stopResize?.();
  const startX = event.clientX;
  const initial = side === "left" ? leftWidth.value : rightWidth.value;
  const onMove = (move: MouseEvent) => {
    const delta = move.clientX - startX;
    const next = Math.max(220, Math.min(520, side === "left" ? initial + delta : initial - delta));
    if (side === "left") leftWidth.value = next;
    else rightWidth.value = next;
  };
  const onUp = () => {
    localStorage.setItem(side === "left" ? "audit_col_left_v3" : "audit_col_right_v3", String(side === "left" ? leftWidth.value : rightWidth.value));
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    stopResize = undefined;
  };
  stopResize = onUp;
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
}

onMounted(() => {
  window.addEventListener("resize", updateViewport);
  window.addEventListener("keydown", onWorkspaceKeydown);
});
onBeforeUnmount(() => {
  stopResize?.();
  window.removeEventListener("resize", updateViewport);
  window.removeEventListener("keydown", onWorkspaceKeydown);
});

function report(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : "底稿操作失败，请重试");
}

async function loadTree(): Promise<void> {
  loading.value = true;
  try {
    try {
      issuesByUnit.value = await api.issueTree();
    } catch (error) {
      // V3 聚合接口不存在时，退回 V2 已有的逐单位读取。这样项目已经打开时
      // 不会因一个可优化接口的 404 而给用户弹出“Not Found”。
      if (!(error instanceof Error) || error.message !== "Not Found") throw error;
      const entries = await Promise.all(props.units.map(async (unit) => [String(unit.id), await api.issues(unit.id)] as const));
      issuesByUnit.value = Object.fromEntries(entries);
    }
    if (current.value) {
      const stillExists = Object.values(issuesByUnit.value).flat().some((issue) => issue.id === current.value?.id);
      if (!stillExists) {
        if (newlyCreatedIssueId.value === current.value.id) newlyCreatedIssueId.value = null;
        current.value = null;
      }
    }
  } catch (error) {
    report(error);
  } finally {
    loading.value = false;
  }
}

watch(() => props.units, async (units) => {
  if (!units.some((unit) => unit.id === selectedUnitId.value)) selectedUnitId.value = units[0]?.id ?? null;
  await loadTree();
}, { immediate: true, deep: true });

function switchView(view: TreeView): void {
  treeView.value = view;
  localStorage.setItem("audit_tree_view_v3", view);
}

function formatNo(seq: number | string): string {
  return formatIssueNo(seq, props.issueNumberRule);
}

async function select(issue: Issue): Promise<boolean> {
  if (current.value?.id === issue.id) return true;
  if (current.value && editor.value && !(await editor.value.confirmLeave())) return false;
  try {
    selectedUnitId.value = issue.unit_id;
    current.value = await api.issue(issue.id);
    return true;
  } catch (error) {
    report(error);
    return false;
  }
}

async function create(unitId = selectedUnitId.value, department = ""): Promise<void> {
  if (!unitId) { ElMessage.warning("请先建立并选择被审计单位"); return; }
  if (current.value && editor.value && !(await editor.value.confirmLeave())) return;
  try {
    selectedUnitId.value = unitId;
    const created = await api.createIssue(unitId, { author: props.operator, ...(department ? { department } : {}) });
    newlyCreatedIssueId.value = created.id;
    await loadTree();
    current.value = await api.issue(created.id);
    ElMessage.success("已新建草稿底稿");
  } catch (error) {
    report(error);
  }
}

async function navigateIssue(offset: -1 | 1): Promise<void> {
  const visibleIssues = allUnitRows.value.flatMap((row) => row.issues);
  if (!visibleIssues.length) {
    ElMessage.info("当前范围没有可定位的底稿");
    return;
  }
  const currentIndex = current.value ? visibleIssues.findIndex((issue) => issue.id === current.value?.id) : -1;
  const nextIndex = currentIndex < 0
    ? (offset > 0 ? 0 : visibleIssues.length - 1)
    : (currentIndex + offset + visibleIssues.length) % visibleIssues.length;
  await select(visibleIssues[nextIndex]);
}

async function issueMoreCommand(issue: Issue, command: string): Promise<void> {
  if (!(await select(issue))) return;
  await nextTick();
  if (command === "duplicate") await editor.value?.openDuplicateDialog();
  if (command === "versions") await editor.value?.openVersionHistory();
  if (command === "template") await editor.value?.saveAsTemplate();
  if (command === "delete") await deleteIssue(issue);
}

function onWorkspaceKeydown(event: KeyboardEvent): void {
  const shortcut = resolveWorkspaceShortcut(event, workspaceShortcuts.value);
  if (!shortcut) return;
  event.preventDefault();
  void runWorkspaceShortcut(shortcut);
}

async function runWorkspaceShortcut(shortcut: WorkspaceShortcut): Promise<void> {
  switch (shortcut) {
    case "new-issue": await create(); break;
    case "next-issue": await navigateIssue(1); break;
    case "previous-issue": await navigateIssue(-1); break;
    case "toggle-issue-list": toggleIssueList(); break;
  }
}

async function copiedIssue(issue: Issue): Promise<void> {
  selectedUnitId.value = issue.unit_id;
  newlyCreatedIssueId.value = issue.id;
  await loadTree();
  current.value = issue;
}

async function openTemplateDialog(): Promise<void> {
  templateTargetUnitId.value = props.units.some((unit) => unit.id === selectedUnitId.value)
    ? selectedUnitId.value
    : props.units[0]?.id ?? null;
  templateVisible.value = true;
  try {
    templates.value = await api.workpaperTemplates();
  } catch (error) {
    report(error);
  }
}

function openShortcutSettings(): void {
  shortcutDraft.value = cloneWorkspaceShortcuts(workspaceShortcuts.value);
  shortcutSettingsVisible.value = true;
}

function saveWorkspaceShortcuts(): void {
  workspaceShortcuts.value = normalizeWorkspaceShortcuts(shortcutDraft.value);
  shortcutDraft.value = cloneWorkspaceShortcuts(workspaceShortcuts.value);
  localStorage.setItem(WORKSPACE_SHORTCUT_STORAGE_KEY, JSON.stringify(workspaceShortcuts.value));
  shortcutSettingsVisible.value = false;
  ElMessage.success("工作区快捷键已保存");
}

function resetWorkspaceShortcuts(): void {
  shortcutDraft.value = cloneWorkspaceShortcuts(DEFAULT_WORKSPACE_SHORTCUTS);
}

function shortcutKeyIsArrow(key: string): boolean {
  return key === "ArrowUp" || key === "ArrowDown";
}

function normalizeShortcutDraftInput(action: WorkspaceShortcut): void {
  const binding = shortcutDraft.value[action];
  binding.key = binding.key.trim();
  binding.altKey = !shortcutKeyIsArrow(binding.key);
}

async function applyTemplate(template: WorkpaperTemplate): Promise<void> {
  if (!templateTargetUnitId.value || templateWorking.value) return;
  if (current.value && editor.value && !(await editor.value.confirmLeave())) return;
  templateWorking.value = true;
  try {
    const issue = await api.applyWorkpaperTemplate(template.id, templateTargetUnitId.value);
    templateVisible.value = false;
    await copiedIssue(issue);
    ElMessage.success(`已按模板“${template.name}”新建草稿底稿`);
  } catch (error) {
    report(error);
  } finally {
    templateWorking.value = false;
  }
}

async function deleteTemplate(template: WorkpaperTemplate): Promise<void> {
  if (templateWorking.value) return;
  try {
    await ElMessageBox.confirm(`删除模板“${template.name}”不会影响已按该模板创建的底稿。`, "删除底稿模板", {
      type: "warning", confirmButtonText: "删除模板", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  templateWorking.value = true;
  try {
    await api.deleteWorkpaperTemplate(template.id);
    templates.value = templates.value.filter((item) => item.id !== template.id);
    ElMessage.success("底稿模板已删除");
  } catch (error) {
    report(error);
  } finally {
    templateWorking.value = false;
  }
}

function createInDepartment(department: string): void {
  departmentForCreate.value = department === "未分版块" ? "" : department;
  departmentUnitId.value = props.units.some((unit) => unit.id === selectedUnitId.value)
    ? selectedUnitId.value
    : props.units[0]?.id ?? null;
  departmentCreateVisible.value = true;
}

async function confirmDepartmentCreate(): Promise<void> {
  if (!departmentUnitId.value) {
    ElMessage.warning("请选择被审计单位");
    return;
  }
  departmentCreateVisible.value = false;
  await create(departmentUnitId.value, departmentForCreate.value);
}

async function addUnit(): Promise<void> {
  try {
    const result = await ElMessageBox.prompt("新建单位后，可在单位或版块视图中继续编制底稿。", "新增被审计单位", {
      inputPlaceholder: "例如：华电集团XX电厂",
      inputValidator: (value) => Boolean(value?.trim()) || "单位名称不能为空",
      confirmButtonText: "新增", cancelButtonText: "取消",
    });
    const created = await api.addUnit(result.value.trim());
    selectedUnitId.value = created.id;
    emit("unitsChanged");
    ElMessage.success("单位已新增");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

async function renameUnit(unit: Unit): Promise<void> {
  try {
    const result = await ElMessageBox.prompt("单位名称会更新在项目树和后续导出中；附件物理目录不随名称变化。", "重命名被审计单位", {
      inputValue: unit.name, inputValidator: (value) => Boolean(value?.trim()) || "单位名称不能为空",
      confirmButtonText: "保存", cancelButtonText: "取消",
    });
    await api.renameUnit(unit.id, result.value.trim());
    emit("unitsChanged");
    ElMessage.success("单位已重命名");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

async function deleteUnit(unit: Unit, issueCount: number): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将删除“${unit.name}”及其 ${issueCount} 条底稿和本单位附件。若附件仍被其他单位底稿引用，系统会拒绝删除以保护审计证据。`,
      "删除被审计单位",
      { type: "warning", confirmButtonText: "删除单位", cancelButtonText: "取消" },
    );
    await api.deleteUnit(unit.id);
    if (current.value?.unit_id === unit.id) current.value = null;
    if (selectedUnitId.value === unit.id) selectedUnitId.value = props.units.find((item) => item.id !== unit.id)?.id ?? null;
    await loadTree();
    emit("unitsChanged");
    ElMessage.success("单位已删除");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

function startUnitDrag(unitId: number, event: DragEvent): void {
  if (treeView.value !== "unit" || orderSaving.value) return;
  draggedUnitId.value = unitId;
  event.dataTransfer?.setData("text/plain", `unit:${unitId}`);
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}

function clearUnitDrag(): void {
  draggedUnitId.value = null;
  unitDropTargetId.value = null;
}

async function dropUnitBefore(targetId: number): Promise<void> {
  const sourceId = draggedUnitId.value;
  clearUnitDrag();
  if (!sourceId || sourceId === targetId || orderSaving.value) return;
  const ids = moveIdBefore(props.units.map((unit) => unit.id), sourceId, targetId);
  if (ids === props.units.map((unit) => unit.id)) return;
  orderSaving.value = true;
  try {
    await api.reorderUnits(ids);
    emit("unitsChanged");
    ElMessage.success("被审单位排序已保存");
  } catch (error) {
    report(error);
  } finally {
    orderSaving.value = false;
  }
}

function startIssueDrag(unitId: number, issueId: number, event: DragEvent): void {
  if (treeView.value !== "unit" || orderSaving.value) return;
  draggedIssue.value = { unitId, issueId };
  event.dataTransfer?.setData("text/plain", `issue:${issueId}`);
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}

function clearIssueDrag(): void {
  draggedIssue.value = null;
  issueDropTargetId.value = null;
}

async function dropIssueBefore(unitId: number, targetId: number): Promise<void> {
  const source = draggedIssue.value;
  clearIssueDrag();
  if (!source || source.unitId !== unitId || source.issueId === targetId || orderSaving.value) return;
  const currentIds = (issuesByUnit.value[String(unitId)] ?? []).map((issue) => issue.id);
  const ids = moveIdBefore(currentIds, source.issueId, targetId);
  if (ids === currentIds) return;
  orderSaving.value = true;
  try {
    await api.reorderIssues(unitId, ids);
    await loadTree();
    ElMessage.success("底稿排序已保存；底稿编号不变");
  } catch (error) {
    report(error);
  } finally {
    orderSaving.value = false;
  }
}

async function updated(issue: Issue): Promise<void> {
  current.value = issue;
  if (newlyCreatedIssueId.value === issue.id && issue.department.trim() && issue.defect_type.trim()) {
    newlyCreatedIssueId.value = null;
  }
  await loadTree();
}

async function openExchange(): Promise<void> {
  const target = current.value ?? exchangeIssueItems.value[0];
  if (!target) {
    ElMessage.warning("项目中暂无可交流的底稿");
    return;
  }
  if (current.value && editor.value && !(await editor.value.confirmLeave())) return;
  try {
    current.value = await api.issue(target.id);
    selectedUnitId.value = current.value.unit_id;
    exchangeVisible.value = true;
  } catch (error) {
    report(error);
  }
}

async function exchangeApplied(issue: Issue): Promise<void> {
  await updated(issue);
}

async function exchangeEvidenceChanged(): Promise<void> {
  evidenceRefreshKey.value += 1;
  await loadTree();
}

function discardedIssue(issueId: number): void {
  if (newlyCreatedIssueId.value === issueId) newlyCreatedIssueId.value = null;
  if (current.value?.id === issueId) current.value = null;
  void loadTree();
}

async function deleteIssue(issue: Issue): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将把问题 ${issue.seq}“${issue.defect_type || "未定性"}”移入回收站；版本和附件关联将保留，可在项目菜单的回收站恢复。`,
      "移入回收站",
      { type: "warning", confirmButtonText: "移入回收站", cancelButtonText: "取消" },
    );
    await api.deleteIssue(issue.id);
    if (current.value?.id === issue.id) current.value = null;
    if (newlyCreatedIssueId.value === issue.id) newlyCreatedIssueId.value = null;
    await loadTree();
    ElMessage.success("底稿已移入回收站，可在项目菜单恢复");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

function openCopyDialog(): void {
  if (!selectedUnitId.value) copyScope.value = "project";
  copyVisible.value = true;
}

function toggleCopyField(field: CopyField, checked: boolean): void {
  copyFields.value = checked
    ? [...new Set([...copyFields.value, field])]
    : copyFields.value.filter((item) => item !== field);
}

function selectAllCopyFields(): void {
  copyFields.value = copyFieldOptions.map((option) => option.value);
}

function clearCopyFields(): void {
  copyFields.value = [];
}

async function copyIssues(): Promise<void> {
  if (!copyFields.value.length) {
    ElMessage.warning("请至少选择一个字段");
    return;
  }
  if (copyText.value.split("\n").length < 2) {
    ElMessage.warning(copyScope.value === "unit" ? "当前单位暂无底稿可复制" : "项目暂无底稿可复制");
    return;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(copyText.value);
    } else {
      const area = document.createElement("textarea");
      area.value = copyText.value;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      if (!copied) throw new Error("浏览器未授予剪贴板权限");
    }
    copyVisible.value = false;
    ElMessage.success("已复制为制表符文本，可直接粘贴到 Excel 或工作底稿");
  } catch (error) {
    report(error instanceof Error ? error : new Error("复制失败，请检查浏览器剪贴板权限"));
  }
}

async function confirmCurrentLeave(): Promise<boolean> {
  if (!current.value || !editor.value) return true;
  return editor.value.confirmLeave();
}

function hasUnsavedChanges(): boolean {
  return Boolean(current.value && editor.value?.hasUnsavedChanges());
}

async function selectIssueById(issueId: number): Promise<void> {
  try {
    const issue = await api.issue(issueId);
    if (current.value?.id === issue.id) return;
    if (current.value && editor.value && !(await editor.value.confirmLeave())) return;
    selectedUnitId.value = issue.unit_id;
    current.value = issue;
  } catch (error) {
    report(error);
  }
}

function selectUnit(unitId: number): void {
  treeView.value = "unit";
  selectedUnitId.value = unitId;
  current.value = null;
}

defineExpose({ confirmCurrentLeave, hasUnsavedChanges, selectIssueById, selectUnit, openExchange, openTemplateDialog, openShortcutSettings });
</script>

<template>
  <section class="workspace" :class="{ 'issue-list-hidden': issueListHidden, 'compact-layout': compactLayout }" :style="workspaceStyle">
    <button v-if="issueListHidden" type="button" class="issue-list-restore" title="展开被审单位和问题列表" aria-label="展开被审单位和问题列表" @click="toggleIssueList">▸</button>
    <aside v-show="!issueListHidden" class="issue-list panel">
      <div class="panel-head"><div><p class="eyebrow">审计底稿</p><h2>问题列表</h2></div><span class="tree-header-actions"><el-button size="small" title="复制底稿字段到剪贴板" @click="openCopyDialog">⧉ 复制</el-button><el-button type="primary" size="small" @click="addUnit">新增单位</el-button><el-button text size="small" class="issue-list-toggle" title="折叠被审单位和问题列表" aria-label="折叠被审单位和问题列表" @click="toggleIssueList">⇤</el-button></span></div>
      <div class="tree-view-tabs"><button :class="{ active: treeView === 'unit' }" @click="switchView('unit')">按单位</button><button :class="{ active: treeView === 'department' }" @click="switchView('department')">按版块</button></div>
      <p class="tree-summary">{{ listSummary }}</p>

      <el-empty v-if="!units.length" description="请先建立被审计单位" :image-size="72" />
      <template v-else-if="treeView === 'unit'">
        <div v-for="row in allUnitRows" :key="row.unit.id" class="tree-group" :class="{ 'tree-drop-target': unitDropTargetId === row.unit.id }" @dragover.prevent="unitDropTargetId = row.unit.id" @dragleave="unitDropTargetId = null" @drop.prevent="dropUnitBefore(row.unit.id)">
          <div class="tree-group-head" :class="{ selected: selectedUnitId === row.unit.id }"><button class="tree-toggle" :aria-expanded="!collapsedUnits.includes(row.unit.id)" :title="collapsedUnits.includes(row.unit.id) ? '展开单位' : '折叠单位'" @click="toggleUnit(row.unit.id)">{{ collapsedUnits.includes(row.unit.id) ? '▸' : '▾' }}</button><span class="sort-drag-handle" :draggable="!orderSaving" title="拖动调整单位顺序" aria-label="拖动调整单位顺序" @dragstart.stop="startUnitDrag(row.unit.id, $event)" @dragend="clearUnitDrag">⠿</span><button class="tree-unit-label" @click="selectedUnitId = row.unit.id">🏢 {{ row.unit.name }}<small>{{ row.issues.length }} 条</small></button><span class="tree-group-tools"><el-button text size="small" title="在该单位新建底稿" @click="create(row.unit.id)">＋</el-button><el-button text size="small" title="重命名单位" @click="renameUnit(row.unit)">✎</el-button><el-button text type="danger" size="small" title="删除单位" @click="deleteUnit(row.unit, row.issues.length)">✕</el-button></span></div>
          <div v-for="issue in row.issues" v-show="!collapsedUnits.includes(row.unit.id)" :key="issue.id" class="issue-list-row" :class="{ 'tree-drop-target': issueDropTargetId === issue.id }" @dragover.prevent="issueDropTargetId = issue.id" @dragleave="issueDropTargetId = null" @drop.prevent="dropIssueBefore(row.unit.id, issue.id)"><button class="issue-item sortable-issue-item" :class="{ active: current?.id === issue.id }" @click="select(issue)"><span class="sort-drag-handle" :draggable="!orderSaving" title="拖动调整底稿顺序" aria-label="拖动调整底稿顺序" @dragstart.stop="startIssueDrag(row.unit.id, issue.id, $event)" @dragend="clearIssueDrag" @click.stop>⠿</span><span class="issue-number">{{ formatNo(issue.seq) }}</span><span><strong>{{ issue.defect_type || '未定性' }}</strong><small>{{ issue.department || '未分版块' }} · 附件 {{ issue.file_count ?? 0 }}</small></span></button><el-dropdown trigger="click" @command="(command: string) => issueMoreCommand(issue, command)"><el-button text size="small" class="issue-more" title="底稿操作菜单" aria-label="底稿操作菜单" @click.stop>▾</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="duplicate">复制为新底稿</el-dropdown-item><el-dropdown-item command="versions">版本历史</el-dropdown-item><el-dropdown-item command="template">保存为模板</el-dropdown-item><el-dropdown-item divided command="delete">移入回收站</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div>
        </div>
      </template>
      <template v-else>
        <div v-if="!departmentGroups.length" class="tree-empty">暂无底稿</div>
        <div v-for="group in departmentGroups" :key="group.name" class="tree-group"><div class="tree-group-title"><span><button class="tree-toggle" :aria-expanded="!collapsedDepartments.includes(group.name)" :title="collapsedDepartments.includes(group.name) ? '展开版块' : '折叠版块'" @click="toggleDepartment(group.name)">{{ collapsedDepartments.includes(group.name) ? '▸' : '▾' }}</button>📂 {{ group.name }}<small>{{ group.issues.length }} 条</small></span><el-button text size="small" title="选择单位后新建并预填版块" @click="createInDepartment(group.name)">＋</el-button></div><div v-for="issue in group.issues" v-show="!collapsedDepartments.includes(group.name)" :key="issue.id" class="issue-list-row"><button class="issue-item" :class="{ active: current?.id === issue.id }" @click="select(issue)"><span class="issue-number">{{ formatNo(issue.seq) }}</span><span><strong>{{ issue.defect_type || '未定性' }}</strong><small>{{ issue.unit_name }} · 附件 {{ issue.file_count ?? 0 }}</small></span></button><el-dropdown trigger="click" @command="(command: string) => issueMoreCommand(issue, command)"><el-button text size="small" class="issue-more" title="底稿操作菜单" aria-label="底稿操作菜单" @click.stop>▾</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="duplicate">复制为新底稿</el-dropdown-item><el-dropdown-item command="versions">版本历史</el-dropdown-item><el-dropdown-item command="template">保存为模板</el-dropdown-item><el-dropdown-item divided command="delete">移入回收站</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></div>
      </template>
    </aside>

    <IssueEditor v-if="current" ref="editor" :issue="current" :units="units" :departments="departments" :categories="categories" :auto-save-mode="autoSaveMode" :is-new="newlyCreatedIssueId === current.id" :issue-number-rule="issueNumberRule" @updated="updated" @copied="copiedIssue" @discarded="discardedIssue" @delete-requested="deleteIssue" />
    <EvidencePanel v-if="current" :key="`${current.id}-${evidenceRefreshKey}`" :issue="current" :units="units" @changed="loadTree" />
    <article v-if="!current" class="empty-editor panel"><el-empty :description="selectedUnit ? '选择或新建底稿后开始编制' : '请先选择被审计单位'" /></article>
    <div class="workspace-resizer left" :style="{ left: `${leftWidth}px` }" title="拖拽调整问题列表宽度" @mousedown.prevent="startResize('left', $event)" />
    <div class="workspace-resizer right" :style="{ right: `${rightWidth}px` }" title="拖拽调整附件列表宽度" @mousedown.prevent="startResize('right', $event)" />

    <el-dialog v-model="departmentCreateVisible" title="在版块中新建底稿" width="min(420px, calc(100vw - 32px))" append-to-body>
      <div class="operation-panel">
        <p>按版块视图跨多个单位聚合，创建前必须确认归属单位。</p>
        <label>所属版块<el-input :model-value="departmentForCreate || '未分版块'" disabled /></label>
        <label>被审计单位 *<el-select v-model="departmentUnitId" placeholder="请选择单位"><el-option v-for="unit in units" :key="unit.id" :label="unit.name" :value="unit.id" /></el-select></label>
      </div>
      <template #footer><el-button @click="departmentCreateVisible = false">取消</el-button><el-button type="primary" @click="confirmDepartmentCreate">新建底稿</el-button></template>
    </el-dialog>
    <el-dialog v-model="copyVisible" title="复制底稿字段" width="min(620px, calc(100vw - 32px))" append-to-body>
      <div class="operation-panel copy-panel">
        <p>复制结果为制表符分隔文本，粘贴到 Excel、Word 表格或审计工作底稿即可保持列结构。</p>
        <div class="copy-option-head"><strong>导出范围</strong><span>按需选择，不会改变底稿数据</span></div>
        <div class="copy-scope-options"><label><input v-model="copyScope" type="radio" value="project" /> 全部单位（{{ totalIssueCount }} 条）</label><label><input v-model="copyScope" type="radio" value="unit" :disabled="!selectedUnit" /> 当前单位{{ selectedUnit ? `（${selectedUnit.name}）` : "" }}</label><label><input v-model="copyScope" type="radio" value="issue" :disabled="!current" /> 当前底稿{{ current ? `（问题 ${current.seq}）` : "" }}</label></div>
        <div class="copy-option-head"><strong>导出字段</strong><span><el-button text size="small" @click="selectAllCopyFields">全选字段</el-button><el-button text size="small" @click="clearCopyFields">清空</el-button></span></div>
        <div class="copy-field-options"><label v-for="option in copyFieldOptions" :key="option.value"><input type="checkbox" :checked="copyFields.includes(option.value)" @change="toggleCopyField(option.value, ($event.target as HTMLInputElement).checked)" /> {{ option.label }}</label></div>
        <pre class="copy-preview">{{ copyText || "请选择字段后预览" }}</pre>
      </div>
      <template #footer><el-button @click="copyVisible = false">取消</el-button><el-button type="primary" :disabled="!copyFields.length" @click="copyIssues">复制到剪贴板</el-button></template>
    </el-dialog>
    <el-dialog v-model="shortcutSettingsVisible" title="工作区快捷键" width="min(560px, calc(100vw - 32px))" append-to-body>
      <p class="shortcut-hint">仅在非输入区域生效。方向键可直接使用；字母和数字必须搭配 Alt，不会接管 Ctrl、Command 或编辑器按键。</p>
      <div class="shortcut-list shortcut-settings-list"><div v-for="item in workspaceShortcutLabels" :key="item.action"><span>{{ item.description }}</span><el-select v-model="shortcutDraft[item.action].key" filterable allow-create default-first-option aria-label="快捷键按键" @change="normalizeShortcutDraftInput(item.action)"><el-option label="↑ 上方向键" value="ArrowUp" /><el-option label="↓ 下方向键" value="ArrowDown" /><el-option v-for="key in 'abcdefghijklmnopqrstuvwxyz0123456789'.split('')" :key="key" :label="key.toUpperCase()" :value="key" /></el-select><span class="shortcut-alt">{{ shortcutKeyIsArrow(shortcutDraft[item.action].key) ? '无修饰键' : 'Alt' }}</span><kbd>{{ formatShortcut(shortcutDraft[item.action]) }}</kbd></div></div>
      <template #footer><el-button @click="resetWorkspaceShortcuts">恢复默认</el-button><el-button @click="shortcutSettingsVisible = false">取消</el-button><el-button type="primary" @click="saveWorkspaceShortcuts">保存</el-button></template>
    </el-dialog>
    <el-dialog v-model="templateVisible" title="项目内底稿模板" width="min(680px, calc(100vw - 32px))" append-to-body>
      <p class="template-hint">模板只复用通用编制内容；人员、状态、附件、版本和交流记录不会带入新底稿。</p>
      <label class="template-target">目标被审计单位<el-select v-model="templateTargetUnitId" filterable placeholder="请选择单位"><el-option v-for="unit in units" :key="unit.id" :label="unit.name" :value="unit.id" /></el-select></label>
      <el-empty v-if="!templates.length" description="暂无模板。可在任一已编制底稿中点击“保存为模板”。" :image-size="58" />
      <div v-else class="template-list"><article v-for="template in templates" :key="template.id"><div><strong>{{ template.name }}</strong><small>{{ template.data.department || '未分版块' }} · {{ template.data.category || '未分类' }} · {{ template.updated_at }} 由 {{ template.updated_by }}</small><p>{{ template.data.defect_type || '未定性底稿' }}</p></div><span><el-button size="small" :disabled="!templateTargetUnitId" :loading="templateWorking" type="primary" @click="applyTemplate(template)">套用</el-button><el-button text size="small" type="danger" :disabled="templateWorking" @click="deleteTemplate(template)">删除</el-button></span></article></div>
    </el-dialog>
    <ExchangeWorkbench v-if="exchangeVisible && current" :issue="current" :issue-items="exchangeIssueItems" :issue-number-rule="issueNumberRule" @applied="exchangeApplied" @evidence-changed="exchangeEvidenceChanged" @close="exchangeVisible = false" />
  </section>
</template>
