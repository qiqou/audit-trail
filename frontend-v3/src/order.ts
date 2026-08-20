/** 在完整 ID 队列中把一个对象移动到目标对象之前；无效输入保持原顺序。 */
export function moveIdBefore(ids: number[], movedId: number, targetId: number): number[] {
  if (movedId === targetId) return ids;
  const sourceIndex = ids.indexOf(movedId);
  const targetIndex = ids.indexOf(targetId);
  if (sourceIndex < 0 || targetIndex < 0) return ids;
  const next = [...ids];
  next.splice(sourceIndex, 1);
  next.splice(next.indexOf(targetId), 0, movedId);
  return next;
}
