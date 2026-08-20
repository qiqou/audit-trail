<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type Issue, type IssueChanges, type IssuePatch, type IssueStatus, type Unit } from "../api/client";
import { formatIssueNo } from "../format";
import ReviewNotes from "./ReviewNotes.vue";
import VersionHistory from "./VersionHistory.vue";

type AutoSaveMode = "realtime" | "5m" | "20m";
type PlainIssueDraft = Required<Pick<IssueChanges,
  "department" | "category" | "defect_type" | "defect_desc" | "amount" | "regulation_basis" | "suggestion" | "author" | "reviewer"
>> & { currency: string; amount_unit: string };

const props = defineProps<{
  issue: Issue;
  units: Unit[];
  departments: string[];
  categories: string[];
  autoSaveMode: AutoSaveMode;
  isNew: boolean;
  issueNumberRule: { prefix: string; suffix: string };
}>();
const emit = defineEmits<{
  updated: [issue: Issue];
  copied: [issue: Issue];
  deleteRequested: [issue: Issue];
  discarded: [issueId: number];
}>();

const saving = ref(false);
const duplicateVisible = ref(false);
const duplicateTargetUnitId = ref<number | null>(null);
const versionHistory = ref<{ open: () => Promise<void> } | null>(null);
const draft = reactive<PlainIssueDraft>({
  department: "", category: "", defect_type: "", defect_desc: "", amount: "", currency: "CNY", amount_unit: "元", regulation_basis: "", suggestion: "", author: "", reviewer: "",
});
const savedSignature = ref("");
const recoveryBaseline = ref<{ versionId: number; updatedAt: string } | null>(null);
const recoverySaving = ref(false);
let syncingDraft = false;
let saveTimer: ReturnType<typeof window.setTimeout> | undefined;
let recoveryTimer: ReturnType<typeof window.setTimeout> | undefined;
let recoveryLoadSequence = 0;

const transitions: Record<IssueStatus, IssueStatus[]> = {
  "草稿": ["编制完成"],
  "编制完成": ["复核退回", "已复核"],
  "复核退回": ["编制完成"],
  "已复核": ["复核退回", "已归档"],
  "已归档": ["编制完成"],
};

const isArchived = computed(() => props.issue.status === "已归档");
const allowed = computed(() => transitions[props.issue.status] ?? []);
const dirty = computed(() => draftSignature() !== savedSignature.value);
const missingRequired = computed(() => [
  !draft.department.trim() ? "所属版块" : "",
  !draft.defect_type.trim() ? "缺陷定性" : "",
].filter(Boolean));
const autoSaveDescription = computed(() => {
  if (props.autoSaveMode === "realtime") return "停止输入后自动保存";
  return props.autoSaveMode === "20m" ? "每 20 分钟自动保存" : "每 5 分钟自动保存";
});
const saveStateText = computed(() => {
  if (saving.value) return "正在保存…";
  if (dirty.value) return `有未保存修改 · ${autoSaveDescription.value}`;
  return `已保存 · ${autoSaveDescription.value} · 更新：${props.issue.updated_at}`;
});

function formatNo(seq: number | string): string {
  return formatIssueNo(seq, props.issueNumberRule);
}

// 审查 F2 修复：金额是否可设置币种/单位。老项目可能保留“120万”等自由文本，
// 自由文本期间币种/单位下拉禁用并提示（防用户修改被静默丢弃），金额改为数字后自动解锁。
const STRUCTURED_AMOUNT_RE = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
const amountIsStructured = computed(() => !draft.amount.trim() || STRUCTURED_AMOUNT_RE.test(draft.amount.trim()));
const amountFreeTextHint = computed(() =>
  amountIsStructured.value ? "" : "金额为自由文本时不可修改币种/单位，请先将金额改为数字",
);

function draftValues(): IssuePatch {
  const values: IssuePatch = {
    department: draft.department, category: draft.category, defect_type: draft.defect_type, defect_desc: draft.defect_desc,
    amount: draft.amount, regulation_basis: draft.regulation_basis, suggestion: draft.suggestion,
    author: draft.author, reviewer: draft.reviewer,
  };
  // 老项目可能保留“120万”等自由文本；未人工改为数字前不要因编辑其他字段
  // 误触结构化校验。新输入及清空金额都走后端的两位小数/币种/单位校验。
  if (amountIsStructured.value) {
    values.currency = draft.currency || "CNY";
    values.amount_unit = draft.amount_unit || "元";
  }
  return values;
}

function draftSignature(): string {
  return JSON.stringify(draftValues());
}

