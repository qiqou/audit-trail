/**
 * 工作区快捷键只在非编辑区域生效：不抢占浏览器组合键，
 * 也绝不截获输入框、下拉框或未来富文本编辑器中的按键。
 */
export type WorkspaceShortcut = "new-issue" | "next-issue" | "previous-issue" | "toggle-issue-list";

export interface ShortcutBinding {
  key: string;
  altKey: boolean;
}

export type WorkspaceShortcuts = Record<WorkspaceShortcut, ShortcutBinding>;

export interface ShortcutInput {
  key: string;
  altKey: boolean;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  defaultPrevented?: boolean;
  target?: EventTarget | null;
}

export const WORKSPACE_SHORTCUT_STORAGE_KEY = "audit_workspace_shortcuts_v1";

export const DEFAULT_WORKSPACE_SHORTCUTS: WorkspaceShortcuts = {
  "previous-issue": { key: "ArrowUp", altKey: false },
  "next-issue": { key: "ArrowDown", altKey: false },
  "new-issue": { key: "n", altKey: true },
  "toggle-issue-list": { key: "b", altKey: true },
};

export const workspaceShortcutLabels: Array<{ action: WorkspaceShortcut; description: string }> = [
  { action: "previous-issue", description: "定位上一份底稿" },
  { action: "next-issue", description: "定位下一份底稿" },
  { action: "new-issue", description: "在当前单位新建底稿" },
  { action: "toggle-issue-list", description: "收起或展开问题列表" },
];

function isEditableTarget(target: EventTarget | null | undefined): boolean {
  if (!target || typeof target !== "object") return false;
  const node = target as { tagName?: string; isContentEditable?: boolean; closest?: (selector: string) => unknown };
  const tagName = String(node.tagName ?? "").toUpperCase();
  if (node.isContentEditable || tagName === "INPUT" || tagName === "TEXTAREA" || tagName === "SELECT") return true;
  return typeof node.closest === "function" && Boolean(node.closest("[contenteditable='true'], [role='textbox']"));
}

function canonicalKey(key: string): string {
  return key === "ArrowUp" || key === "ArrowDown" ? key : key.toLocaleLowerCase("en-US");
}

function isAllowedBinding(binding: unknown): binding is ShortcutBinding {
  if (!binding || typeof binding !== "object") return false;
  const candidate = binding as { key?: unknown; altKey?: unknown };
  if (typeof candidate.key !== "string" || typeof candidate.altKey !== "boolean") return false;
  const key = canonicalKey(candidate.key);
  return key === "ArrowUp" || key === "ArrowDown" || (candidate.altKey && /^[a-z0-9]$/i.test(key));
}

function bindingSignature(binding: ShortcutBinding): string {
  return `${binding.altKey ? "Alt+" : ""}${canonicalKey(binding.key)}`;
}

export function normalizeWorkspaceShortcuts(value: unknown): WorkspaceShortcuts {
  const raw = value && typeof value === "object" ? value as Partial<WorkspaceShortcuts> : {};
  const result = Object.fromEntries(
    (Object.keys(DEFAULT_WORKSPACE_SHORTCUTS) as WorkspaceShortcut[]).map((action) => [action, { ...DEFAULT_WORKSPACE_SHORTCUTS[action] }]),
  ) as WorkspaceShortcuts;
  const used = new Set(Object.values(result).map(bindingSignature));

  for (const action of Object.keys(DEFAULT_WORKSPACE_SHORTCUTS) as WorkspaceShortcut[]) {
    if (!isAllowedBinding(raw[action])) continue;
    const candidate = { key: canonicalKey(raw[action]!.key), altKey: raw[action]!.altKey };
    const current = result[action];
    used.delete(bindingSignature(current));
    if (used.has(bindingSignature(candidate))) {
      used.add(bindingSignature(current));
      continue;
    }
    result[action] = candidate;
    used.add(bindingSignature(candidate));
  }
  return result;
}

export function formatShortcut(binding: ShortcutBinding): string {
  const key = canonicalKey(binding.key);
  const displayKey = key === "ArrowUp" ? "↑" : key === "ArrowDown" ? "↓" : key.toUpperCase();
  return binding.altKey ? `Alt + ${displayKey}` : displayKey;
}

export function resolveWorkspaceShortcut(
  event: ShortcutInput,
  shortcuts: WorkspaceShortcuts = DEFAULT_WORKSPACE_SHORTCUTS,
): WorkspaceShortcut | null {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || isEditableTarget(event.target)) return null;
  const input = { key: canonicalKey(event.key), altKey: event.altKey };
  for (const action of Object.keys(shortcuts) as WorkspaceShortcut[]) {
    const binding = shortcuts[action];
    if (canonicalKey(binding.key) === input.key && binding.altKey === input.altKey) return action;
  }
  return null;
}
