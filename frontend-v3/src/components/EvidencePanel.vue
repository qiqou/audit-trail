<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  api,
  type EvidenceFile,
  type FileReference,
  type FolderUploadItem,
  type Issue,
  type Unit,
} from "../api/client";

type WebkitEntry = {
  name: string;
  isFile: boolean;
  isDirectory: boolean;
  file?: (success: (file: File) => void, error?: () => void) => void;
  createReader?: () => { readEntries: (success: (entries: WebkitEntry[]) => void, error?: () => void) => void };
};

const props = defineProps<{ issue: Issue; units: Unit[] }>();
const emit = defineEmits<{ changed: [] }>();
const attached = ref<EvidenceFile[]>([]);
const library = ref<EvidenceFile[]>([]);
const uploading = ref(false);
const picker = ref<HTMLInputElement | null>(null);
const folderPicker = ref<HTMLInputElement | null>(null);
const selectedIds = ref<number[]>([]);
const dragDepth = ref(0);
const movingFiles = ref<EvidenceFile[]>([]);
const moveTarget = ref<number | null>(null);
const referenceFile = ref<EvidenceFile | null>(null);
const references = ref<FileReference[]>([]);
const batchRenameItems = ref<Array<{ id: number; name: string }>>([]);
const libraryExpanded = ref(true);

const available = computed(() => library.value.filter((file) => !attached.value.some((item) => item.id === file.id)));
const allVisibleFiles = computed(() => [...attached.value, ...available.value]);
const selectedFiles = computed(() => allVisibleFiles.value.filter((file) => selectedIds.value.includes(file.id)));
const selectedAttached = computed(() => attached.value.filter((file) => selectedIds.value.includes(file.id)));
const selectedAvailable = computed(() => available.value.filter((file) => selectedIds.value.includes(file.id)));
const dragging = computed(() => dragDepth.value > 0);

function report(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : "附件操作失败，请重试");
}

function isSelected(file: EvidenceFile): boolean {
  return selectedIds.value.includes(file.id);
}

function setSelected(file: EvidenceFile, selected: boolean): void {
  selectedIds.value = selected
    ? [...new Set([...selectedIds.value, file.id])]
    : selectedIds.value.filter((id) => id !== file.id);
}

function clearSelection(): void {
  selectedIds.value = [];
}

function selectAll(files: EvidenceFile[]): void {
  selectedIds.value = [...new Set([...selectedIds.value, ...files.map((file) => file.id)])];
}

function invertSelection(files: EvidenceFile[]): void {
  const groupIds = new Set(files.map((file) => file.id));
  const selected = new Set(selectedIds.value);
  for (const fileId of groupIds) {
    if (selected.has(fileId)) selected.delete(fileId);
    else selected.add(fileId);
  }
  selectedIds.value = [...selected];
}

async function load(): Promise<void> {
  try {
    [attached.value, library.value] = await Promise.all([api.issueFiles(props.issue.id), api.libraryFiles(props.issue.unit_id)]);
    const visible = new Set([...attached.value, ...library.value].map((file) => file.id));
    selectedIds.value = selectedIds.value.filter((id) => visible.has(id));
  } catch (error) {
    report(error);
  }
}

watch(() => props.issue.id, load, { immediate: true });

async function uploadFiles(files: File[]): Promise<void> {
  if (!files.length) return;
  uploading.value = true;
  let complete = 0;
  try {
    for (const file of files) {
      const response = await api.uploadFile(props.issue.unit_id, file);
      const evidence = "duplicated" in response ? response.file : response;
      await api.linkFile(props.issue.id, evidence.id);
      complete += 1;
    }
    ElMessage.success(`已导入并关联 ${complete} 个附件`);
  } catch (error) {
    report(error);
  } finally {
    uploading.value = false;
    await load();
    if (complete) emit("changed");
  }
}

async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  await uploadFiles(files);
}

async function uploadFolderItems(folderName: string, files: Array<File | FolderUploadItem>): Promise<void> {
  if (!files.length) return;
  uploading.value = true;
  let changed = false;
  try {
    const folder = await api.uploadFolder(props.issue.unit_id, folderName, files);
    await api.linkFile(props.issue.id, folder.id);
    changed = true;
    ElMessage.success(`文件夹“${folderName}”已作为证据实体导入并关联`);
  } catch (error) {
    report(error);
  } finally {
    uploading.value = false;
    await load();
    if (changed) emit("changed");
  }
}

async function uploadFolder(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (!files.length) return;
  const relative = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath || "";
  const folderName = relative.split("/").filter(Boolean)[0] || "导入文件夹";
  await uploadFolderItems(folderName, files);
}