function clearScheduledSave(): void {
  window.clearTimeout(saveTimer);
  saveTimer = undefined;
}

function clearScheduledRecoverySave(): void {
  window.clearTimeout(recoveryTimer);
  recoveryTimer = undefined;
}

function syncDraft(issue: Issue): void {
  clearScheduledSave();
  clearScheduledRecoverySave();
  syncingDraft = true;
  Object.assign(draft, {
    department: issue.department ?? "", category: issue.category ?? "", defect_type: issue.defect_type ?? "", defect_desc: issue.defect_desc ?? "",
    amount: issue.amount ?? "", currency: issue.currency || "CNY", amount_unit: issue.amount_unit || "元", regulation_basis: issue.regulation_basis ?? "", suggestion: issue.suggestion ?? "",
    author: issue.author ?? "", reviewer: issue.reviewer ?? "",
  });
  savedSignature.value = draftSignature();
  syncingDraft = false;
}

async function refreshRecoveryBaseline(issueId = props.issue.id): Promise<void> {
  const state = await api.issueDraft(issueId);
  if (issueId !== props.issue.id) return;
  recoveryBaseline.value = {
    versionId: state.current_version_id,
    updatedAt: state.current_updated_at,
  };
}

async function loadRecoveryDraft(issue: Issue): Promise<void> {
  const sequence = ++recoveryLoadSequence;
  try {
    const state = await api.issueDraft(issue.id);
    if (sequence !== recoveryLoadSequence || issue.id !== props.issue.id) return;
    recoveryBaseline.value = {
      versionId: state.current_version_id,
      updatedAt: state.current_updated_at,
    };
    if (!state.draft) return;
    if (state.draft.conflicted) {
      try {
        await ElMessageBox.confirm(
          "检测到一份基于旧正式版本的异常恢复草稿。为避免覆盖正式底稿，系统不会自动恢复；可保留草稿待核对，或立即放弃。",
          "草稿基线已变化",
          { type: "warning", confirmButtonText: "放弃草稿", cancelButtonText: "保留草稿", distinguishCancelAndClose: true },
        );
        await api.discardIssueDraft(issue.id);
        ElMessage.info("已放弃过期草稿，正式底稿未受影响");
      } catch (error) {
        if (error !== "cancel" && error !== "close") ElMessage.error(errorMessage(error));
      }
      return;
    }
    try {
      await ElMessageBox.confirm(
        `检测到 ${state.draft.saved_at} 保存的异常恢复草稿，恢复不会立即写入正式版本。`,
        "恢复未提交草稿",
        { type: "info", confirmButtonText: "恢复草稿", cancelButtonText: "放弃草稿", distinguishCancelAndClose: true },
      );
      syncingDraft = true;
      Object.assign(draft, state.draft.payload);
      syncingDraft = false;
      ElMessage.success("草稿已恢复，请核对后手工保存为正式版本");
    } catch (error) {
      if (error === "cancel") {
        await api.discardIssueDraft(issue.id);
        ElMessage.info("已放弃恢复草稿，正式底稿未受影响");
      } else if (error !== "close") {
        ElMessage.error(errorMessage(error));
      }
    }
  } catch (error) {
    if (sequence === recoveryLoadSequence) ElMessage.error(errorMessage(error));
  }
}

function scheduleRecoverySave(): void {
  clearScheduledRecoverySave();
  if (!recoveryBaseline.value || !dirty.value || isArchived.value) return;
  recoveryTimer = window.setTimeout(() => { void persistRecoveryDraft(); }, 600);
}

async function persistRecoveryDraft(): Promise<void> {
  clearScheduledRecoverySave();
  if (!recoveryBaseline.value || !dirty.value || isArchived.value || recoverySaving.value) return;
  recoverySaving.value = true;
  try {
    const state = await api.saveIssueDraft(
      props.issue.id, draftValues(), recoveryBaseline.value.versionId, recoveryBaseline.value.updatedAt,
    );
    recoveryBaseline.value = { versionId: state.current_version_id, updatedAt: state.current_updated_at };
  } catch (error) {
    ElMessage.warning(errorMessage(error));
  } finally {
    recoverySaving.value = false;
  }
}

watch(() => props.issue, (issue) => {
  syncDraft(issue);
  void loadRecoveryDraft(issue);
}, { immediate: true });
watch(draft, () => {
  if (!syncingDraft && !isArchived.value && dirty.value) {
    scheduleRecoverySave();
    if (!saving.value) scheduleAutoSave();
  }
}, { deep: true, flush: "sync" });
watch(() => props.autoSaveMode, () => {
  clearScheduledSave();
  clearScheduledRecoverySave();
  if (dirty.value && !isArchived.value) scheduleAutoSave();
});

