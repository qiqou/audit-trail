/** 富文本编辑器的纯函数：可在无 DOM 的逻辑测试中验证。 */

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
