import { ref, type Ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type ExcelImportPreflight, type ImportResult, type Unit } from "../../api/client";

type ExcelTransferOptions = {
  units: () => Unit[];
  working: Ref<boolean>;
  report: (error: unknown) => void;
  dataChanged: () => void;
};

/** Excel 导入与导出都经由这一模块，导入仍坚持“先预检、后提交”。 */
export function useExcelTransferOperation({ units, working, report, dataChanged }: ExcelTransferOptions) {
  const importPicker = ref<HTMLInputElement | null>(null);
  const importFile = ref<File | null>(null);
  const importResult = ref<ImportResult | null>(null);
  const importPreflight = ref<ExcelImportPreflight | null>(null);
  const exportScope = ref<"project" | "unit">("project");
  const exportUnitId = ref<number | null>(null);

  function initializeExport(): void {
    if (!exportUnitId.value) exportUnitId.value = units()[0]?.id ?? null;
  }

  function resetImport(): void {
    importResult.value = null;
  }

  function inputImportFile(event: Event): void {
    importFile.value = (event.target as HTMLInputElement).files?.[0] ?? null;
    importPreflight.value = null;
    importResult.value = null;
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
      importPreflight.value = await api.preflightExcelImport(importFile.value);
      if (importPreflight.value.errors.length) {
        ElMessage.warning(`预检发现 ${importPreflight.value.errors.length} 项错误，未写入项目`);
        return;
      }
      await ElMessageBox.confirm(
        `将新增 ${importPreflight.value.imported} 条底稿，并新建 ${importPreflight.value.new_units} 个单位。确认提交？`,
        "Excel 导入预检通过",
        { type: "warning", confirmButtonText: "确认提交", cancelButtonText: "取消" },
      );
      importResult.value = await api.commitExcelImport(importFile.value, importPreflight.value.confirmation_token);
      dataChanged();
      ElMessage.success(`导入完成：${importResult.value.imported} 条底稿${importResult.value.skipped ? `，跳过 ${importResult.value.skipped} 条` : ""}`);
    } catch (error) {
      if (error !== "cancel") report(error);
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

  return {
    downloadTemplate,
    exportExcel,
    exportScope,
    exportUnitId,
    importExcel,
    importFile,
    importPicker,
    importPreflight,
    importResult,
    initializeExport,
    inputImportFile,
    resetImport,
  };
}
