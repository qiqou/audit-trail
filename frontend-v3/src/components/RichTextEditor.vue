<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { richTextCharacterCount } from "../richText";

const props = withDefaults(defineProps<{
  modelValue: string;
  disabled?: boolean;
  placeholder?: string;
  minHeight?: number;
}>(), {
  disabled: false,
  placeholder: "请输入内容",
  minHeight: 150,
});
const emit = defineEmits<{ "update:modelValue": [value: string] }>();

const root = ref<HTMLElement>();
const editor = ref<HTMLDivElement>();
const activeTable = ref<HTMLTableElement | null>(null);
const tableWidth = ref("100");
const commandState = ref({ bold: false, italic: false, underline: false, list: false });
const resizeHandleStyle = ref<Record<string, string>>({ display: "none" });
const internalValue = ref(props.modelValue ?? "");
const characterCount = computed(() => richTextCharacterCount(internalValue.value));
let savedRange: Range | null = null;
let resizeState: { table: HTMLTableElement; startX: number; startY: number; width: number; height: number } | null = null;

const standardColors = [
  { value: "#000000", label: "黑色" }, { value: "#404040", label: "深灰" },
  { value: "#C00000", label: "标准红" }, { value: "#0070C0", label: "标准蓝" },
  { value: "#008000", label: "标准绿" }, { value: "#7030A0", label: "紫色" },
  { value: "#BF9000", label: "橙色" },
];

function syncEditor(value: string): void {
  internalValue.value = value ?? "";
  if (editor.value && document.activeElement !== editor.value && editor.value.innerHTML !== internalValue.value) {
    editor.value.innerHTML = internalValue.value;
    ensurePlainTail();
  }
}

watch(() => props.modelValue, syncEditor, { immediate: true });

function isPlainTail(element: Element | null): boolean {
  const firstChild = element?.firstElementChild;
  return element?.tagName === "P"
    && element.textContent?.replace(/\u00a0/g, "").trim() === ""
    && firstChild?.tagName === "SPAN"
    && (firstChild as HTMLElement).style.fontWeight === "normal";
}

function ensurePlainTail(): void {
  if (!editor.value || isPlainTail(editor.value.lastElementChild)) return;
  const tail = document.createElement("p");
  // 用显式 normal 的尾部段落提供一个不会继承前文格式的可输入位置。
  // 该 span 的样式在服务端白名单内，保存、重开后仍可保持这一边界。
  const plain = document.createElement("span");
  plain.style.fontWeight = "normal";
  plain.append(document.createElement("br"));
  tail.append(plain);
  editor.value.append(tail);
}

function emitValue(): void {
  internalValue.value = editor.value?.innerHTML ?? "";
  emit("update:modelValue", internalValue.value);
}

function refreshCommandState(): void {
  const selection = window.getSelection();
  // 工具栏状态只反映当前真实选区，不能使用已保存的选区回填。
  // 否则光标点到空白处时会把上一次加粗状态误显示为当前状态。
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  const inEditor = Boolean(range && editor.value?.contains(range.commonAncestorContainer));
  commandState.value = inEditor ? {
    bold: document.queryCommandState("bold"),
    italic: document.queryCommandState("italic"),
    underline: document.queryCommandState("underline"),
    list: document.queryCommandState("insertUnorderedList"),
  } : { bold: false, italic: false, underline: false, list: false };
}

function captureSelection(): void {
  const selection = window.getSelection();
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  if (range && editor.value?.contains(range.commonAncestorContainer)) savedRange = range.cloneRange();
  refreshCommandState();
}

function restoreSelection(): void {
  editor.value?.focus();
  if (!savedRange) return;
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(savedRange);
}

function elementAtSelection(): Element | null {
  const selection = window.getSelection();
  const range = selection?.rangeCount ? selection.getRangeAt(0) : savedRange;
  if (!range) return null;
  const node = range.startContainer;
  return node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement;
}

function updateTableContext(): void {
  const table = elementAtSelection()?.closest("table") as HTMLTableElement | null;
  activeTable.value = table && editor.value?.contains(table) ? table : null;
  const width = activeTable.value?.style.width || "100%";
  tableWidth.value = width.endsWith("%") ? width.slice(0, -1) : "custom";
  updateResizeHandle();
}

