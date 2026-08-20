import { api } from "../../api/client";
import { useProjectStore } from "../../app/projectStore";

/** 当前项目的单位与录入预设刷新；这些值均以后端 SQLite 投影为准。 */
export function useReferenceData(report: (error: unknown) => void) {
  const projectStore = useProjectStore();

  async function refreshUnits(): Promise<void> {
    try {
      projectStore.units = await api.units();
    } catch (error) {
      report(error);
    }
  }

  async function refreshDepartments(): Promise<void> {
    try {
      projectStore.departments = await api.departments();
    } catch (error) {
      report(error);
    }
  }

  async function refreshCategories(): Promise<void> {
    try {
      projectStore.categories = await api.categories();
    } catch (error) {
      report(error);
    }
  }

  async function refreshIssueNumber(): Promise<void> {
    try {
      projectStore.issueNumberRule = await api.issueNumber();
    } catch (error) {
      report(error);
    }
  }

  async function refreshAll(): Promise<void> {
    await Promise.all([refreshUnits(), refreshDepartments(), refreshCategories(), refreshIssueNumber()]);
  }

  return { refreshUnits, refreshDepartments, refreshCategories, refreshIssueNumber, refreshAll };
}