onMounted(() => {
  if (!props.isNew || props.issue.amount.trim()) return;
  void api.amountSettings().then((settings) => {
    // 只初始化空白新底稿，不能覆盖用户已输入或历史项目原有口径。
    if (props.isNew && !draft.amount.trim() && !dirty.value) {
      draft.currency = settings.currency;
      draft.amount_unit = settings.amount_unit;
      savedSignature.value = draftSignature();
    }
  }).catch(() => undefined);
});

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请重试";
}

function scheduleAutoSave(): void {
  const delay = props.autoSaveMode === "realtime"
    ? 1200
    : props.autoSaveMode === "20m" ? 20 * 60 * 1000 : 5 * 60 * 1000;
  // 实时模式按最后一次输入重新计时；定期模式从首次修改起计时，继续输入不延后。
  if (props.autoSaveMode === "realtime") clearScheduledSave();
  else if (saveTimer !== undefined) return;
  saveTimer = window.setTimeout(() => { void persist(false); }, delay);
}

async function persist(showMessage: boolean): Promise<boolean> {
  clearScheduledSave();
  if (!dirty.value) return true;
  if (isArchived.value) {
    if (showMessage) ElMessage.warning("已归档底稿不能直接编辑，请使用“归档后编辑”");
    return false;
  }
  if (saving.value) return false;

  const signatureBeforeSave = draftSignature();
  saving.value = true;
  try {
    const result = await api.updateIssue(props.issue.id, draftValues());
    const changedWhileSaving = draftSignature() !== signatureBeforeSave;
    savedSignature.value = signatureBeforeSave;
    // 请求期间若继续输入，不用旧响应刷新父组件，否则 props watcher 会把新输入覆盖掉。
    // 下一轮自动保存完成后再同步最新服务端结果。
    if (!changedWhileSaving) emit("updated", result.issue);
    try {
      await refreshRecoveryBaseline();
      if (!changedWhileSaving) await api.discardIssueDraft(props.issue.id);
    } catch {
      // 正式保存已成功；恢复草稿清理失败只会留下可再次选择的独立草稿，不能影响正文。
    }
    if (showMessage) ElMessage.success(result.changed ? "已保存并留存版本" : "内容没有变化");
    return true;
  } catch (error) {
    ElMessage.error(errorMessage(error));
    return false;
  } finally {
    saving.value = false;
    // 保存期间继续输入时，保持脏状态并重新安排一次保存，不能悄然覆盖新输入。
    if (dirty.value && !isArchived.value) {
      scheduleRecoverySave();
      scheduleAutoSave();
    }
  }
}

async function save(): Promise<void> {
  await persist(true);
}

async function openDuplicateDialog(): Promise<void> {
  if (saving.value) return;
  // 复制必须以当前正式内容为准，不能让用户误以为未保存输入已被带入新稿。
  if (!(await persist(false))) return;
  duplicateTargetUnitId.value = props.issue.unit_id;
  duplicateVisible.value = true;
}

async function duplicate(): Promise<void> {
  if (!duplicateTargetUnitId.value || saving.value) return;
  saving.value = true;
  try {
    const copied = await api.duplicateIssue(props.issue.id, duplicateTargetUnitId.value);
    emit("copied", copied);
    duplicateVisible.value = false;
    const targetUnit = props.units.find((unit) => unit.id === copied.unit_id);
    ElMessage.success(`已复制为“${targetUnit?.name ?? "目标单位"}”的新草稿；附件、版本、状态和交流记录未复制`);
  } catch (error) {
    ElMessage.error(errorMessage(error));
  } finally {
    saving.value = false;
  }
}

async function saveAsTemplate(): Promise<void> {
  if (saving.value) return;
  if (!(await persist(false))) return;
  try {
    const result = await ElMessageBox.prompt(
      "模板会保存底稿的版块、分类、正文、金额、制度依据和审计建议；不保存编制人、审核人、状态、附件、版本或交流记录。",
      "保存为项目内模板",
      {
        inputPlaceholder: props.issue.defect_type || "例如：收入截止测试问题",
        inputValidator: (value) => Boolean(value?.trim()) || "模板名称不能为空",
        confirmButtonText: "保存模板",
        cancelButtonText: "取消",
      },
    );
    saving.value = true;
    await api.createWorkpaperTemplate(result.value.trim(), props.issue.id);
    ElMessage.success("底稿模板已保存，可在问题列表的“模板”入口按单位新建");
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(errorMessage(error));
  } finally {
    saving.value = false;
  }
}