function updateResizeHandle(): void {
  const table = activeTable.value;
  const container = root.value;
  if (!table || !container || !editor.value?.contains(table)) {
    resizeHandleStyle.value = { display: "none" };
    return;
  }
  const tableRect = table.getBoundingClientRect();
  const containerRect = container.getBoundingClientRect();
  resizeHandleStyle.value = {
    display: "block",
    left: `${tableRect.right - containerRect.left - 11}px`,
    top: `${tableRect.bottom - containerRect.top - 11}px`,
  };
}

function runCommand(command: string, value?: string): void {
  if (props.disabled || (!value && ["fontName", "fontSize"].includes(command))) return;
  restoreSelection();
  // 浏览器原生编辑 API 的输出会在保存时由服务端规范化成受控 span 样式。
  document.execCommand(command, false, value);
  captureSelection();
  updateTableContext();
  emitValue();
}

function toggleInlineCommand(command: "bold" | "italic" | "underline" | "insertUnorderedList"): void {
  if (props.disabled) return;
  restoreSelection();
  // 原生命令本身就是切换操作。只执行一次，绝不根据旧光标状态强制反向改写。
  document.execCommand(command, false);
  captureSelection();
  emitValue();
}

function onEditorInput(): void {
  ensurePlainTail();
  emitValue();
  refreshCommandState();
}

function captureToolbarSelection(): void {
  captureSelection();
  updateTableContext();
}

function setFont(event: Event): void {
  runCommand("fontName", (event.target as HTMLSelectElement).value);
  (event.target as HTMLSelectElement).value = "";
}

function setSize(event: Event): void {
  runCommand("fontSize", (event.target as HTMLSelectElement).value);
  (event.target as HTMLSelectElement).value = "";
}

function setColor(event: Event): void {
  runCommand("foreColor", (event.currentTarget as HTMLButtonElement).value);
}

function insertTable(): void {
  if (props.disabled) return;
  restoreSelection();
  document.execCommand(
    "insertHTML",
    false,
    '<table style="width:100%"><tbody><tr><th>项目</th><th>说明</th></tr><tr><td><br></td><td><br></td></tr></tbody></table><p><br></p>',
  );
  const tables = editor.value?.querySelectorAll<HTMLTableElement>("table");
  activeTable.value = tables?.length ? tables[tables.length - 1] : null;
  tableWidth.value = "100";
  captureSelection();
  emitValue();
}

function activeCell(): HTMLTableCellElement | null {
  const cell = elementAtSelection()?.closest("td,th") as HTMLTableCellElement | null;
  if (cell && editor.value?.contains(cell)) return cell;
  return null;
}

function tableForAction(): HTMLTableElement | null {
  const selectedTable = activeCell()?.closest("table") as HTMLTableElement | null;
  const table = selectedTable ?? activeTable.value;
  return table && editor.value?.contains(table) ? table : null;
}

function completeTableAction(table: HTMLTableElement | null): void {
  activeTable.value = table;
  updateTableContext();
  emitValue();
}

function insertRow(): void {
  restoreSelection();
  const table = tableForAction();
  const row = activeCell()?.parentElement as HTMLTableRowElement | null;
  if (!table || !row) return;
  const created = table.insertRow(row.rowIndex + 1);
  const count = Array.from(row.cells).reduce((sum, cell) => sum + cell.colSpan, 0);
  for (let index = 0; index < count; index += 1) created.insertCell().innerHTML = "<br>";
  completeTableAction(table);
}

function deleteRow(): void {
  restoreSelection();
  const table = tableForAction();
  const row = activeCell()?.parentElement as HTMLTableRowElement | null;
  if (!table || !row || table.rows.length <= 1) return;
  table.deleteRow(row.rowIndex);
  completeTableAction(table);
}

function insertColumn(): void {
  restoreSelection();
  const table = tableForAction();
  const cell = activeCell();
  if (!table || !cell) return;
  const position = cell.cellIndex + 1;
  for (const row of Array.from(table.rows)) {
    const isHeader = row.cells.length > 0 && row.cells[0].tagName === "TH";
    const created = document.createElement(isHeader ? "th" : "td");
    created.innerHTML = "<br>";
    row.insertBefore(created, row.cells[position] ?? null);
  }
  completeTableAction(table);
}

