import { describe, expect, it } from "vitest";

import { createTabLease, type LeaseStorage } from "./tabLease";

class MemoryStorage implements LeaseStorage {
  private readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

describe("single-tab lease", () => {
  it("blocks a concurrent tab, expires safely, and never releases another tab's lease", () => {
    const storage = new MemoryStorage();
    let clock = 1_000;
    const first = createTabLease(storage, "lease", "tab-a", 100, () => clock);
    const second = createTabLease(storage, "lease", "tab-b", 100, () => clock);
    expect(first.claim()).toBe(true);
    expect(second.claim()).toBe(false);
    second.release();
    expect(first.heldByOther()).toBe(false);
    clock = 1_101;
    expect(second.claim()).toBe(true);
    expect(first.renew()).toBe(false);
  });
});
