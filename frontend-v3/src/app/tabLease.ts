export type TabLease = { tabId: string; expiresAt: number };

export interface LeaseStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * 协调浏览器标签页的本地写入权。它不保存项目数据，只保存一个短期租约标记；
 * 后端项目租约仍是最终写入权限判断。
 */
export function createTabLease(
  storage: LeaseStorage, key: string, tabId: string, durationMs: number, now: () => number = Date.now,
) {
  function read(): TabLease | null {
    try {
      const value = JSON.parse(storage.getItem(key) ?? "null") as Partial<TabLease> | null;
      return value && typeof value.tabId === "string" && typeof value.expiresAt === "number"
        ? { tabId: value.tabId, expiresAt: value.expiresAt }
        : null;
    } catch {
      return null;
    }
  }

  function write(): void {
    storage.setItem(key, JSON.stringify({ tabId, expiresAt: now() + durationMs } satisfies TabLease));
  }

  return {
    claim(): boolean {
      const existing = read();
      if (existing && existing.tabId !== tabId && existing.expiresAt > now()) return false;
      write();
      return read()?.tabId === tabId;
    },
    renew(): boolean {
      const existing = read();
      if (existing && existing.tabId !== tabId && existing.expiresAt > now()) return false;
      write();
      return true;
    },
    heldByOther(): boolean {
      const existing = read();
      return Boolean(existing && existing.tabId !== tabId && existing.expiresAt > now());
    },
    release(): void {
      if (read()?.tabId === tabId) storage.removeItem(key);
    },
  };
}