function deleteColumn(): void {
  restoreSelection();
  const table = tableForAction();
  const cell = activeCell();
  if (!table || !cell || table.rows[0]?.cells.length <= 1) return;
  const position = cell.cellIndex;
  for (const row of Array.from(table.rows)) {
    if (row.cells[position]) row.deleteCell(position);
  }
  completeTableAction(table);
}

function mergeRightCell(): void {
  restoreSelection();
  const table = tableForAction();
  const cell = activeCell();
  const next = cell?.nextElementSibling as HTMLTableCellElement | null;
  if (!table || !cell || !next) return;
  cell.colSpan += next.colSpan;
  if (next.innerHTML.trim() && next.innerHTML !== "<br>") cell.innerHTML += `<br>${next.innerHTML}`;
  next.remove();
  completeTableAction(table);
}

function splitCell(): void {
  restoreSelection();
  const table = tableForAction();
  const cell = activeCell();
  if (!table || !cell || cell.colSpan <= 1) return;
  const extraCells = cell.colSpan - 1;
  cell.colSpan = 1;
  for (let index = 0; index < extraCells; index += 1) {
    const created = document.createElement(cell.tagName.toLowerCase());
    created.innerHTML = "<br>";
    cell.insertAdjacentElement("afterend", created);
  }
  completeTableAction(table);
}

function setTableWidth(event: Event): void {
  restoreSelection();
  const table = tableForAction();
  const width = (event.target as HTMLSelectElement).value;
  if (!table || width === "custom") return;
  table.style.width = `${width}%`;
  tableWidth.value = width;
  completeTableAction(table);
}

function resizeTable(event: PointerEvent): void {
  if (!resizeState) return;
  const width = Math.max(160, Math.round(resizeState.width + event.clientX - resizeState.startX));
  const height = Math.max(48, Math.round(resizeState.height + event.clientY - resizeState.startY));
  resizeState.table.style.width = `${width}px`;
  resizeState.table.style.height = `${height}px`;
  updateResizeHandle();
}

function finishTableResize(): void {
  if (!resizeState) return;
  const table = resizeState.table;
  resizeState = null;
  window.removeEventListener("pointermove", resizeTable);
  window.removeEventListener("pointerup", finishTableResize);
  completeTableAction(table);
}

function startTableResize(event: PointerEvent): void {
  const table = tableForAction();
  if (props.disabled || !table) return;
  event.preventDefault();
  resizeState = {
    table,
    startX: event.clientX,
    startY: event.clientY,
    width: table.getBoundingClientRect().width,
    height: table.getBoundingClientRect().height,
  };
  window.addEventListener("pointermove", resizeTable);
  window.addEventListener("pointerup", finishTableResize, { once: true });
}

function rememberTable(event: MouseEvent | KeyboardEvent): void {
  captureSelection();
  const target = event.target as Element | null;
  const table = target?.closest("table") as HTMLTableElement | null;
  activeTable.value = table && editor.value?.contains(table) ? table : null;
  updateTableContext();
  refreshCommandState();
}

onMounted(ensurePlainTail);

onBeforeUnmount(() => {
  window.removeEventListener("pointermove", resizeTable);
  window.removeEventListener("pointerup", finishTableResize);
});
</script>

