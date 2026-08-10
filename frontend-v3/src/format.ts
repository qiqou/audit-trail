/**
 * 底稿编号格式化（V3.2）：编号 = 前缀 + 数字序号 + 后缀。
 *
 * 规则来自数据层（/api/settings/issue-number），树/标题/复制统一走这里，
 * 与后端 export.issue_no() 保持一致；默认前后缀为空 = 纯数字序号。
 */
export function formatIssueNo(
  seq: number | string,
  rule: { prefix: string; suffix: string },
): string {
  return `${rule.prefix}${seq}${rule.suffix}`;
}