async function openVersionHistory(): Promise<void> {
  await versionHistory.value?.open();
}

async function exportConfirmationDocx(): Promise<void> {
  try {
    const result = await api.exportIssueConfirmationDocx(props.issue.id);
    window.open(result.download_url, "_blank", "noopener,noreferrer");
    ElMessage.success("问题确认单已生成；请在真实 Word 中核对中文、表格和分页");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "生成问题确认单失败，请重试");
  }
}

async function prepareVersionRestore(): Promise<boolean> {
  return persist(false);
}

async function confirmLeave(): Promise<boolean> {
  if (props.isNew && missingRequired.value.length) {
    clearScheduledSave();
    try {
      await ElMessageBox.confirm(
        `当前新建底稿尚未填写必填项：${missingRequired.value.join("、")}。确认离开后将自动删除该未完成底稿。`,
        "未完成底稿",
        {
          type: "warning",
          confirmButtonText: "删除并离开",
          cancelButtonText: "继续编制",
          closeOnClickModal: false,
          closeOnPressEscape: false,
        },
      );
      await api.deleteIssue(props.issue.id);
      emit("discarded", props.issue.id);
      ElMessage.info("未完成底稿已自动删除");
      return true;
    } catch (reason) {
      if (reason !== "cancel" && reason !== "close") ElMessage.error(errorMessage(reason));
      if (dirty.value && !isArchived.value) scheduleAutoSave();
      return false;
    }
  }
  if (!dirty.value) return true;
  clearScheduledSave();
  try {
    await ElMessageBox.confirm(
      "当前底稿有未保存修改。可先保存后切换，或放弃本次修改。",
      "未保存的底稿内容",
      {
        type: "warning",
        confirmButtonText: "保存并切换",
        cancelButtonText: "放弃修改",
        distinguishCancelAndClose: true,
        closeOnClickModal: false,
        closeOnPressEscape: false,
      },
    );
    return persist(false);
  } catch (reason) {
    if (reason === "cancel") {
      syncDraft(props.issue);
      ElMessage.info("已放弃未保存修改");
      return true;
    }
    return false;
  }
}

async function transition(target: IssueStatus): Promise<void> {
  if (!(await persist(false))) return;
  let comment = "";
  const mustExplain = target === "复核退回" || props.issue.status === "已归档";
  if (mustExplain) {
    const label = target === "复核退回" ? "退回意见" : "修改原因";
    try {
      const result = await ElMessageBox.prompt(
        target === "复核退回" ? "退回意见将写入项目操作日志。" : "归档后编辑将生成新版本并重新进入复核流程。",
        label,
        { inputPlaceholder: `请输入${label}`, inputValidator: (value) => Boolean(value?.trim()) || `${label}不能为空` },
      );
      comment = result.value.trim();
    } catch {
      return;
    }
  }
  saving.value = true;
  try {
    const fresh = await api.transitionIssue(props.issue.id, target, comment);
    emit("updated", fresh);
    ElMessage.success(`状态已流转至“${target}”`);
  } catch (error) {
    ElMessage.error(errorMessage(error));
  } finally {
    saving.value = false;
  }
}

onBeforeUnmount(() => {
  clearScheduledSave();
  clearScheduledRecoverySave();
});
// beforeunload 不能等待确认弹窗，向工作区暴露同步脏状态，由浏览器显示原生离开提示。
function hasUnsavedChanges(): boolean {
  return dirty.value || (props.isNew && missingRequired.value.length > 0);
}

defineExpose({ confirmLeave, hasUnsavedChanges, openDuplicateDialog, openVersionHistory, saveAsTemplate });
</script>