<template>
  <section ref="root" class="rich-text" :class="{ disabled }">
    <details v-if="!disabled" class="rich-toolbar-disclosure">
      <summary class="rich-toolbar-toggle" title="展开或收起格式工具" aria-label="展开或收起格式工具" @mousedown.stop="captureSelection" />
      <div class="rich-toolbar" aria-label="富文本工具栏" @mousedown.capture="captureToolbarSelection">
      <button type="button" :disabled="disabled" :class="{ active: commandState.bold }" :aria-pressed="commandState.bold" title="加粗" aria-label="加粗" @mousedown.prevent="captureSelection" @click="toggleInlineCommand('bold')"><strong>B</strong></button>
      <button type="button" :disabled="disabled" :class="{ active: commandState.italic }" :aria-pressed="commandState.italic" title="斜体" aria-label="斜体" @mousedown.prevent="captureSelection" @click="toggleInlineCommand('italic')"><em>I</em></button>
      <button type="button" :disabled="disabled" :class="{ active: commandState.underline }" :aria-pressed="commandState.underline" title="下划线" aria-label="下划线" @mousedown.prevent="captureSelection" @click="toggleInlineCommand('underline')"><u>U</u></button>
      <span class="toolbar-divider" />
      <select :disabled="disabled" aria-label="字体" @change="setFont">
        <option value="">字体</option><option value="Arial">Arial</option><option value="Microsoft YaHei">微软雅黑</option><option value="SimSun">宋体</option><option value="KaiTi">楷体</option>
      </select>
      <select :disabled="disabled" aria-label="字号" @change="setSize">
        <option value="">字号</option><option value="2">12</option><option value="3">14</option><option value="4">16</option><option value="5">18</option><option value="6">24</option><option value="7">32</option>
      </select>
      <span class="color-group" aria-label="文字颜色"><span class="color-label">颜色</span><button v-for="color in standardColors" :key="color.value" type="button" class="color-swatch" :disabled="disabled" :value="color.value" :style="{ '--swatch-color': color.value }" :title="color.label" :aria-label="color.label" @mousedown.prevent="captureSelection" @click="setColor"><span /></button></span>
      <button type="button" :disabled="disabled" :class="{ active: commandState.list }" :aria-pressed="commandState.list" title="项目符号" @mousedown.prevent="captureSelection" @click="toggleInlineCommand('insertUnorderedList')">• 列表</button>
      <button type="button" :disabled="disabled" title="清除当前选区的格式" @mousedown.prevent="captureSelection" @click="runCommand('removeFormat')">清除格式</button>
      <span class="toolbar-divider" />
      <button type="button" :disabled="disabled" title="插入 2 列表格" @mousedown.prevent="captureSelection" @click="insertTable">插入表格</button>
      <template v-if="activeTable">
        <button type="button" :disabled="disabled" title="在当前行后新增一行" @mousedown.prevent="captureSelection" @click="insertRow">+行</button>
        <button type="button" :disabled="disabled" title="删除当前行" @mousedown.prevent="captureSelection" @click="deleteRow">−行</button>
        <button type="button" :disabled="disabled" title="在当前列右侧新增一列" @mousedown.prevent="captureSelection" @click="insertColumn">+列</button>
        <button type="button" :disabled="disabled" title="删除当前列" @mousedown.prevent="captureSelection" @click="deleteColumn">−列</button>
        <button type="button" :disabled="disabled" title="合并当前单元格及其右侧单元格" @mousedown.prevent="captureSelection" @click="mergeRightCell">合并右格</button>
        <button type="button" :disabled="disabled" title="拆分当前已合并单元格" @mousedown.prevent="captureSelection" @click="splitCell">拆分单元格</button>
        <select :value="tableWidth" :disabled="disabled" aria-label="表格宽度" title="表格宽度" @change="setTableWidth">
          <option value="50">表格 50%</option><option value="75">表格 75%</option><option value="100">表格 100%</option><option value="custom">自定义宽度</option>
        </select>
      </template>
      <span class="toolbar-divider" />
      <button type="button" :disabled="disabled" title="撤销" @mousedown.prevent="captureSelection" @click="runCommand('undo')">撤销</button>
      <button type="button" :disabled="disabled" title="重做" @mousedown.prevent="captureSelection" @click="runCommand('redo')">重做</button>
      </div>
    </details>
    <div ref="editor" class="rich-editable" :class="{ empty: !internalValue }" :style="{ minHeight: `${minHeight}px` }" :contenteditable="!disabled" role="textbox" aria-multiline="true" :data-placeholder="placeholder" @focus="captureSelection" @input="onEditorInput" @keyup="rememberTable" @scroll="updateResizeHandle" />
    <button v-if="activeTable && !disabled" type="button" class="table-resize-handle" :style="resizeHandleStyle" title="拖拽调整表格宽度和高度" aria-label="拖拽调整表格宽度和高度" @pointerdown="startTableResize">↘</button>
    <footer class="rich-footer">字数：{{ characterCount }}<span v-if="activeTable"> · 已选中表格，可在工具栏调整</span></footer>
  </section>