function webkitEntry(item: DataTransferItem): WebkitEntry | null {
  return (item as DataTransferItem & { webkitGetAsEntry?: () => WebkitEntry | null }).webkitGetAsEntry?.() ?? null;
}

function entryFile(entry: WebkitEntry): Promise<File> {
  return new Promise((resolve, reject) => {
    if (!entry.file) {
      reject(new Error(`无法读取“${entry.name}”`));
      return;
    }
    entry.file(resolve, () => reject(new Error(`无法读取“${entry.name}”`)));
  });
}

async function readerEntries(entry: WebkitEntry): Promise<WebkitEntry[]> {
  const reader = entry.createReader?.();
  if (!reader) return [];
  const result: WebkitEntry[] = [];
  while (true) {
    const batch = await new Promise<WebkitEntry[]>((resolve, reject) => reader.readEntries(resolve, () => reject(new Error(`无法读取目录“${entry.name}”`))));
    if (!batch.length) return result;
    result.push(...batch);
  }
}

async function collectEntry(entry: WebkitEntry, base: string, result: FolderUploadItem[]): Promise<void> {
  if (entry.isFile) {
    result.push({ file: await entryFile(entry), relativePath: `${base}${entry.name}` });
    return;
  }
  if (entry.isDirectory) {
    for (const child of await readerEntries(entry)) await collectEntry(child, `${base}${entry.name}/`, result);
  }
}

async function onDrop(event: DragEvent): Promise<void> {
  dragDepth.value = 0;
  const items = Array.from(event.dataTransfer?.items ?? []);
  const entries = items.map(webkitEntry).filter((entry): entry is WebkitEntry => Boolean(entry));
  const directories = entries.filter((entry) => entry.isDirectory);
  if (!directories.length) {
    await uploadFiles(Array.from(event.dataTransfer?.files ?? []));
    return;
  }
  const collected: FolderUploadItem[] = [];
  try {
    for (const entry of entries) await collectEntry(entry, "", collected);
  } catch (error) {
    report(error);
    return;
  }
  if (!collected.length) {
    ElMessage.warning("拖入的文件夹没有可上传的文件");
    return;
  }
  const folderName = directories.length === 1 && entries.length === 1 ? directories[0].name : "拖入文件夹";
  await uploadFolderItems(folderName, collected);
}

function onDragEnter(): void {
  dragDepth.value += 1;
}

function onDragLeave(): void {
  dragDepth.value = Math.max(0, dragDepth.value - 1);
}

