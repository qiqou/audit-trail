/**
 * 不驱动浏览器的前端交付契约检查。
 *
 * 用户明确要求页面交互由人工验收，因此这里不模拟页面操作；只防止“设置标签
 * 存在而没有可见输入控件”的代码回退。视觉和实际输入仍由人工清单验收。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const operation = readFileSync(resolve(root, "frontend-v3/src/components/ProjectOperations.vue"), "utf8");
const workspace = readFileSync(resolve(root, "frontend-v3/src/components/IssueWorkspace.vue"), "utf8");
const exchange = readFileSync(resolve(root, "frontend-v3/src/components/ExchangeWorkbench.vue"), "utf8");
const editor = readFileSync(resolve(root, "frontend-v3/src/components/IssueEditor.vue"), "utf8");
const styles = readFileSync(resolve(root, "frontend-v3/src/styles.css"), "utf8");
const app = readFileSync(resolve(root, "frontend-v3/src/App.vue"), "utf8");
const required = [
  'v-model="backupTargetDir" class="backup-native-input" type="text"',
  'v-model.number="backupIntervalMinutes" class="backup-native-input" type="number"',
  'v-model.number="backupRetentionDays" class="backup-native-input" type="number"',
  'v-model.number="backupMaxGiB" class="backup-native-input" type="number"',
];
for (const snippet of required) {
  if (!operation.includes(snippet)) throw new Error(`自动备份表单契约缺失：${snippet}`);
}
for (const snippet of [".backup-native-input", "display: block !important", "height: 36px", "visibility: visible !important"]) {
  if (!styles.includes(snippet)) throw new Error(`自动备份输入框可见性样式缺失：${snippet}`);
}
for (const snippet of ['data-theme="paper"', "--success: #2f6b45", "--disabled-bg: #eee2cd", "button:focus-visible"]) {
  if (!styles.includes(snippet)) throw new Error(`纸质书主题可读性样式缺失：${snippet}`);
}
for (const snippet of ['type Theme = "dark" | "light" | "green" | "paper"', 'storedTheme === "paper"', "applyTheme('paper')", 'title="纸质书"']) {
  if (!app.includes(snippet)) throw new Error(`纸质书主题切换或本机偏好契约缺失：${snippet}`);
}
for (const snippet of ["defineAsyncComponent", 'import("./components/IssueWorkspace.vue")', 'import("./components/ProjectOperations.vue")']) {
  if (!app.includes(snippet)) throw new Error(`项目列表首屏分包契约缺失：${snippet}`);
}
for (const snippet of ['ExchangeWorkbench', 'openExchange', 'defineExpose({ confirmCurrentLeave, hasUnsavedChanges, selectIssueById, selectUnit, openExchange })']) {
  if (!workspace.includes(snippet)) throw new Error(`问题交流入口契约缺失：${snippet}`);
}
for (const snippet of ['evidenceRefreshKey', '@evidence-changed="exchangeEvidenceChanged"', ':key="`${current.id}-${evidenceRefreshKey}`"']) {
  if (!workspace.includes(snippet)) throw new Error(`交流附件列表联动刷新契约缺失：${snippet}`);
}
for (const snippet of ["emit('openExchange')", '💬 交流修订']) {
  if (!operation.includes(snippet)) throw new Error(`顶部交流入口契约缺失：${snippet}`);
}
if (editor.includes("emit('exchange', issue)")) throw new Error("底稿详情仍保留重复的交流修订入口");
for (const snippet of [
  'v-model="search"', 'placeholder="编号、单位、版块、定性或描述"', '修订已保存，将在结束本轮时统一生成一个版本',
  '本轮可连续保存修订', '版本时间线', '选择版本后，右侧仅显示该版本的修订记录',
  'timelineVersions', '本轮编辑中', '确认结束并生成版本', 'focusRevision(revision)', 'revisionSummary(revision)', '缺陷定性',
  'v-model="selectedUnitFilter"', '全部单位', 'requestUploadPicker', '上传并关联', 'uploadForRequest',
  'trackedParts(field)', 'hasTrackedChanges(field)', 'activeRequests', 'pasteForRequest($event, request.request_uuid)',
  'removeRequest(request)', '历史记录已保留', 'item.session_uuid === session.value?.session_uuid',
  'await api.linkFile(session.value.issue.id, evidence.id)', 'emit("evidenceChanged")',
  '隐藏问题栏', '隐藏审阅栏', 'problem-sidebar-hidden', 'review-sidebar-hidden',
  'visibleSessionUuids', 'visibleComments', 'visibleRequests',
  '修订 {{ visibleRevisions.length }}', '批注 {{ visibleComments.length }}', '待补 {{ visibleRequests.length }}',
]) {
  if (!exchange.includes(snippet)) throw new Error(`问题交流修订或检索契约缺失：${snippet}`);
}
if (exchange.includes('修订 {{ session?.revisions.length') || exchange.includes('批注 {{ session?.comments.length') || exchange.includes('待补 {{ session?.requests.length')) {
  throw new Error("右侧审阅统计仍使用全部会话数量，未跟随版本切换");
}
if (exchange.includes('await api.linkFileExclusive(session.value.issue.id, evidence.id)')) {
  throw new Error("交流补充资料仍被设为独占附件，无法联动到本单位资料库");
}
for (const snippet of ['overflow-x: auto', 'flex: 0 0 118px', '.exchange-detail-grid { grid-template-columns: repeat(2', '.exchange-revision-meta', '.exchange-field { margin: 0 0 8px', 'background: var(--panel)', '.exchange-field-changed', '.exchange-grid.problem-sidebar-hidden', '.exchange-grid.review-sidebar-hidden']) {
  if (!styles.includes(snippet)) throw new Error(`交流模式紧凑或横向滚动样式缺失：${snippet}`);
}