</template>

<style scoped>
.rich-text { position: relative; border: 1px solid var(--el-border-color, #dcdfe6); border-radius: 6px; overflow: hidden; background: var(--el-bg-color, transparent); color: var(--el-text-color-primary, inherit); }
.rich-text:focus-within { border-color: var(--el-color-primary, #409eff); box-shadow: 0 0 0 2px color-mix(in srgb, var(--el-color-primary, #409eff) 14%, transparent); }
.rich-text.disabled { background: var(--el-fill-color-light, transparent); }
.rich-toolbar-disclosure { margin: 0; padding: 0; }
.rich-toolbar-toggle { position: absolute; z-index: 4; top: 5px; right: 7px; display: grid; width: 24px; height: 24px; padding: 0; place-items: center; list-style: none; border: 0; border-radius: 4px; background: transparent; color: var(--el-text-color-secondary, #909399); cursor: pointer; font-size: 15px; line-height: 1; }
.rich-toolbar-toggle::-webkit-details-marker { display: none; }
.rich-toolbar-toggle::before { content: "⌄"; }
.rich-toolbar-disclosure[open] .rich-toolbar-toggle::before { content: "⌃"; }
.rich-toolbar-toggle:hover:not(:disabled) { color: var(--el-color-primary, #409eff); background: var(--el-fill-color, #f0f2f5); }
.rich-toolbar-toggle:disabled { cursor: not-allowed; opacity: .65; }
.rich-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 8px; border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5); background: var(--el-fill-color-light, #f5f7fa); }
.rich-toolbar button, .rich-toolbar select { height: 28px; border: 1px solid var(--el-border-color, #dcdfe6); border-radius: 4px; background: var(--el-fill-color-blank, var(--el-bg-color, transparent)); color: inherit; font-size: 12px; padding: 0 7px; }
.rich-toolbar button { cursor: pointer; }
.rich-toolbar button:disabled, .rich-toolbar select:disabled { cursor: not-allowed; opacity: .65; }
.rich-toolbar button.active { border-color: var(--el-color-primary, #409eff); background: var(--el-color-primary-light-8, rgba(64, 158, 255, .16)); color: var(--el-color-primary, #409eff); box-shadow: inset 0 0 0 1px var(--el-color-primary, #409eff); }
.toolbar-divider { width: 1px; height: 20px; margin: 0 2px; background: var(--el-border-color, #dcdfe6); }
.color-group { display: inline-flex; align-items: center; gap: 3px; }
.color-label { color: var(--el-text-color-secondary, #909399); font-size: 12px; }
.rich-toolbar .color-swatch { display: inline-grid; width: 20px; min-width: 20px; padding: 3px; place-items: center; }
.color-swatch span { display: block; width: 10px; height: 10px; border: 1px solid color-mix(in srgb, var(--swatch-color) 75%, #fff); border-radius: 2px; background: var(--swatch-color); }
.rich-editable { overflow-x: auto; padding: 10px 42px 10px 12px; outline: none; background: transparent; color: inherit; line-height: 1.65; word-break: break-word; }
.rich-editable.empty::before { content: attr(data-placeholder); color: var(--el-text-color-placeholder, #a8abb2); pointer-events: none; }
.rich-editable :deep(table) { width: 100%; min-width: 160px; min-height: 48px; margin: 8px 0; border-collapse: collapse; }
.rich-editable :deep(th), .rich-editable :deep(td) { min-width: 72px; padding: 5px 7px; border: 1px solid var(--el-border-color, #c9ced6); vertical-align: top; }
.rich-editable :deep(th) { background: var(--el-fill-color-light, #f5f7fa); font-weight: 600; }
.table-resize-handle { position: absolute; z-index: 2; width: 22px; height: 22px; border: 1px solid var(--el-color-primary, #409eff); border-radius: 50%; background: var(--el-bg-color, #fff); color: var(--el-color-primary, #409eff); cursor: nwse-resize; font-size: 13px; line-height: 18px; padding: 0; touch-action: none; }
.rich-footer { padding: 4px 10px; border-top: 1px solid var(--el-border-color-lighter, #ebeef5); color: var(--el-text-color-secondary, #909399); font-size: 12px; text-align: right; }
</style>
