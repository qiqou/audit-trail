<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  api,
  type ExchangeRequest,
  type ExchangeRequestStatus,
  type ExchangeRevision,
  type ExchangeSession,
  type Issue,
  type IssueVersion,
} from "../api/client";
import { formatIssueNo } from "../format";

type IssueNavItem = Issue & { unit_name: string };
type SideTab = "revisions" | "comments" | "requests";

const props = defineProps<{
  issue: Issue;
  issueItems: IssueNavItem[];
  issueNumberRule: { prefix: string; suffix: string };
}>();
const emit = defineEmits<{ close: []; applied: [issue: Issue]; evidenceChanged: [] }>();

const session = ref<ExchangeSession | null>(null);
// 交流时间线只展示交流轮次固化生成的版本（round_versions），
// 与底稿编辑保存的版本历史（IssueEditor 的版本历史）相互隔离。
const roundVersions = ref<IssueVersion[]>([]);
const selectedSnapshot = ref<"current" | number>("current");
const selectedRevisionUuid = ref<string | null>(null);
const selectedIssueId = ref(props.issue.id);
const search = ref("");
const selectedUnitFilter = ref<number | "all">("all");
const sideTab = ref<SideTab>("revisions");
const loading = ref(false);
const requestUploadPicker = ref<HTMLInputElement | null>(null);
const uploadingRequestUuid = ref<string | null>(null);
const composer = ref<{ kind: "revision" | "comment" | "request"; field: string } | null>(null);
const composerValue = ref("");
const composerReason = ref("");
const requestFileIds = ref<Record<string, number | null>>({});
const requestNotes = ref<Record<string, string>>({});
const problemSidebarHidden = ref(false);
const reviewSidebarHidden = ref(false);

const fields = [
  ["category", "问题分类"], ["defect_desc", "问题描述"],
  ["regulation_basis", "制度依据"], ["suggestion", "审计建议"],
] as const;
const fieldLabel: Record<string, string> = Object.fromEntries(fields);
fieldLabel.department = "所属版块";
fieldLabel.defect_type = "缺陷定性";
fieldLabel.amount = "问题金额";
const visibleFields = computed(() => fields.filter(([field]) => {
  const base = String(activeSnapshot.value[field] ?? "");
  const current = String(session.value?.issue?.[field as keyof Issue] ?? "");
  return base || current || hasTrackedChanges(field);
}));
const unitOptions = computed(() => {
  const units = new Map<number, string>();
  for (const item of props.issueItems) units.set(item.unit_id, item.unit_name);
  return [...units.entries()].map(([id, name]) => ({ id, name }));
});
const unitFilteredItems = computed(() => selectedUnitFilter.value === "all"
  ? props.issueItems
  : props.issueItems.filter((item) => item.unit_id === selectedUnitFilter.value));
const filteredItems = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  if (!keyword) return unitFilteredItems.value;
  return unitFilteredItems.value.filter((item) => [item.unit_name, item.department, item.defect_type, item.defect_desc, item.seq]
    .some((value) => String(value ?? "").toLocaleLowerCase().includes(keyword)));
});
const currentItem = computed(() => props.issueItems.find((item) => item.id === selectedIssueId.value) ?? null);
const selectedVersion = computed(() => selectedSnapshot.value === "current" ? null
  : timelineVersions.value.find((item) => item.id === selectedSnapshot.value) ?? null);
const isCurrentSnapshot = computed(() => selectedSnapshot.value === "current");
const activeSnapshot = computed<Record<string, string | number | null>>(() => {
  if (selectedVersion.value) return selectedVersion.value.snapshot;
  return (session.value?.issue ?? session.value?.base_snapshot ?? {}) as Record<string, string | number | null>;
});
const revisionsForVersion = (version: IssueVersion) => session.value?.revisions
  .filter((item) => item.version_id === version.id) ?? [];
