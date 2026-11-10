<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import { tableRoleLabel, tableRoleTag } from '@/utils/roles'
import { renderMarkdown } from '@/utils/markdown'
import type { RetrievalMetric, TurnRecord } from '@/types/retrieval'

/**
 * 一轮问答：问题 → 模型消息（可展开）→ 反问 / 失败 → 指标口径、选中的表 → SQL 与结果 → Token / 耗时。
 * 帧里没有节点状态，状态由「是否有反问 / 失败 / 是否还在跑」推断；
 * 以反问收尾的那一轮会带上澄清输入框，提交后由页面按同一会话续跑。
 */

const props = defineProps<{
  turn: TurnRecord
  /** 轮次序号（同一会话内从 1 开始） */
  round?: number
  /** 该轮以反问收尾、正在等用户补充 */
  pending?: boolean
  /** 这一轮还在跑 */
  running?: boolean
}>()

const emit = defineEmits<{ answer: [text: string] }>()

/** 待补充时的回答输入 */
const reply = ref('')
const showMessages = ref(Boolean(props.running))

const rows = computed<Record<string, unknown>[]>(() => {
  const data = props.turn.data
  if (!Array.isArray(data) || !data.length) return []
  if (!data.every(item => item !== null && typeof item === 'object' && !Array.isArray(item))) {
    return []
  }
  return data as Record<string, unknown>[]
})

const columns = computed<string[]>(() => {
  const first = rows.value[0]
  return first ? Object.keys(first) : []
})

/** 不是「一行行记录」时按 JSON 原样展示 */
const rawData = computed<string>(() => {
  const data = props.turn.data
  if (data === null || data === undefined) return ''
  if (rows.value.length) return ''
  return JSON.stringify(data, null, 2) ?? String(data)
})

const statusLabel = computed<string>(() => {
  if (props.running) return '进行中'
  if (props.turn.clarification) return '待澄清'
  if (props.turn.failure) return '失败'
  return '完成'
})

const statusTag = computed<string>(() => {
  if (props.running) return 'tag--sky'
  if (props.turn.clarification) return 'tag--peach'
  if (props.turn.failure) return 'tag--danger'
  return 'tag--mint'
})

const startAt = computed<string>(() => {
  const date = new Date(props.turn.started_at)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
})

const tokenHint = computed<string>(() => {
  const token = props.turn.token
  if (!token) return ''
  if (!token.prompt && !token.completion) return `本轮共 ${formatNumber(token.total)} Token`
  return `prompt ${formatNumber(token.prompt)} · completion ${formatNumber(token.completion)}`
})

const footerLabel = computed<string>(() => {
  const parts: string[] = []
  const token = props.turn.token
  if (token) parts.push(`Token ${formatNumber(token.total)}`)
  if (props.turn.elapsed) parts.push(`耗时 ${props.turn.elapsed.toFixed(2)}s`)
  return parts.join(' · ')
})

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value) ?? ''
  return String(value)
}

function metricHint(metric: RetrievalMetric): string {
  const alias = metric.alias?.length ? `别名：${metric.alias.join('、')}` : ''
  return [metric.description, alias].filter(Boolean).join('；')
}

function toggleMessages(): void {
  showMessages.value = !showMessages.value
}

function submitReply(): void {
  const text = reply.value.trim()
  if (!text || props.running) return
  emit('answer', text)
  reply.value = ''
}

async function copySql(): Promise<void> {
  if (!props.turn.sql) return
  try {
    await navigator.clipboard.writeText(props.turn.sql)
    ElMessage.success('SQL 已复制')
  } catch {
    ElMessage.warning('当前浏览器不允许访问剪贴板')
  }
}
</script>

