<script setup lang="ts">
import { ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type Issue, type ReviewNote } from "../api/client";

const props = defineProps<{ issue: Issue }>();

const notes = ref<ReviewNote[]>([]);
const loading = ref(false);
const creating = ref(false);
const body = ref("");
const anchorField = ref("");

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请重试";
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    notes.value = await api.reviewNotes(props.issue.id);
  } catch (error) {
    ElMessage.error(errorMessage(error));
  } finally {
    loading.value = false;
  }
}

async function create(): Promise<void> {
  if (!body.value.trim() || creating.value) return;
  creating.value = true;
  try {
    const baseline = await api.issueDraft(props.issue.id);
    const created = await api.createReviewNote(
      props.issue.id, body.value.trim(), baseline.current_version_id, anchorField.value,
    );
    notes.value = [...notes.value, created];
    body.value = "";
    anchorField.value = "";
    ElMessage.success("已提出内部复核意见");
  } catch (error) {
    ElMessage.error(errorMessage(error));
  } finally {
    creating.value = false;
  }
}

async function append(note: ReviewNote, eventType: "replied" | "resolved" | "reopened"): Promise<void> {
  const labels = { replied: "回复", resolved: "清除", reopened: "重开" };
  let eventBody = "";
  try {
    if (eventType === "replied" || eventType === "reopened") {
      const result = await ElMessageBox.prompt(
        `${labels[eventType]}将追加到意见历史，既有内容不会被改写。`,
        `${labels[eventType]}复核意见`,
        { inputValidator: (value) => Boolean(value?.trim()) || "内容不能为空" },
      );
      eventBody = result.value.trim();
    } else {
      await ElMessageBox.confirm("清除只会追加处理事件，原意见和回复仍可查阅。", "清除复核意见", {
        type: "warning", confirmButtonText: "确认清除", cancelButtonText: "取消",
      });
    }
    const updated = await api.appendReviewNoteEvent(note.note_uuid, eventType, eventBody);
    notes.value = notes.value.map((item) => item.note_uuid === updated.note_uuid ? updated : item);
    ElMessage.success(`已${labels[eventType]}复核意见`);
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(errorMessage(error));
  }
}

watch(() => props.issue.id, () => { void refresh(); }, { immediate: true });
</script>

<template>
  <section class="review-notes">
    <div class="review-notes-head"><h3>内部复核意见</h3><el-button text size="small" :loading="loading" @click="refresh">刷新</el-button></div>
    <div class="review-create">
      <el-input v-model="body" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" placeholder="提出需复核的具体事项" />
      <div class="review-create-actions"><el-select v-model="anchorField" clearable placeholder="关联字段（可选）"><el-option label="缺陷描述" value="defect_desc" /><el-option label="制度依据" value="regulation_basis" /><el-option label="审计建议" value="suggestion" /></el-select><el-button type="primary" size="small" :disabled="!body.trim()" :loading="creating" @click="create">提出意见</el-button></div>
    </div>
    <p v-if="!loading && !notes.length" class="review-empty">暂无内部复核意见</p>
    <article v-for="note in notes" :key="note.note_uuid" class="review-note">
      <div class="review-note-meta"><span>{{ note.created_by }} · {{ note.created_at }}</span><span v-if="note.anchor_field">关联：{{ note.anchor_field }}</span><el-tag size="small" :type="note.status === 'resolved' ? 'success' : 'warning'">{{ note.status === 'resolved' ? '已清除' : '待处理' }}</el-tag><el-tag v-if="note.is_stale" size="small" type="info">旧版本锚点</el-tag></div>
      <p>{{ note.body }}</p>
      <p v-for="event in note.events.slice(1)" :key="event.event_uuid" class="review-event">{{ event.created_by }} · {{ event.event_type === 'replied' ? '回复' : event.event_type === 'resolved' ? '清除' : '重开' }}{{ event.body ? `：${event.body}` : '' }}</p>
      <div class="review-note-actions"><el-button v-if="note.status === 'open'" text size="small" @click="append(note, 'replied')">回复</el-button><el-button v-if="note.status === 'open'" text size="small" type="success" @click="append(note, 'resolved')">清除</el-button><el-button v-else text size="small" @click="append(note, 'reopened')">重开</el-button></div>
    </article>
  </section>
</template>

<style scoped>
.review-notes { display: grid; gap: 10px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--el-border-color-lighter); }
.review-notes-head, .review-create-actions, .review-note-meta, .review-note-actions { display: flex; align-items: center; gap: 8px; }
.review-notes-head { justify-content: space-between; }.review-notes h3 { margin: 0; font-size: 14px; }.review-create { display: grid; gap: 8px; }.review-create-actions { justify-content: flex-end; }.review-create-actions :deep(.el-select) { width: 150px; }.review-empty { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }.review-note { padding: 10px; border-radius: 6px; background: var(--el-fill-color-light); }.review-note-meta { flex-wrap: wrap; color: var(--el-text-color-secondary); font-size: 12px; }.review-note p { margin: 7px 0; white-space: pre-wrap; }.review-event { padding-left: 8px; border-left: 2px solid var(--el-border-color); color: var(--el-text-color-secondary); font-size: 12px; }.review-note-actions { justify-content: flex-end; }
</style>