// 时间线 = 交流轮次版本；round_versions 已由后端按交流轮次固化过滤
const timelineVersions = computed(() => roundVersions.value);
const pendingRevisions = computed(() => session.value?.revisions.filter((item) => (
  item.version_id === null && item.session_uuid === session.value?.session_uuid
)) ?? []);
const visibleRevisions = computed(() => {
  if (selectedVersion.value) return revisionsForVersion(selectedVersion.value);
  return session.value?.status === "open" ? pendingRevisions.value : [];
});
const visibleSessionUuids = computed(() => {
  if (selectedVersion.value) {
    return new Set(revisionsForVersion(selectedVersion.value).map((item) => item.session_uuid));
  }
  if (isCurrentSnapshot.value && session.value?.status === "open") {
    return new Set([session.value.session_uuid]);
  }
  return new Set<string>();
});
const visibleComments = computed(() => session.value?.comments.filter((item) => (
  visibleSessionUuids.value.has(item.session_uuid)
)) ?? []);
const visibleRequests = computed(() => session.value?.requests.filter((item) => (
  visibleSessionUuids.value.has(item.session_uuid)
)) ?? []);
const commentsForField = (field: string) => visibleComments.value.filter((item) => item.anchor_field === field);
const activeRequests = computed(() => session.value?.requests.filter((item) => (
  item.session_uuid === session.value?.session_uuid && item.status !== "withdrawn"
)) ?? []);
const selectedRevision = computed(() => session.value?.revisions
  .find((item) => item.revision_uuid === selectedRevisionUuid.value) ?? null);
function versionLabel(version: IssueVersion): string {
  const index = timelineVersions.value.findIndex((item) => item.id === version.id);
  const isLatest = index === timelineVersions.value.length - 1 && session.value?.status !== "open";
  const name = index === 0 ? "初稿" : `第 ${index + 1} 版`;
  return isLatest ? `${name}（当前）` : name;
}

