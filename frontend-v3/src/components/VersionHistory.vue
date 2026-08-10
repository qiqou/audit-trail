<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, type Issue, type IssueVersion } from "../api/client";

const props = defineProps<{ issue: Issue; beforeRestore?: () => Promise<boolean> }>();
const emit = defineEmits<{ restored: [issue: Issue] }>();

const expanded = ref(false);
const loading = ref(false);
const versions = ref<IssueVersion[]>([]);
const preview = ref<IssueVersion | null>(null);
const previewVisible = computed({
  get: () => preview.value !== null,
  set: (visible: boolean) => { if (!visible) preview.value = null; },
});

const ordered = computed(() => [...versions.value].reverse());

function describe(version: IssueVersion): string {
  const snapshot = version.snapshot;
  const description = (snapshot.defect_desc ?? "").replace(/\s+/g, " ").trim();
  return `${snapshot.department || "未分版块"} · ${snapshot.defect_type || "未定性"}${description ? ` · ${description.slice(0, 48)}` : ""}`;
}

function report(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : "版本操作失败，请重试");
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    versions.value = await api.versions(props.issue.id);
  } catch (error) {
    report(error);
  } finally {
    loading.value = false;
  }
}

async function toggle(): Promise<void> {
  expanded.value = !expanded.value;
  if (expanded.value) await refresh();
}

async function restore(version: IssueVersion): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `恢复到版本 ${version.version_no}（${version.created_at}，保存人：${version.saved_by || "未知"}）？当前内容会先自动留存为新版本。`,
      "恢复底稿版本",
      { type: "warning", confirmButtonText: "恢复", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  if (props.beforeRestore && !(await props.beforeRestore())) return;
  loading.value = true;
  try {
    await api.restoreVersion(props.issue.id, version.id);
    const fresh = await api.issue(props.issue.id);
    emit("restored", fresh);
    await refresh();
    ElMessage.success(`已恢复至版本 ${version.version_no}`);
  } catch (error) {
    report(error);
  } finally {
    loading.value = false;
  }
}

watch(() => props.issue.id, () => {
  versions.value = [];
  expanded.value = false;
  preview.value = null;
});
</script>

<template>
  <section class="version-history">
    <el-button size="small" :loading="loading" @click="toggle">{{ expanded ? "收起版本历史" : "版本历史" }}</el-button>
    <div v-if="expanded" class="version-list">
      <el-empty v-if="!versions.length && !loading" description="暂无版本记录" :image-size="52" />
      <div v-for="version in ordered" :key="version.id" class="version-row">
        <div><strong>v{{ version.version_no }}</strong><span>{{ describe(version) }}</span><small>{{ version.created_at }} · {{ version.saved_by || "未知" }}</small></div>
        <div class="version-actions"><el-button text type="primary" size="small" @click="preview = version">预览</el-button><el-button size="small" :disabled="issue.status === '已归档'" @click="restore(version)">恢复</el-button></div>
      </div>
      <p v-if="issue.status === '已归档'" class="version-hint">已归档底稿不能直接恢复历史版本，请先执行“归档后编辑”。</p>
    </div>
    <el-dialog v-model="previewVisible" :title="preview ? `版本 ${preview.version_no} · ${preview.created_at}` : '版本预览'" width="min(560px, calc(100vw - 32px))" append-to-body>
      <div v-if="preview" class="version-detail">
        <div class="version-detail-row"><span>所属版块</span><strong>{{ preview.snapshot.department || '（空）' }}</strong></div>
        <div class="version-detail-row"><span>问题分类</span><strong>{{ preview.snapshot.category || '（空）' }}</strong></div>
        <div class="version-detail-row"><span>缺陷定性</span><strong>{{ preview.snapshot.defect_type || '（空）' }}</strong></div>
        <div class="version-detail-row"><span>问题金额</span><strong>{{ preview.snapshot.amount || '（空）' }}</strong></div>
        <div class="version-detail-row block"><span>缺陷描述</span><p>{{ preview.snapshot.defect_desc || '（空）' }}</p></div>
        <div class="version-detail-row block"><span>制度依据</span><p>{{ preview.snapshot.regulation_basis || '（空）' }}</p></div>
        <div class="version-detail-row block"><span>审计建议</span><p>{{ preview.snapshot.suggestion || '（空）' }}</p></div>
        <div class="version-detail-row"><span>编写人</span><strong>{{ preview.snapshot.author || '（空）' }}</strong></div>
        <div class="version-detail-row"><span>审核人</span><strong>{{ preview.snapshot.reviewer || '（空）' }}</strong></div>
        <p class="version-preview-meta">保存人：{{ preview.saved_by || '未知' }}</p>
      </div>
    </el-dialog>
  </section>
</template>
