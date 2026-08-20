import { computed, ref, type Ref } from "vue";
import { ElMessage } from "element-plus";

import { api, type ArchivePreflight, type Unit } from "../../api/client";

type ArchiveOperationOptions = {
  units: () => Unit[];
  working: Ref<boolean>;
  report: (error: unknown) => void;
};

/** 归档范围、预检令牌和下载动作保持在同一模块，避免绕过预检直接打包。 */
export function useArchiveOperation({ units, working, report }: ArchiveOperationOptions) {
  const packageScope = ref<"all" | "selected">("all");
  const packageUnitIds = ref<number[]>([]);
  const groupByDepartment = ref(false);
  const archivePreflight = ref<ArchivePreflight | null>(null);
  const selectedPackageCount = computed(() => (
    packageScope.value === "all" ? units().length : packageUnitIds.value.length
  ));

  function clearArchivePreflight(): void {
    archivePreflight.value = null;
  }

  function initialize(): void {
    if (!packageUnitIds.value.length) packageUnitIds.value = units().map((unit) => unit.id);
  }

  function togglePackageUnit(id: number, checked: boolean): void {
    packageUnitIds.value = checked
      ? [...new Set([...packageUnitIds.value, id])]
      : packageUnitIds.value.filter((item) => item !== id);
  }

  function hasSelectedUnits(): boolean {
    if (packageScope.value !== "selected" || packageUnitIds.value.length) return true;
    ElMessage.warning("请至少勾选一个被审计单位");
    return false;
  }

  async function prepareArchivePreflight(): Promise<void> {
    if (!hasSelectedUnits()) return;
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

  async function packageProject(): Promise<void> {
    if (!archivePreflight.value?.confirmation_token) {
      await prepareArchivePreflight();
      return;
    }
    if (!hasSelectedUnits()) return;
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

  return {
    archivePreflight,
    clearArchivePreflight,
    groupByDepartment,
    initialize,
    packageProject,
    packageScope,
    packageUnitIds,
    prepareArchivePreflight,
    selectedPackageCount,
    togglePackageUnit,
  };
}