type DiffPart = { type: "same" | "deleted" | "inserted"; text: string };
function mergeDiffParts(parts: DiffPart[]): DiffPart[] {
  return parts.reduce<DiffPart[]>((merged, part) => {
    const previous = merged.at(-1);
    if (previous && previous.type === part.type) previous.text += part.text;
    else merged.push({ ...part });
    return merged;
  }, []);
}
function findAnchor(oldValue: string, newValue: string): { oldIndex: number; newIndex: number; length: number } | null {
  const width = oldValue.length > 12 && newValue.length > 12 ? 4 : 2;
  if (oldValue.length < width || newValue.length < width) return null;
  const positions = new Map<string, number>();
  for (let index = 0; index <= oldValue.length - width; index += 1) {
    const token = oldValue.slice(index, index + width);
    if (!positions.has(token)) positions.set(token, index);
  }
  let best: { oldIndex: number; newIndex: number; length: number } | null = null;
  for (let newIndex = 0; newIndex <= newValue.length - width; newIndex += 1) {
    const oldIndex = positions.get(newValue.slice(newIndex, newIndex + width));
    if (oldIndex === undefined) continue;
    let left = 0;
    let right = width;
    while (oldIndex - left > 0 && newIndex - left > 0 && oldValue[oldIndex - left - 1] === newValue[newIndex - left - 1]) left += 1;
    while (oldIndex + right < oldValue.length && newIndex + right < newValue.length
      && oldValue[oldIndex + right] === newValue[newIndex + right]) right += 1;
    const candidate = { oldIndex: oldIndex - left, newIndex: newIndex - left, length: left + right };
    if (!best || candidate.length > best.length) best = candidate;
  }
  return best;
}
function diffParts(oldValue: string, newValue: string, depth = 0): DiffPart[] {
  let start = 0;
  while (start < oldValue.length && start < newValue.length && oldValue[start] === newValue[start]) start += 1;
  let oldEnd = oldValue.length;
  let newEnd = newValue.length;
  while (oldEnd > start && newEnd > start && oldValue[oldEnd - 1] === newValue[newEnd - 1]) {
    oldEnd -= 1;
    newEnd -= 1;
  }
  const prefix = oldValue.slice(0, start);
  const oldMiddle = oldValue.slice(start, oldEnd);
  const newMiddle = newValue.slice(start, newEnd);
  const suffix = oldValue.slice(oldEnd);
  if (!oldMiddle || !newMiddle || depth >= 16) {
    return [
      { type: "same" as const, text: prefix },
      { type: "deleted" as const, text: oldMiddle },
      { type: "inserted" as const, text: newMiddle },
      { type: "same" as const, text: suffix },
    ].filter((part) => part.text);
  }
  const anchor = findAnchor(oldMiddle, newMiddle);
  if (!anchor || anchor.length < 2) {
    return [
      { type: "same" as const, text: prefix },
      { type: "deleted" as const, text: oldMiddle },
      { type: "inserted" as const, text: newMiddle },
      { type: "same" as const, text: suffix },
    ].filter((part) => part.text);
  }
  return mergeDiffParts([
    { type: "same" as const, text: oldValue.slice(0, start) },
    ...diffParts(oldMiddle.slice(0, anchor.oldIndex), newMiddle.slice(0, anchor.newIndex), depth + 1),
    { type: "same" as const, text: oldMiddle.slice(anchor.oldIndex, anchor.oldIndex + anchor.length) },
    ...diffParts(oldMiddle.slice(anchor.oldIndex + anchor.length), newMiddle.slice(anchor.newIndex + anchor.length), depth + 1),
    { type: "same" as const, text: suffix },
  ].filter((part) => part.text));
}
function revisionsForField(field: string): ExchangeRevision[] {
  return visibleRevisions.value.filter((revision) => revision.field_name === field);
}
function hasTrackedChanges(field: string): boolean {
  return revisionsForField(field).length > 0;
}
function trackedParts(field: string): DiffPart[] {
  const revisions = revisionsForField(field);
  if (!revisions.length) {
    return [{ type: "same", text: String(activeSnapshot.value[field] ?? "—") }];
  }
  // 同一轮内同一字段可能连续修改多次：正文从本轮第一次修改前的值比较到
  // 最后一次修改后的值，从而同时呈现本轮在所有字段上的累计修订痕迹。
  return diffParts(revisions[0].old_value || "", revisions.at(-1)?.new_value || "");
}
function revisionSummary(revision: ExchangeRevision): DiffPart[] {
  return diffParts(revision.old_value || "", revision.new_value || "").filter((part) => part.type !== "same");
}
function changeExcerpt(text: string): string {
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length > 120 ? `${compact.slice(0, 120)}…` : compact;
}

function report(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : "交流操作失败，请重试");
}

function requestStatusLabel(status: ExchangeRequestStatus): string {
  return ({ open: "待提供", provided: "已提供", verified: "已核验", withdrawn: "已撤回" })[status];
}

async function load(issueId = selectedIssueId.value): Promise<void> {
  loading.value = true;
  try {
    const opened = await api.startExchange(issueId);
    session.value = opened;
    roundVersions.value = opened.round_versions ?? [];
    selectedIssueId.value = issueId;
    selectedSnapshot.value = "current";
    selectedRevisionUuid.value = null;
    composer.value = null;
  } catch (error) {
    report(error);
  } finally {
    loading.value = false;
  }
}

async function chooseIssue(item: IssueNavItem): Promise<void> {
  if (item.id === selectedIssueId.value) return;
  await load(item.id);
}

function selectVersion(version: IssueVersion): void {
  selectedSnapshot.value = version.id;
  selectedRevisionUuid.value = null;
}

