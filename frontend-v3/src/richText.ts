/**
 * 富文本编辑器的纯函数：可在无 DOM 的逻辑测试中验证。
 *
 * v1.4 预留（2026-08-20 范围调整）：v1.3 正式剔除富文本功能，此模块保留
 * 纯文本提取/字数/HTML 转义规范，供 v1.4 富文本编辑器与只读渲染复用；
 * 当前无 UI 调用方，仅由 richText.test.ts 覆盖。
 */

export function richTextToPlainText(value: string): string {
  return value
    .replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/(?:td|th)>/gi, " | ")
    .replace(/<\/(?:p|div|li|tr|h[1-6])>/gi, "\n")
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .split("\n")
    .map((line) => line.replace(/[ \t\r\f\v]+/g, " ").trim().replace(/\s*\|\s*$/, ""))
    .filter(Boolean)
    .join("\n");
}

export function richTextCharacterCount(value: string): number {
  return Array.from(richTextToPlainText(value).replace(/\s/g, "")).length;
}

export function plainTextToRichHtml(value: string): string {
  const escaped = String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
  if (!escaped) return "";
  return escaped.split(/\r?\n/).map((line) => `<p>${line || "<br>"}</p>`).join("");
}