<template>
  <article class="turn" :class="{ 'turn--pending': pending, 'turn--live': running }">
    <header class="turn__head">
      <span class="turn__mark">Q</span>
      <p class="turn__question">{{ turn.content }}</p>
      <span v-if="round" class="muted-12 turn__round">#{{ round }}</span>
      <span class="tag" :class="statusTag">{{ statusLabel }}</span>
      <span class="muted-12 turn__time">{{ startAt }}</span>
    </header>

    <p v-if="turn.failure" class="alert alert--danger">{{ turn.failure }}</p>

    <div v-if="turn.clarification" class="clarify">
      <div class="clarify__head">
        <span class="tag tag--peach">反问</span>
        <span class="muted-12">
          {{ pending ? '补充后按同一会话继续' : '这一轮停在这里，可在下方接着提问' }}
        </span>
      </div>

      <p class="clarify__text" v-html="renderMarkdown(turn.clarification)" />

      <template v-if="pending">
        <el-input
          v-model="reply"
          type="textarea"
          :rows="2"
          resize="none"
          :disabled="running"
          placeholder="回答上面的反问，如：口径按已支付订单算，销售额扣退款"
          @keydown.ctrl.enter="submitReply"
          @keydown.meta.enter="submitReply"
        />
        <div class="clarify__foot">
          <span class="muted-12">Ctrl / ⌘ + Enter 提交</span>
          <el-button type="primary" :disabled="!reply.trim() || running" @click="submitReply">
            提交澄清
          </el-button>
        </div>
      </template>
    </div>

    <div class="turn__messages">
      <el-button link type="primary" :disabled="!turn.messages.length" @click="toggleMessages">
        {{ showMessages ? '收起过程' : `展开过程（${turn.messages.length} 条）` }}
      </el-button>
      <ol v-if="showMessages && turn.messages.length" class="messages">
        <li v-for="(text, index) in turn.messages" :key="index" class="messages__item">
          <div class="messages__text" v-html="renderMarkdown(text)" />
        </li>
      </ol>

      <div v-if="running" class="loading">
        <span class="loading__dots">
          <span class="loading__dot" />
          <span class="loading__dot" />
          <span class="loading__dot" />
        </span>
        <span class="muted-12">
          {{ turn.messages.length ? '子智能体正在处理…' : '正在调度子智能体…' }}
        </span>
      </div>
    </div>

    <div v-if="turn.metrics.length" class="block">
      <span class="sub-head__title">指标口径</span>
      <div class="tag-list">
        <span
          v-for="metric in turn.metrics"
          :key="metric.name"
          class="tag tag--mint"
          :title="metricHint(metric)"
        >
          {{ metric.name }}
        </span>
      </div>
    </div>

    <div v-if="turn.tables.length" class="block">
      <span class="sub-head__title">选中的表</span>
      <ul class="table-list">
        <li v-for="table in turn.tables" :key="table.id" class="table-line">
          <span class="cell-name">{{ table.name }}</span>
          <span class="tag" :class="tableRoleTag(table.role)">{{ tableRoleLabel(table.role) }}</span>
          <span v-if="table.description" class="muted-12 table-line__desc">
            {{ table.description }}
          </span>
        </li>
      </ul>
    </div>

    <template v-if="turn.sql">
      <div class="sub-head">
        <span class="sub-head__title">SQL</span>
        <el-button link type="primary" @click="copySql">
          <AppIcon name="copy" :size="14" />
          <span>复制</span>
        </el-button>
      </div>
      <code class="sql">{{ turn.sql }}</code>
    </template>

    <template v-if="rows.length || rawData">
      <div class="sub-head">
        <span class="sub-head__title">查询结果</span>
        <span v-if="rows.length" class="muted-12">{{ rows.length }} 行</span>
      </div>

      <div v-if="rows.length" class="table-wrap">
        <el-table :data="rows" max-height="360" size="small">
          <el-table-column type="index" label="#" width="56" />
          <el-table-column
            v-for="column in columns"
            :key="column"
            :label="column"
            min-width="140"
            show-overflow-tooltip
          >
            <template #default="{ row }">
              <span class="mono">{{ formatCell(row[column]) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <pre v-else class="raw">{{ rawData }}</pre>
    </template>

    <footer v-if="footerLabel" class="turn__foot">
      <span class="muted-12" :title="tokenHint">{{ footerLabel }}</span>
    </footer>
  </article>
</template>

<style scoped>
.turn {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 20px 0;
  border-bottom: 1px solid var(--hairline);
}

.turn:last-child {
  border-bottom: none;
}

.turn__head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}

.turn__mark {
  display: grid;
  flex: none;
  place-items: center;
  width: 22px;
  height: 22px;
  margin-top: 1px;
  border-radius: var(--radius-md);
  background: var(--tint-lavender);
  color: var(--accent);
  font-family: var(--font-mono), monospace;
  font-size: 11px;
  font-weight: 600;
}

.turn__question {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.turn__round,
.turn__time {
  flex: none;
}

/* 反问：用警告色块区分「不是失败，也不是结果」 */
.clarify {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface-soft);
}

.clarify__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.clarify__text {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
  color: var(--ink);
}

.clarify__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

/* 过程：模型消息按顺序排开，用序号与细线区分 */
.turn__messages {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.messages {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  margin: 0;
  padding: 0 0 0 18px;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text);
}

.messages__item {
  padding-left: 2px;
  overflow-wrap: anywhere;
}

.messages__item::marker {
  color: var(--muted);
  font-family: var(--font-mono), monospace;
  font-size: 11px;
}

/* 模型消息是 markdown 文本：渲染后的常见元素贴一眼舒适的排版 */
.messages__text :deep(p) {
  margin: 0 0 6px;
}

.messages__text :deep(p:last-child) {
  margin-bottom: 0;
}

.messages__text :deep(ul),
.messages__text :deep(ol) {
  margin: 4px 0 6px;
  padding-left: 18px;
}

.messages__text :deep(li) {
  margin: 2px 0;
}

.messages__text :deep(h1),
.messages__text :deep(h2),
.messages__text :deep(h3),
.messages__text :deep(h4) {
  margin: 8px 0 4px;
  font-size: 14px;
  font-weight: 600;
}

.messages__text :deep(code) {
  padding: 1px 5px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-family: var(--font-mono), monospace;
  font-size: 12px;
}

.messages__text :deep(pre) {
  margin: 6px 0;
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow-x: auto;
}

.messages__text :deep(pre code) {
  padding: 0;
  background: none;
}

.messages__text :deep(a) {
  color: var(--accent);
}

.messages__text :deep(blockquote) {
  margin: 6px 0;
  padding-left: 10px;
  border-left: 3px solid var(--hairline);
  color: var(--muted);
}

/* 查询过程加载态：三个圆点起伏 */
.loading {
  display: flex;
  align-items: center;
  gap: 10px;
}

.loading__dots {
  display: inline-flex;
  gap: 4px;
  align-items: center;
}

.loading__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  animation: loading-bounce 1.2s ease-in-out infinite;
}

.loading__dot:nth-child(2) {
  animation-delay: 0.15s;
}

.loading__dot:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes loading-bounce {
  0%,
  80%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }

  40% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .loading__dot {
    animation: none;
    opacity: 0.6;
  }
}

.block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sub-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}

.sub-head__title {
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
}

.table-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.table-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.table-line__desc {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sql {
  display: block;
  padding: 12px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  font-family: var(--font-mono), monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.raw {
  margin: 0;
  padding: 12px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  font-family: var(--font-mono), monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.alert {
  display: block;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  font-size: 13px;
}

.alert--danger {
  background: #fdeaea;
  color: var(--danger);
}

.turn__foot {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