async function focusRevision(revision: ExchangeRevision): Promise<void> {
  const version = timelineVersions.value.find((item) => revisionsForVersion(item)
    .some((candidate) => candidate.revision_uuid === revision.revision_uuid));
  if (version) selectedSnapshot.value = version.id;
  selectedRevisionUuid.value = revision.revision_uuid;
  await nextTick();
  document.getElementById(`exchange-field-${revision.field_name}`)
    ?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function openComposer(kind: "revision" | "comment" | "request", field = ""): void {
  composer.value = { kind, field };
  composerReason.value = "";
  composerValue.value = kind === "revision" ? String(session.value?.issue?.[field as keyof Issue] ?? "") : "";
}

async function submitComposer(): Promise<void> {
  if (!session.value || !composer.value || !composerValue.value.trim()) {
    ElMessage.warning(composer.value?.kind === "request" ? "请输入待补资料要求" : "请输入内容");
    return;
  }
  try {
    if (composer.value.kind === "revision") {
      session.value = await api.proposeExchangeRevision(
        session.value.session_uuid, composer.value.field, composerValue.value, composerReason.value,
      );
      selectedSnapshot.value = "current";
      selectedRevisionUuid.value = [...session.value.revisions].reverse()
        .find((revision) => revision.session_uuid === session.value?.session_uuid)?.revision_uuid ?? null;
      emit("applied", session.value.issue!);
      sideTab.value = "revisions";
      ElMessage.success("修订已保存，将在结束本轮时统一生成一个版本");
    } else if (composer.value.kind === "comment") {
      session.value = await api.addExchangeComment(session.value.session_uuid, composerValue.value, composer.value.field);
      sideTab.value = "comments";
    } else {
      session.value = await api.createExchangeRequest(session.value.session_uuid, composerValue.value);
      sideTab.value = "requests";
    }
    composer.value = null;
    composerValue.value = "";
    composerReason.value = "";
  } catch (error) {
    report(error);
  }
}

async function updateRequest(requestUuid: string, status: ExchangeRequestStatus): Promise<void> {
  if (!session.value) return;
  try {
    session.value = await api.updateExchangeRequest(
      session.value.session_uuid, requestUuid, status,
      requestFileIds.value[requestUuid] ?? null, requestNotes.value[requestUuid] ?? "",
    );
  } catch (error) {
    report(error);
  }
}

function chooseRequestUpload(requestUuid: string): void {
  uploadingRequestUuid.value = requestUuid;
  requestUploadPicker.value?.click();
}

function clipboardName(file: File): File {
  const genericNames = new Set(["image.png", "image.jpg", "image.jpeg", "image.gif", "image.webp", "image.bmp", "image.svg"]);
  const needsName = !file.name || !file.name.includes(".") || (file.type.startsWith("image/") && genericNames.has(file.name.toLowerCase()));
  if (!needsName) return file;
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}_${String(now.getMilliseconds()).padStart(3, "0")}`;
  const ext = file.type.split("/")[1] || "png";
  return new File([file], `截图_${stamp}.${ext}`, { type: file.type || "image/png" });
}

function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement
    || (target instanceof HTMLElement && target.isContentEditable);
}

async function uploadRequestFile(requestUuid: string, file: File): Promise<void> {
  if (!session.value?.issue) return;
  uploadingRequestUuid.value = requestUuid;
  try {
    const response = await api.uploadFile(session.value.issue.unit_id, file);
    const evidence = "duplicated" in response ? response.file : response;
    // 交流中取得的补充资料既关联当前底稿，也保留在本单位资料库供后续复用。
    await api.linkFile(session.value.issue.id, evidence.id);
    requestFileIds.value[requestUuid] = evidence.id;
    session.value = await api.updateExchangeRequest(
      session.value.session_uuid, requestUuid, "provided", evidence.id, requestNotes.value[requestUuid] ?? "",
    );
    emit("evidenceChanged");
    ElMessage.success("资料已上传、关联当前底稿并标记为已提供");
  } catch (error) {
    report(error);
  } finally {
    uploadingRequestUuid.value = null;
  }
}

async function uploadForRequest(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  const requestUuid = uploadingRequestUuid.value;
  if (!file || !requestUuid) return;
  await uploadRequestFile(requestUuid, file);
}

async function pasteForRequest(event: ClipboardEvent, requestUuid: string): Promise<void> {
  if (isEditableTarget(event.target)) return;
  const files = Array.from(event.clipboardData?.items ?? [])
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
  if (!files.length) return;
  event.preventDefault();
  if (files.length > 1) ElMessage.warning("一条待补资料只能关联一个文件，已处理剪贴板中的第一个文件");
  await uploadRequestFile(requestUuid, clipboardName(files[0]));
}

async function removeRequest(request: ExchangeRequest): Promise<void> {
  if (!session.value || session.value.status !== "open") return;
  try {
    await ElMessageBox.confirm(
      "将从当前待补清单移除该项；为保留交流留痕，右侧审阅记录仍会显示为“已撤回”。",
      "删除待补资料",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    session.value = await api.updateExchangeRequest(
      session.value.session_uuid, request.request_uuid, "withdrawn",
      request.provided_file_id, request.note,
    );
    ElMessage.success("已从待补清单移除，历史记录已保留");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

async function openEvidence(fileId: number): Promise<void> {
  try {
    await api.openFile(fileId);
  } catch (error) {
    report(error);
  }
}

async function finish(): Promise<void> {
  emit("close");
}

async function closeRound(): Promise<void> {
  if (session.value?.status === "open") {
    const revisionCount = pendingRevisions.value.length;
    try {
      await ElMessageBox.confirm(`将本轮 ${revisionCount} 条修订一次性固化为一个底稿版本；结束后不可再新增修订或批注。`, "结束本轮", {
        type: "warning", confirmButtonText: "确认结束并生成版本", cancelButtonText: "继续交流",
      });
      session.value = await api.closeExchange(session.value.session_uuid);
      roundVersions.value = session.value.round_versions ?? [];
      selectedSnapshot.value = timelineVersions.value.at(-1)?.id ?? "current";
      selectedRevisionUuid.value = null;
      emit("applied", session.value.issue!);
      ElMessage.success(revisionCount ? "本轮已固化为一个底稿版本" : "本轮已结束");
    } catch (error) {
      // 审查 F1 修复：用户点“继续交流”时 ElMessageBox reject 值为 "cancel"/"close"，
      // 属于预期取消；其余（网络错误/后端 400/500）必须提示，不能静默吞掉——
      // 否则接口失败时用户误以为本轮已固化，实际会话仍 open。
      if (error !== "cancel" && error !== "close") report(error);
    }
  }
}

onMounted(() => { void load(); });
</script>

<template>
  <section class="exchange-workbench" aria-label="问题交流模式">
    <header class="exchange-topbar">
      <div><p class="eyebrow">ISSUE EXCHANGE · 持续交流记录</p><h2>{{ session?.issue?.defect_type || '问题交流' }}</h2><small>本轮可连续保存修订；仅在结束本轮确认后，统一生成一个底稿版本。</small></div>
      <div class="exchange-top-actions"><el-button size="small" @click="problemSidebarHidden = !problemSidebarHidden">{{ problemSidebarHidden ? '显示问题栏' : '隐藏问题栏' }}</el-button><el-button size="small" @click="reviewSidebarHidden = !reviewSidebarHidden">{{ reviewSidebarHidden ? '显示审阅栏' : '隐藏审阅栏' }}</el-button><el-tag :type="session?.status === 'open' ? 'warning' : 'info'">{{ session?.status === 'open' ? '交流进行中' : '本轮已结束' }}</el-tag><el-button v-if="session?.status === 'open'" type="warning" plain @click="closeRound">结束本轮</el-button><el-button @click="finish">返回底稿</el-button></div>
    </header>

    <main class="exchange-grid" :class="{ 'problem-sidebar-hidden': problemSidebarHidden, 'review-sidebar-hidden': reviewSidebarHidden }" v-loading="loading">
      <aside v-if="!problemSidebarHidden" class="exchange-nav">
        <label class="exchange-search">筛选单位<el-select v-model="selectedUnitFilter" placeholder="选择单位"><el-option label="全部单位" value="all" /><el-option v-for="unit in unitOptions" :key="unit.id" :label="unit.name" :value="unit.id" /></el-select></label>
        <label class="exchange-search">检索问题<el-input v-model="search" clearable placeholder="编号、单位、版块、定性或描述" /></label>
        <p class="exchange-nav-summary">{{ filteredItems.length }} / {{ unitFilteredItems.length }} 条问题</p>
        <div class="exchange-issue-list"><button v-for="item in filteredItems" :key="item.id" class="exchange-issue-item" :class="{ active: item.id === selectedIssueId }" @click="chooseIssue(item)"><span>{{ formatIssueNo(item.seq, issueNumberRule) }}</span><strong>{{ item.defect_type || '未定性' }}</strong><small>{{ item.unit_name }} · {{ item.department || '未分版块' }}</small></button><p v-if="!filteredItems.length" class="exchange-empty">未找到匹配问题</p></div>
      </aside>

      <article class="exchange-document">
        <div class="exchange-document-head"><div><strong>问题 {{ session?.issue ? formatIssueNo(session.issue.seq, issueNumberRule) : '—' }}</strong><small>{{ currentItem?.unit_name || '—' }} · 本轮保存 {{ pendingRevisions.length }} 条修订，结束本轮后统一固化</small></div><div class="exchange-view-tabs"><span>当前范围修订 {{ visibleRevisions.length }} 条</span></div></div>
        <section class="exchange-timeline" aria-label="底稿版本时间线"><div class="exchange-timeline-title"><strong>版本时间线</strong><small>选择版本后，右侧仅显示该版本的修订记录</small></div><div v-if="timelineVersions.length || session?.status === 'open'" class="exchange-timeline-track"><button v-for="version in timelineVersions" :key="version.id" class="exchange-timeline-step" :class="{ active: selectedSnapshot === version.id }" @click="selectVersion(version)"><span class="exchange-timeline-dot"></span><strong>{{ versionLabel(version) }}</strong><small>{{ version.created_at }}</small><em v-if="revisionsForVersion(version).length">{{ revisionsForVersion(version).length }} 项修订</em></button><button v-if="session?.status === 'open'" class="exchange-timeline-step" :class="{ active: isCurrentSnapshot }" @click="selectedSnapshot = 'current'; selectedRevisionUuid = null"><span class="exchange-timeline-dot"></span><strong>本轮编辑中</strong><small>{{ session?.opened_at }}</small><em>{{ pendingRevisions.length }} 条待固化</em></button></div><p v-else class="exchange-timeline-note">当前底稿尚无业务内容，暂不生成空白初稿。</p><p v-if="selectedVersion" class="exchange-timeline-note">正在查看{{ versionLabel(selectedVersion) }}；点击右侧修订记录可在正文定位并高亮具体改动。</p><p v-else-if="session?.status === 'open'" class="exchange-timeline-note">正在查看本轮编辑内容；右侧显示本轮待固化的修订记录。</p></section>
        <section class="exchange-primary-grid"><div :class="{ 'exchange-field-changed': hasTrackedChanges('department'), 'exchange-change-highlight': selectedRevision?.field_name === 'department' }"><span>所属版块</span><strong class="exchange-word-diff"><template v-for="(part, index) in trackedParts('department')" :key="`department-${part.type}-${index}`"><del v-if="part.type === 'deleted'">{{ part.text }}</del><ins v-else-if="part.type === 'inserted'">{{ part.text }}</ins><template v-else>{{ part.text }}</template></template></strong></div><div :class="{ 'exchange-field-changed': hasTrackedChanges('defect_type'), 'exchange-change-highlight': selectedRevision?.field_name === 'defect_type' }"><span>缺陷定性</span><strong class="exchange-word-diff"><template v-for="(part, index) in trackedParts('defect_type')" :key="`defect-type-${part.type}-${index}`"><del v-if="part.type === 'deleted'">{{ part.text }}</del><ins v-else-if="part.type === 'inserted'">{{ part.text }}</ins><template v-else>{{ part.text }}</template></template></strong></div></section>
        <section class="exchange-detail-grid"><div :class="{ 'exchange-field-changed': hasTrackedChanges('category'), 'exchange-change-highlight': selectedRevision?.field_name === 'category' }"><span>问题分类</span><strong class="exchange-word-diff"><template v-for="(part, index) in trackedParts('category')" :key="`category-${part.type}-${index}`"><del v-if="part.type === 'deleted'">{{ part.text }}</del><ins v-else-if="part.type === 'inserted'">{{ part.text }}</ins><template v-else>{{ part.text }}</template></template></strong></div><div :class="{ 'exchange-field-changed': hasTrackedChanges('amount'), 'exchange-change-highlight': selectedRevision?.field_name === 'amount' }"><span>问题金额</span><strong class="exchange-word-diff"><template v-for="(part, index) in trackedParts('amount')" :key="`amount-${part.type}-${index}`"><del v-if="part.type === 'deleted'">{{ part.text }}</del><ins v-else-if="part.type === 'inserted'">{{ part.text }}</ins><template v-else>{{ part.text }}</template></template> {{ activeSnapshot.currency || '' }} {{ activeSnapshot.amount_unit || '' }}</strong></div></section>
        <section v-for="[field, label] in visibleFields" :id="`exchange-field-${field}`" :key="field" class="exchange-field" :class="{ long: ['defect_desc', 'regulation_basis', 'suggestion'].includes(field), 'exchange-field-changed': hasTrackedChanges(field), 'exchange-change-highlight': selectedRevision?.field_name === field }"><header><h3>{{ label }}</h3><span v-if="session?.status === 'open' && isCurrentSnapshot" class="exchange-inline-actions"><button @click="openComposer('revision', field)">修订</button><button @click="openComposer('comment', field)">批注</button></span></header><template v-if="composer?.field === field && composer.kind === 'revision'"><el-input v-model="composerValue" type="textarea" :autosize="{ minRows: 4, maxRows: 30 }" class="exchange-inline-edit" /><el-input v-model="composerReason" placeholder="修改理由（可选）" size="small" class="exchange-inline-edit-reason" /><div class="exchange-inline-edit-actions"><el-button size="small" @click="composer = null">取消</el-button><el-button size="small" type="primary" @click="submitComposer">保存修订</el-button></div></template><p v-else class="exchange-word-diff"><template v-for="(part, index) in trackedParts(field)" :key="`${field}-${part.type}-${index}`"><del v-if="part.type === 'deleted'">{{ part.text }}</del><ins v-else-if="part.type === 'inserted'">{{ part.text }}</ins><template v-else>{{ part.text }}</template></template></p><div v-for="comment in commentsForField(field)" :key="comment.comment_uuid" class="exchange-inline-comment"><strong>批注 · {{ comment.created_by }}</strong><span>{{ comment.body }}</span><small>{{ comment.created_at }}</small></div><div v-if="composer?.field === field && composer.kind === 'comment'" class="exchange-inline-composer"><strong>批注「{{ label }}」</strong><el-input v-model="composerValue" type="textarea" :rows="2" placeholder="请输入批注" /><div><el-button size="small" @click="composer = null">取消</el-button><el-button size="small" type="primary" @click="submitComposer">保存批注</el-button></div></div></section>
        <section class="exchange-evidence">
          <header><h3>关联附件与待补资料</h3><button v-if="session?.status === 'open'" @click="openComposer('request')">＋ 待补资料</button></header>
          <input ref="requestUploadPicker" class="hidden-input" type="file" @change="uploadForRequest" />
          <div v-if="session?.files.length" class="exchange-file-list"><button v-for="file in session.files" :key="file.id" @dblclick="openEvidence(file.id)"><span>📎</span>{{ file.orig_name }}<small>双击用本机程序打开</small></button></div>
          <p v-else>当前底稿没有关联附件。</p>
          <div v-for="request in activeRequests" :key="request.request_uuid" class="exchange-inline-request" tabindex="0" @paste="pasteForRequest($event, request.request_uuid)">
            <div class="exchange-request-head"><strong>{{ requestStatusLabel(request.status) }} · {{ request.content }}</strong><el-button v-if="session?.status === 'open'" text type="danger" size="small" @click="removeRequest(request)">删除</el-button></div>
            <small>{{ request.created_by }} · {{ request.created_at }}</small>
            <template v-if="session?.status === 'open' && request.status !== 'verified'">
              <div class="exchange-request-file-row"><el-select v-model="requestFileIds[request.request_uuid]" clearable placeholder="关联已上传附件"><el-option v-for="file in session.files" :key="file.id" :label="file.orig_name" :value="file.id" /></el-select><el-button size="small" :loading="uploadingRequestUuid === request.request_uuid" @click="chooseRequestUpload(request.request_uuid)">上传并关联</el-button></div>
              <p class="exchange-paste-hint">点击本条空白处后，可直接粘贴截图或文件</p>
              <el-input v-model="requestNotes[request.request_uuid]" placeholder="说明（可选）" />
              <div><el-button size="small" @click="updateRequest(request.request_uuid, 'provided')">标记已提供</el-button><el-button size="small" type="success" @click="updateRequest(request.request_uuid, 'verified')">核验通过</el-button></div>
            </template>
            <p v-else-if="request.provided_file_name">关联资料：{{ request.provided_file_name }}</p>
          </div>
          <div v-if="composer?.kind === 'request'" class="exchange-inline-composer"><strong>新增待补资料</strong><el-input v-model="composerValue" type="textarea" :rows="2" placeholder="例如：请补充审批单及授权依据" /><div><el-button size="small" @click="composer = null">取消</el-button><el-button size="small" type="primary" @click="submitComposer">保存待补资料</el-button></div></div>
        </section>
      </article>

      <aside v-if="!reviewSidebarHidden" class="exchange-review">
        <div class="exchange-tabs"><button :class="{ active: sideTab === 'revisions' }" @click="sideTab = 'revisions'">修订 {{ visibleRevisions.length }}</button><button :class="{ active: sideTab === 'comments' }" @click="sideTab = 'comments'">批注 {{ visibleComments.length }}</button><button :class="{ active: sideTab === 'requests' }" @click="sideTab = 'requests'">待补 {{ visibleRequests.length }}</button></div>
        <div v-if="sideTab === 'revisions'" class="exchange-side-list"><p class="exchange-review-version">{{ selectedVersion ? `${versionLabel(selectedVersion)} · ${visibleRevisions.length} 条修订` : session?.status === 'open' ? `本轮编辑中 · ${visibleRevisions.length} 条待固化修订` : '请选择时间线中的版本' }}</p><button v-for="revision in visibleRevisions" :key="revision.revision_uuid" class="exchange-revision-row" :class="{ selected: selectedRevisionUuid === revision.revision_uuid }" @click="focusRevision(revision)"><span class="exchange-revision-meta"><strong>{{ fieldLabel[revision.field_name] || revision.field_name }}</strong><small>{{ revision.proposed_by }} · {{ revision.proposed_at }}</small></span><p class="exchange-revision-summary"><span v-for="(part, index) in revisionSummary(revision)" :key="`${revision.revision_uuid}-${part.type}-${index}`" :class="part.type">{{ part.type === 'deleted' ? '删除：' : '插入：' }}{{ changeExcerpt(part.text) }}</span></p></button><p v-if="(selectedVersion || session?.status === 'open') && !visibleRevisions.length" class="exchange-empty">该范围没有交流修订记录</p></div>
        <div v-else-if="sideTab === 'comments'" class="exchange-side-list"><article v-for="comment in visibleComments" :key="comment.comment_uuid" class="exchange-comment-row"><strong>{{ fieldLabel[comment.anchor_field] || '整体批注' }}</strong><p>{{ comment.body }}</p><small>{{ comment.created_by }} · {{ comment.created_at }}</small></article><p v-if="!visibleComments.length" class="exchange-empty">该版本暂无批注</p></div>
        <div v-else class="exchange-side-list"><article v-for="request in visibleRequests" :key="request.request_uuid" class="exchange-request-row"><strong>{{ request.content }}</strong><small>{{ requestStatusLabel(request.status) }} · {{ request.created_by }} · {{ request.updated_at || request.created_at }}</small><p v-if="request.provided_file_name">关联资料：{{ request.provided_file_name }}</p><p v-if="request.note">{{ request.note }}</p></article><p v-if="!visibleRequests.length" class="exchange-empty">该版本暂无待补资料</p></div>
      </aside>
    </main>
  </section>
</template>
