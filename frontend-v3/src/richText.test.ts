import { describe, expect, it } from "vitest";

import { plainTextToRichHtml, richTextCharacterCount, richTextToPlainText } from "./richText";

describe("rich text helpers", () => {
  it("converts paragraph and table content to stable plain text", () => {
    expect(richTextToPlainText("<p>审计 <strong>发现</strong></p><table><tr><td>金额</td><td>120</td></tr></table>"))
      .toBe("审计 发现\n金额 | 120");
  });

  it("counts visible characters, excluding whitespace and markup", () => {
    expect(richTextCharacterCount("<p>审计 <strong>发现</strong></p>")).toBe(4);
  });

  it("escapes legacy plain text before putting it into the editor", () => {
    expect(plainTextToRichHtml("A < B\nC")).toBe("<p>A &lt; B</p><p>C</p>");
  });
});
