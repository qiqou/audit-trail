import { describe, expect, it } from "vitest";

import {
  DEFAULT_WORKSPACE_SHORTCUTS,
  formatShortcut,
  normalizeWorkspaceShortcuts,
  resolveWorkspaceShortcut,
} from "./shortcuts";

const base = { altKey: false, shiftKey: false, ctrlKey: false, metaKey: false };

describe("resolveWorkspaceShortcut", () => {
  it("识别默认的方向键和 Alt 工作区快捷键", () => {
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp" })).toBe("previous-issue");
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowDown" })).toBe("next-issue");
    expect(resolveWorkspaceShortcut({ ...base, key: "n", altKey: true })).toBe("new-issue");
    expect(resolveWorkspaceShortcut({ ...base, key: "B", altKey: true })).toBe("toggle-issue-list");
    expect(resolveWorkspaceShortcut({ ...base, key: "x" })).toBeNull();
  });

  it("不接管浏览器组合键、已处理事件和编辑区域按键", () => {
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp", ctrlKey: true })).toBeNull();
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp", metaKey: true })).toBeNull();
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp", shiftKey: true })).toBeNull();
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp", defaultPrevented: true })).toBeNull();
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp", target: { tagName: "textarea" } as unknown as EventTarget })).toBeNull();
    expect(resolveWorkspaceShortcut({ ...base, key: "ArrowUp", target: { isContentEditable: true } as unknown as EventTarget })).toBeNull();
  });

  it("使用有效且不重复的用户配置，异常配置回退默认值", () => {
    const custom = normalizeWorkspaceShortcuts({
      "new-issue": { key: "m", altKey: true },
      "next-issue": { key: "ArrowUp", altKey: false },
      "previous-issue": { key: "x", altKey: false },
    });
    expect(custom["new-issue"]).toEqual({ key: "m", altKey: true });
    expect(custom["next-issue"]).toEqual(DEFAULT_WORKSPACE_SHORTCUTS["next-issue"]);
    expect(custom["previous-issue"]).toEqual(DEFAULT_WORKSPACE_SHORTCUTS["previous-issue"]);
    expect(resolveWorkspaceShortcut({ ...base, key: "m", altKey: true }, custom)).toBe("new-issue");
  });

  it("以清晰的本地化文本展示快捷键", () => {
    expect(formatShortcut({ key: "ArrowUp", altKey: false })).toBe("↑");
    expect(formatShortcut({ key: "b", altKey: true })).toBe("Alt + B");
  });
});