<template>
  <article class="editor panel">
    <div class="panel-head issue-editor-head">
      <div class="issue-heading">
        <p class="eyebrow">底稿 {{ formatNo(issue.seq) }}</p>
        <div class="issue-title-line"><h2>{{ issue.defect_type || "未定性底稿" }}</h2></div>
      </div>
      <el-tag class="issue-status" :type="issue.status === '已归档' ? 'info' : issue.status === '已复核' ? 'success' : issue.status === '复核退回' ? 'warning' : 'primary'">{{ issue.status }}</el-tag>
    </div>
    <div class="form-grid">
      <label>所属版块 *<el-select v-model="draft.department" :disabled="isArchived" filterable allow-create default-first-option placeholder="选择或输入版块"><el-option v-for="department in departments" :key="department" :label="department" :value="department" /></el-select></label>
      <label>缺陷定性 *<el-input v-model="draft.defect_type" :disabled="isArchived" placeholder="例如：电费回收不及时" /></label>
      <label>问题金额
        <div class="amount-inputs">
          <el-input v-model="draft.amount" :disabled="isArchived" inputmode="decimal" placeholder="例如：120.00" />
          <el-select v-model="draft.currency" :disabled="isArchived || !amountIsStructured" aria-label="币种" :title="amountFreeTextHint"><el-option label="CNY" value="CNY" /><el-option label="USD" value="USD" /><el-option label="EUR" value="EUR" /><el-option label="HKD" value="HKD" /></el-select>
          <el-select v-model="draft.amount_unit" :disabled="isArchived || !amountIsStructured" aria-label="金额单位" :title="amountFreeTextHint"><el-option label="元" value="元" /><el-option label="万元" value="万元" /><el-option label="亿元" value="亿元" /></el-select>
          <span v-if="amountFreeTextHint" class="amount-free-text-hint">{{ amountFreeTextHint }}</span>
        </div>
      </label>
      <label>问题分类<el-select v-model="draft.category" :disabled="isArchived" filterable allow-create default-first-option clearable placeholder="选择或输入分类"><el-option v-for="category in categories" :key="category" :label="category" :value="category" /></el-select></label>
    </div>
    <label class="field">缺陷描述<el-input v-model="draft.defect_desc" :disabled="isArchived" type="textarea" :autosize="{ minRows: 6, maxRows: 14 }" placeholder="请输入缺陷描述" /></label>
    <label class="field">制度依据<el-input v-model="draft.regulation_basis" :disabled="isArchived" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" placeholder="请输入制度依据" /></label>
    <label class="field">审计建议<el-input v-model="draft.suggestion" :disabled="isArchived" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" placeholder="请输入审计建议" /></label>
    <div class="form-grid author-reviewer"><label>编制人<el-input v-model="draft.author" :disabled="isArchived" /></label><label>审核人<el-input v-model="draft.reviewer" :disabled="isArchived" /></label></div>
    <div class="editor-footer">
      <span :class="{ 'editor-dirty': dirty }">{{ saveStateText }}</span>
      <div class="editor-footer-actions"><VersionHistory ref="versionHistory" :issue="issue" :before-restore="prepareVersionRestore" :trigger-visible="false" @restored="(fresh) => emit('updated', fresh)" /><el-button size="small" @click="exportConfirmationDocx">确认单 DOCX</el-button><span class="status-actions footer-status-actions"><el-button v-for="target in allowed" :key="target" size="small" :loading="saving" :type="target === '已归档' ? 'info' : target === '已复核' ? 'success' : target === '复核退回' ? 'warning' : 'primary'" @click="transition(target)">{{ issue.status === '已归档' && target === '编制完成' ? '归档后编辑' : target }}</el-button></span><el-button size="small" type="primary" :disabled="isArchived" :loading="saving" @click="save">保存</el-button></div>
    </div>
    <ReviewNotes :issue="issue" />
    <el-dialog v-model="duplicateVisible" title="复制为新底稿" width="min(480px, calc(100vw - 32px))" append-to-body>
      <div class="duplicate-panel">
        <p>将复制当前底稿的正文和元数据为新草稿。可选择任意被审计单位；附件、版本、状态和交流记录不会复制。</p>
        <label>目标被审计单位
          <el-select v-model="duplicateTargetUnitId" filterable placeholder="请选择目标单位">
            <el-option v-for="unit in units" :key="unit.id" :label="unit.id === issue.unit_id ? `${unit.name}（当前单位）` : unit.name" :value="unit.id" />
          </el-select>
        </label>
      </div>
      <template #footer>
        <el-button :disabled="saving" @click="duplicateVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!duplicateTargetUnitId" :loading="saving" @click="duplicate">复制并打开新底稿</el-button>
      </template>
    </el-dialog>
  </article>
</template>

<style scoped>
.amount-inputs {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 88px 82px;
  gap: 6px;
}

.amount-free-text-hint {
  grid-column: 1 / -1;
  font-size: 12px;
  color: var(--el-color-warning, #e6a23c);
  line-height: 1.4;
}

.duplicate-panel {
  display: grid;
  gap: 14px;
}

.duplicate-panel p {
  margin: 0;
  color: var(--el-text-color-regular);
  line-height: 1.6;
}

.duplicate-panel label {
  display: grid;
  gap: 6px;
  color: var(--el-text-color-regular);
  font-size: 14px;
}

@media (max-width: 760px) {
  .amount-inputs { grid-template-columns: 1fr; }
}
</style>