function clipboardName(file: File, index: number): File {
  const genericImageNames = new Set(["image.png", "image.jpg", "image.jpeg", "image.gif", "image.webp", "image.bmp", "image.svg"]);
  const isClipboardImage = !file.name || !file.name.includes(".") || (file.type.startsWith("image/") && genericImageNames.has(file.name.toLowerCase()));
  if (!isClipboardImage) return file;
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}_${String(now.getMilliseconds()).padStart(3, "0")}`;
  const ext = file.type.split("/")[1] || "png";
  return new File([file], `截图_${stamp}${index ? `_${index + 1}` : ""}.${ext}`, { type: file.type || "image/png" });
}

function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || (target instanceof HTMLElement && target.isContentEditable);
}

async function onPaste(event: ClipboardEvent): Promise<void> {
  if (isEditableTarget(event.target)) return;
  const items = Array.from(event.clipboardData?.items ?? []);
  const entries = items.map(webkitEntry).filter((entry): entry is WebkitEntry => Boolean(entry));
  const directories = entries.filter((entry) => entry.isDirectory);
  const files = items.filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter((file): file is File => Boolean(file));
  if (!files.length && !directories.length) return;
  event.preventDefault();
  if (directories.length) {
    const collected: FolderUploadItem[] = [];
    try {
      for (const entry of entries) await collectEntry(entry, "", collected);
    } catch (error) {
      report(error);
      return;
    }
    if (collected.length) await uploadFolderItems(directories.length === 1 ? directories[0].name : "粘贴文件夹", collected);
    else ElMessage.warning("粘贴的文件夹没有可上传的文件");
    return;
  }
  await uploadFiles(files.map(clipboardName));
}

async function link(file: EvidenceFile, exclusive = false): Promise<void> {
  try {
    if (exclusive) await api.linkFileExclusive(props.issue.id, file.id);
    else await api.linkFile(props.issue.id, file.id);
    await load();
    emit("changed");
    ElMessage.success(exclusive ? "已仅关联到当前底稿" : "已关联附件");
  } catch (error) {
    report(error);
  }
}

async function unlink(file: EvidenceFile): Promise<void> {
  try {
    await api.unlinkFile(props.issue.id, file.id);
    await load();
    emit("changed");
    ElMessage.success("已取消关联，文件仍保留在资料库");
  } catch (error) {
    report(error);
  }
}

async function rename(file: EvidenceFile): Promise<void> {
  try {
    const result = await ElMessageBox.prompt("仅修改展示名称，不改变证据文件内容与哈希。", "重命名附件", {
      inputValue: file.orig_name, inputValidator: (value) => Boolean(value?.trim()) || "文件名不能为空",
      confirmButtonText: "保存", cancelButtonText: "取消",
    });
    await api.renameFile(file.id, result.value.trim());
    await load();
    ElMessage.success("附件已重命名");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

async function toggleShared(file: EvidenceFile): Promise<void> {
  try {
    if (file.exclusive_to) {
      await api.makeFileShared(file.id);
      ElMessage.success("附件已恢复为共享资料");
    } else {
      await api.linkFileExclusive(props.issue.id, file.id);
      ElMessage.success("附件已仅关联当前底稿");
    }
    await load();
  } catch (error) {
    report(error);
  }
}

function startMove(files: EvidenceFile[]): void {
  if (!files.length) {
    ElMessage.warning("请先勾选附件");
    return;
  }
  movingFiles.value = files;
  moveTarget.value = files[0].unit_id;
}

async function move(): Promise<void> {
  if (!movingFiles.value.length || !moveTarget.value) return;
  let moved = 0;
  const errors: string[] = [];
  for (const file of movingFiles.value) {
    if (file.unit_id === moveTarget.value) continue;
    try {
      await api.moveFile(file.id, moveTarget.value);
      moved += 1;
    } catch (error) {
      errors.push(`${file.orig_name}：${error instanceof Error ? error.message : "移动失败"}`);
    }
  }
  movingFiles.value = [];
  clearSelection();
  await load();
  if (moved) ElMessage.success(`已移动 ${moved} 个附件；既有底稿关联保持不变`);
  if (errors.length) ElMessage.warning(`有 ${errors.length} 个附件未移动：${errors[0]}`);
}

async function showReferences(file: EvidenceFile): Promise<void> {
  referenceFile.value = file;
  references.value = [];
  try {
    references.value = await api.fileReferences(file.id);
  } catch (error) {
    report(error);
  }
}

async function remove(file: EvidenceFile): Promise<void> {
  try {
    await ElMessageBox.confirm("将永久删除物理证据文件。仍被任何底稿引用时，系统会阻止删除。", `删除“${file.orig_name}”`, {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "取消",
    });
    await api.deleteFile(file.id);
    await load();
    ElMessage.success("附件已删除");
  } catch (error) {
    if (error !== "cancel" && error !== "close") report(error);
  }
}

async function download(file: EvidenceFile): Promise<void> {
  if (file.mime === "folder") {
    try {
      await api.openEvidenceFolder(file.id);
      ElMessage.success("已在系统文件管理器中打开证据目录");
    } catch (error) {
      report(error);
    }
    return;
  }
  try {
    await api.downloadFile(file.id, file.orig_name);
  } catch (error) {
    report(error);
  }
}

async function openAttachmentDirectory(unitId = props.issue.unit_id): Promise<void> {
  try {
    await api.openUnitAttachmentDirectory(unitId);
    ElMessage.success("已在系统文件管理器中打开附件目录");
  } catch (error) {
    report(error);
  }
}

async function batchLink(): Promise<void> {
  if (!selectedAvailable.value.length) {
    ElMessage.warning("请勾选资料库中的附件");
    return;
  }
  let linked = 0;
  for (const file of selectedAvailable.value) {
    try { await api.linkFile(props.issue.id, file.id); linked += 1; } catch (error) { report(error); break; }
  }
  clearSelection();
  await load();
  if (linked) emit("changed");
  if (linked) ElMessage.success(`已关联 ${linked} 个附件`);
}

async function batchUnlink(): Promise<void> {
  if (!selectedAttached.value.length) {
    ElMessage.warning("请勾选当前底稿已关联的附件");
    return;
  }
  try {
    await ElMessageBox.confirm(`解除 ${selectedAttached.value.length} 个附件与当前底稿的关联？文件会保留在资料库中。`, "批量解除关联", {
      type: "warning", confirmButtonText: "解除关联", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  let unlinked = 0;
  for (const file of selectedAttached.value) {
    try { await api.unlinkFile(props.issue.id, file.id); unlinked += 1; } catch (error) { report(error); break; }
  }
  clearSelection();
  await load();
  if (unlinked) emit("changed");
  if (unlinked) ElMessage.success(`已解除 ${unlinked} 个附件关联`);
}

function openBatchRename(): void {
  if (!selectedFiles.value.length) {
    ElMessage.warning("请先勾选附件");
    return;
  }
  batchRenameItems.value = selectedFiles.value.map((file) => ({ id: file.id, name: file.orig_name }));
}

async function saveBatchRename(): Promise<void> {
  const items = batchRenameItems.value.map((item) => ({ id: item.id, name: item.name.trim() })).filter((item) => item.name);
  if (!items.length) {
    ElMessage.warning("至少保留一个有效文件名");
    return;
  }
  try {
    const result = await api.batchRenameFiles(items);
    batchRenameItems.value = [];
    clearSelection();
    await load();
    ElMessage.success(`已重命名 ${result.renamed} 个附件${result.conflicts.length ? `，${result.conflicts.length} 个因冲突跳过` : ""}`);
  } catch (error) {
    report(error);
  }
}

async function batchDelete(): Promise<void> {
  const deletable = selectedFiles.value.filter((file) => !attached.value.some((item) => item.id === file.id));
  if (!deletable.length) {
    ElMessage.warning("已关联附件不能直接删除，请先解除关联；系统会保留审计证据保护。");
    return;
  }
  try {
    await ElMessageBox.confirm(`永久删除 ${deletable.length} 个未关联附件？仍被其他底稿引用的附件会被系统跳过。`, "批量删除附件", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  let deleted = 0;
  const errors: string[] = [];
  for (const file of deletable) {
    try { await api.deleteFile(file.id); deleted += 1; } catch (error) { errors.push(`${file.orig_name}：${error instanceof Error ? error.message : "删除失败"}`); }
  }
  clearSelection();
  await load();
  if (deleted) ElMessage.success(`已删除 ${deleted} 个附件`);
  if (errors.length) ElMessage.warning(`有 ${errors.length} 个附件因仍被引用等原因未删除`);
}

function evidenceCommand(command: string): void {
  if (command === "file") picker.value?.click();
  if (command === "folder") folderPicker.value?.click();
  if (command === "library") void openAttachmentDirectory();
}

function fileCommand(file: EvidenceFile, command: string): void {
  if (command === "download") void download(file);
  if (command === "rename") void rename(file);
  if (command === "references") void showReferences(file);
  if (command === "move") startMove([file]);
  if (command === "shared") void toggleShared(file);
  if (command === "exclusive") void link(file, true);
  if (command === "delete") void remove(file);
}
</script>

<template>
  <article class="evidence panel" :class="{ 'evidence-dragging': dragging }" tabindex="0" @dragenter.prevent="onDragEnter" @dragover.prevent @dragleave.prevent="onDragLeave" @drop.prevent="onDrop" @paste="onPaste">
    <div class="panel-head"><div><p class="eyebrow">审计证据</p><h2>附件列表</h2></div><div class="evidence-actions"><el-dropdown trigger="click" @command="evidenceCommand"><el-button type="primary" :loading="uploading">导入证据 ▾</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="file">上传文件</el-dropdown-item><el-dropdown-item command="folder">导入文件夹</el-dropdown-item><el-dropdown-item divided command="library">打开本单位附件库</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></div>
    <input ref="picker" class="hidden-input" type="file" multiple @change="upload" />
    <input ref="folderPicker" class="hidden-input" type="file" webkitdirectory multiple @change="uploadFolder" />
    <div v-if="dragging" class="evidence-drop-mask">释放以导入文件或文件夹</div>
    <p class="evidence-tip">可拖入文件/文件夹，或点击附件区域后粘贴截图；文件夹作为一个证据实体保留目录结构，同内容文件会复用。</p>
    <div v-if="selectedFiles.length" class="batch-bar"><strong>已选 {{ selectedFiles.length }} 项</strong><el-button text size="small" @click="batchLink">批量关联</el-button><el-button text size="small" @click="batchUnlink">批量解除</el-button><el-button text size="small" @click="openBatchRename">批量重命名</el-button><el-button text size="small" @click="startMove(selectedFiles)">批量移动</el-button><el-button text type="danger" size="small" @click="batchDelete">批量删除</el-button><el-button text size="small" @click="clearSelection">取消选择</el-button></div>
    <div class="evidence-subhead"><h3>当前底稿附件（{{ attached.length }}）</h3><span class="selection-tools"><el-button text size="small" :disabled="!attached.length" @click="selectAll(attached)">全选</el-button><el-button text size="small" :disabled="!attached.length" @click="invertSelection(attached)">反选</el-button></span></div>
    <el-empty v-if="!attached.length" description="尚未关联附件" :image-size="58" />
    <div v-else class="file-list compact-file-list"><div v-for="file in attached" :key="file.id" class="file-row compact-file-row"><input class="file-check" type="checkbox" :checked="isSelected(file)" :aria-label="`选择 ${file.orig_name}`" @change="setSelected(file, ($event.target as HTMLInputElement).checked)" /><span class="file-icon" aria-hidden="true">{{ file.mime === 'folder' ? '📁' : '📎' }}</span><div class="file-info"><strong :title="file.orig_name">{{ file.orig_name }}</strong></div><div class="file-row-tail"><el-dropdown trigger="click" @command="(command: string) => fileCommand(file, command)"><el-button text size="small">更多 ▾</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="download">{{ file.mime === 'folder' ? '查看目录' : '下载' }}</el-dropdown-item><el-dropdown-item command="rename">重命名</el-dropdown-item><el-dropdown-item command="references">查看引用</el-dropdown-item><el-dropdown-item command="move">移动到单位</el-dropdown-item><el-dropdown-item command="shared">{{ file.exclusive_to ? '恢复为共享附件' : '设为仅关联当前底稿' }}</el-dropdown-item></el-dropdown-menu></template></el-dropdown><el-button text type="danger" size="small" @click="unlink(file)">取消关联</el-button></div></div></div>
    <div class="evidence-subhead"><h3>本单位资料库（{{ available.length }}）</h3><span class="selection-tools"><el-button text size="small" :disabled="!available.length" @click="selectAll(available)">全选</el-button><el-button text size="small" :disabled="!available.length" @click="invertSelection(available)">反选</el-button><el-button text size="small" @click="libraryExpanded = !libraryExpanded">{{ libraryExpanded ? '收起' : '展开' }}</el-button></span></div>
    <template v-if="libraryExpanded"><el-empty v-if="!available.length" description="没有可关联的共享附件" :image-size="58" /><div v-else class="file-list compact-file-list"><div v-for="file in available" :key="file.id" class="file-row compact-file-row"><input class="file-check" type="checkbox" :checked="isSelected(file)" :aria-label="`选择 ${file.orig_name}`" @change="setSelected(file, ($event.target as HTMLInputElement).checked)" /><span class="file-icon" aria-hidden="true">{{ file.mime === 'folder' ? '📁' : '📎' }}</span><div class="file-info"><strong :title="file.orig_name">{{ file.orig_name }}</strong></div><div class="file-row-tail"><el-dropdown trigger="click" @command="(command: string) => fileCommand(file, command)"><el-button text size="small">更多 ▾</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="download">{{ file.mime === 'folder' ? '查看目录' : '下载' }}</el-dropdown-item><el-dropdown-item command="rename">重命名</el-dropdown-item><el-dropdown-item command="references">查看引用</el-dropdown-item><el-dropdown-item command="move">移动到单位</el-dropdown-item><el-dropdown-item command="exclusive">仅关联到当前底稿</el-dropdown-item><el-dropdown-item divided command="delete">删除附件</el-dropdown-item></el-dropdown-menu></template></el-dropdown><el-button text type="primary" size="small" @click="link(file)">关联</el-button></div></div></div></template>

    <div v-if="movingFiles.length" class="inline-panel"><strong>移动 {{ movingFiles.length }} 个附件</strong><el-select v-model="moveTarget" placeholder="目标单位"><el-option v-for="unit in units" :key="unit.id" :value="unit.id" :label="unit.name" /></el-select><div><el-button size="small" @click="movingFiles = []">取消</el-button><el-button size="small" type="primary" @click="move">确认移动</el-button></div></div>
    <div v-if="batchRenameItems.length" class="inline-panel"><div class="panel-head"><strong>批量重命名（{{ batchRenameItems.length }} 项）</strong><el-button text size="small" @click="batchRenameItems = []">关闭</el-button></div><div class="batch-rename-list"><el-input v-for="item in batchRenameItems" :key="item.id" v-model="item.name" /></div><div><el-button size="small" @click="batchRenameItems = []">取消</el-button><el-button size="small" type="primary" @click="saveBatchRename">保存重命名</el-button></div></div>
    <div v-if="referenceFile" class="inline-panel"><div class="panel-head"><strong>“{{ referenceFile.orig_name }}”的引用</strong><el-button text size="small" @click="referenceFile = null">关闭</el-button></div><el-empty v-if="!references.length" description="暂无底稿引用" :image-size="46" /><ul v-else class="reference-list"><li v-for="item in references" :key="item.id">{{ item.unit_name }} · 问题{{ item.seq }} · {{ item.defect_type || '未定性' }}</li></ul></div>
  </article>
</template>
