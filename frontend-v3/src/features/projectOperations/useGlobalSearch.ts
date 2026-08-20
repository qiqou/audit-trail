import { ref } from "vue";

import { api, type SearchResult } from "../../api/client";

/** 项目级全局搜索的查询、防抖和生命周期清理。 */
export function useGlobalSearch(report: (error: unknown) => void) {
  const searchQuery = ref("");
  const searchResult = ref<SearchResult | null>(null);
  const searchLoading = ref(false);
  let searchTimer: ReturnType<typeof window.setTimeout> | undefined;

  async function runSearch(): Promise<void> {
    const query = searchQuery.value.trim();
    if (!query) {
      searchResult.value = null;
      return;
    }
    searchLoading.value = true;
    try {
      searchResult.value = await api.search(query);
    } catch (error) {
      report(error);
    } finally {
      searchLoading.value = false;
    }
  }

  function onSearchInput(): void {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { void runSearch(); }, 300);
  }

  function dispose(): void {
    window.clearTimeout(searchTimer);
  }

  return { searchQuery, searchResult, searchLoading, runSearch, onSearchInput, dispose };
}
