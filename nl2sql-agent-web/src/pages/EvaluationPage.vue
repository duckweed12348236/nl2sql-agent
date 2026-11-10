<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import ContextTables from '@/components/ContextTables.vue'
import PageHeader from '@/components/PageHeader.vue'
import TaskPanel from '@/components/TaskPanel.vue'
import {
  createDataset,
  deleteDataset,
  evaluateDataset,
  getDataset,
  listDatasetTables,
  listDatasets,
} from '@/api/evaluation'
import { listDatabases } from '@/api/modeling'
import { useTasks } from '@/composables/useTasks'
import { errorMessage } from '@/plugins/request'
import { renderMarkdown } from '@/utils/markdown'
import { tableRoleLabel, tableRoleTag } from '@/utils/roles'
import type { DatasetBrief, DatasetPayload, ResultItem } from '@/types/dataset'
import type { Database, Table } from '@/types/modeling'

const route = useRoute()
const { settledTick, openPanel } = useTasks()

const datasets = ref<DatasetBrief[]>([])
const loadingDatasets = ref(false)
const keyword = ref('')

const activeId = ref('')
/** 从任务面板跳过来时带上的数据集编号 */
const requestedId = ref(typeof route.query.dataset_id === 'string' ? route.query.dataset_id : '')
const payload = ref<DatasetPayload | null>(null)
const loadingDetail = ref(false)
const deletingId = ref('')
const activeTab = ref(route.query.tab === 'results' ? 'results' : 'items')
const openResults = ref<string[]>([])

const databases = ref<Database[]>([])

/* ---------------- 出题 / 评测：只投递，进度去任务面板看 ---------------- */

const taskOpen = ref(false)
const taskMode = ref<'create' | 'evaluate'>('create')
const submitting = ref(false)
const taskSize = ref(10)
const taskDatabaseId = ref('')
const taskTableIds = ref<string[]>([])
const taskTables = ref<Table[]>([])
const selectedTables = ref<Table[]>([])
const loadingTables = ref(false)

const filtered = computed<DatasetBrief[]>(() => {
  const search = keyword.value.trim().toLowerCase()
  if (!search) return datasets.value
  return datasets.value.filter(
    item => item.name.toLowerCase().includes(search) || item.id.includes(search),
  )
})

const emptyText = computed<string>(() =>
  datasets.value.length ? '没有匹配的数据集' : '还没有数据集',
)

const taskTitle = computed<string>(() =>
  taskMode.value === 'create' ? '新建数据集（出题）' : '评估数据集',
)

const canStart = computed<boolean>(() => {
  if (!taskDatabaseId.value) return false
  if (taskMode.value === 'create') return taskTableIds.value.length > 0
  return Boolean(activeId.value)
})

/** 已选表要留在选项里，否则搜索结果一变，已选项的回显会退化成表编号 */
const tableOptions = computed<Table[]>(() => {
  const merged = new Map<string, Table>()
  for (const item of selectedTables.value) merged.set(item.id, item)
  for (const item of taskTables.value) merged.set(item.id, item)
  return [...merged.values()]
})

async function loadDatabases(): Promise<void> {
  try {
    databases.value = await listDatabases()
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  }
}

async function loadDatasets(): Promise<void> {
  loadingDatasets.value = true
  try {
    datasets.value = await listDatasets()

    // 优先选中路由带上来的编号（任务面板跳转），其次保持当前选中，最后落到第一条
    const preferred = requestedId.value || activeId.value
    requestedId.value = ''
    const target = datasets.value.find(item => item.id === preferred) ?? datasets.value[0]

    if (!target) {
      activeId.value = ''
      payload.value = null
      openResults.value = []
      return
    }

    if (target.id !== activeId.value) {
      activeId.value = target.id
      payload.value = null
      openResults.value = []
      await loadDetail()
    }
  } catch (error) {
    datasets.value = []
    ElMessage.error(errorMessage(error, '数据集列表加载失败'))
  } finally {
    loadingDatasets.value = false
  }
}

async function loadDetail(): Promise<void> {
  if (!activeId.value) return
  loadingDetail.value = true
  try {
    const data = await getDataset(activeId.value)
    payload.value = data
    openResults.value = data.results[0] ? [data.results[0].id] : []
  } catch (error) {
    payload.value = null
    ElMessage.error(errorMessage(error, '数据集详情加载失败'))
  } finally {
    loadingDetail.value = false
  }
}

async function selectDataset(item: DatasetBrief): Promise<void> {
  activeId.value = item.id
  payload.value = null
  activeTab.value = 'items'
  await loadDetail()
}

async function removeDataset(dataset: { id: string; name: string }): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '条目与历次评测结果会随外键级联删除；检索侧的会话记录属于另一条业务线，不受影响。',
      `删除数据集「${dataset.name}」`,
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }

  deletingId.value = dataset.id
  try {
    await deleteDataset(dataset.id)
    ElMessage.success('数据集已删除')
    if (activeId.value === dataset.id) {
      activeId.value = ''
      payload.value = null
    }
    await loadDatasets()
  } catch (error) {
    ElMessage.error(errorMessage(error, '数据集删除失败'))
  } finally {
    deletingId.value = ''
  }
}

/** 平均等效率按百分比读，配色随档位 */
function meanLabel(mean: number | null): string {
  if (mean === null || Number.isNaN(mean)) return '未评分'
  return `${(mean * 100).toFixed(1)}%`
}

function meanTag(mean: number | null): string {
  if (mean === null || Number.isNaN(mean)) return 'tag--neutral'
  if (mean >= 0.8) return 'tag--mint'
  if (mean >= 0.5) return 'tag--peach'
  return 'tag--danger'
}

function roundLabel(index: number): string {
  const total = payload.value?.results.length ?? 0
  return index === 0 ? '最新一轮' : `第 ${total - index} 轮`
}

function itemScoreLabel(item: ResultItem): string {
  if (!item.answer) return '未生成'
  return item.score >= 1 ? '等效' : '不等效'
}

function itemScoreTag(item: ResultItem): string {
  if (!item.answer) return 'tag--neutral'
  return item.score >= 1 ? 'tag--mint' : 'tag--danger'
}

/** 按业务库拉取可选表，keyword 为表名关键字（服务端子串匹配） */
async function loadTaskTables(keyword = ''): Promise<void> {
  if (!taskDatabaseId.value) {
    taskTables.value = []
    return
  }

  loadingTables.value = true
  try {
    taskTables.value = await listDatasetTables(taskDatabaseId.value, keyword)
  } catch (error) {
    taskTables.value = []
    ElMessage.error(errorMessage(error, '可选表加载失败'))
  } finally {
    loadingTables.value = false
  }
}

async function onTaskDatabaseChange(): Promise<void> {
  taskTableIds.value = []
  selectedTables.value = []
  await loadTaskTables('')
}

function onTableChange(value: string[]): void {
  const known = new Map<string, Table>()
  for (const item of [...selectedTables.value, ...taskTables.value]) known.set(item.id, item)

  selectedTables.value = value
    .map(id => known.get(id))
    .filter((item): item is Table => Boolean(item))
}

function openCreate(): void {
  taskMode.value = 'create'
  taskSize.value = 10
  taskTableIds.value = []
  selectedTables.value = []
  taskTables.value = []
  taskDatabaseId.value = databases.value[0]?.id ?? ''
  taskOpen.value = true
  void loadTaskTables('')
}

/** 出题所用的业务库：表编号形如 `<库编号>.<表名>`，取首张表的库编号即可 */
function datasetDatabaseId(): string {
  const first = payload.value?.dataset.reference_contexts[0]?.id
  if (!first) return ''
  const index = first.indexOf('.')
  return index > 0 ? first.slice(0, index) : ''
}

function openEvaluate(): void {
  if (!payload.value) return
  taskMode.value = 'evaluate'
  taskDatabaseId.value = datasetDatabaseId() || databases.value[0]?.id || ''
  taskOpen.value = true
}

/** 任务收尾后把数据集列表与当前详情刷一次，不用手动点 */
async function reloadAfterTasks(): Promise<void> {
  const keep = activeId.value
  await loadDatasets()
  // loadDatasets 只在原选中项不在列表里时才改动 activeId，那种情况它已经把详情刷过了
  if (keep && activeId.value === keep) await loadDetail()
}

/** 投递即返回，不等后台跑完：进度去任务面板看 */
async function submitTask(): Promise<void> {
  if (!taskDatabaseId.value) {
    ElMessage.warning('请先选择业务库')
    return
  }
  if (taskMode.value === 'create' && !taskTableIds.value.length) {
    ElMessage.warning('请至少选择一张出题所用的表')
    return
  }
  if (taskMode.value === 'evaluate' && !activeId.value) {
    ElMessage.warning('请先选择数据集')
    return
  }

  submitting.value = true
  try {
    if (taskMode.value === 'create') {
      await createDataset({
        table_ids: [...taskTableIds.value],
        size: taskSize.value,
        database_id: taskDatabaseId.value,
      })
      ElMessage.success('出题任务已投递，可以在任务面板里看进度')
    } else {
      await evaluateDataset({
        dataset_id: activeId.value,
        database_id: taskDatabaseId.value,
      })
      ElMessage.success('评测任务已投递，可以在任务面板里看进度')
    }

    taskOpen.value = false
    openPanel()
  } catch (error) {
    // 投递本身失败（参数校验 422、表 / 数据集不存在 404）
    ElMessage.error(errorMessage(error, '任务投递失败'))
  } finally {
    submitting.value = false
  }
}

// 任务收尾时自动刷新列表与详情
watch(settledTick, () => {
  void reloadAfterTasks()
})

// 任务面板跳转过来（同一个路由带不同 query）时切换选中项
watch(
  () => route.query.dataset_id,
  async value => {
    if (typeof value !== 'string' || !value || value === activeId.value) return
    requestedId.value = value
    activeTab.value = route.query.tab === 'results' ? 'results' : 'items'
    await loadDatasets()
  },
)

onMounted(async () => {
  await Promise.all([loadDatabases(), loadDatasets()])
})
</script>

<template>
  <div class="page">
    <PageHeader
      title="数据集"
      description="一轮出题（指定表 → 生成问题与 SQL → 校验纠正 → 落库）之后，按数据集编号在指定业务库上跑一轮评测（逐条答题 → 判定等效性 → 写解读报告）；每轮评测各落一条结果，可以反复评估。出题与评测都是后台任务：投递即返回，不用等；进度都收在「任务」面板里，一次看最近的一批任务。"
    >
      <template #actions>
        <TaskPanel />
        <el-button :loading="loadingDatasets" @click="loadDatasets">
          <AppIcon name="refresh" :size="16" />
          <span>刷新</span>
        </el-button>
        <el-button type="primary" :disabled="!databases.length" @click="openCreate">
          <AppIcon name="plus" :size="16" />
          <span>新建数据集</span>
        </el-button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <el-input v-model="keyword" class="search" placeholder="按名称或编号搜索" clearable>
        <template #prefix>
          <AppIcon name="search" :size="16" />
        </template>
      </el-input>
      <span class="muted-12">共 {{ datasets.length }} 个数据集</span>
      <span v-if="!databases.length" class="muted-12">· 未取到业务库，出题与评测暂不可用</span>
    </div>

    <div class="split">
      <aside class="split__side">
        <div class="split__side-head">
          <span class="split__side-title">数据集</span>
          <span class="muted-12">{{ filtered.length }}</span>
        </div>
        <div v-loading="loadingDatasets" class="table-list">
          <button
            v-for="item in filtered"
            :key="item.id"
            type="button"
            class="table-item"
            :class="{ 'table-item--active': item.id === activeId }"
            @click="selectDataset(item)"
          >
            <span class="table-item__name">{{ item.name }}</span>
          </button>
          <p v-if="!filtered.length && !loadingDatasets" class="table-list__empty muted-12">
            {{ emptyText }}
          </p>
        </div>
      </aside>

      <section v-loading="loadingDetail" class="split__main">
        <template v-if="payload">
          <div class="detail__head">
            <div class="detail__meta">
              <h2 class="detail__title">{{ payload.dataset.name }}</h2>
              <p class="mono detail__line" :title="payload.dataset.id">
                编号 {{ payload.dataset.id }}
              </p>
              <p class="mono detail__line" :title="payload.dataset.thread_id ?? ''">
                来源会话 {{ payload.dataset.thread_id ?? '—' }}（仅作标记）
              </p>
            </div>
            <div class="detail__actions">
              <el-button :loading="loadingDetail" @click="loadDetail">
                <AppIcon name="refresh" :size="16" />
                <span>刷新</span>
              </el-button>
              <el-button type="primary" :disabled="!databases.length" @click="openEvaluate">
                <AppIcon name="play" :size="16" />
                <span>评估数据集</span>
              </el-button>
              <el-button
                plain
                type="danger"
                :loading="deletingId === payload.dataset.id"
                @click="removeDataset(payload.dataset)"
              >
                <AppIcon name="trash" :size="16" />
                <span>删除</span>
              </el-button>
            </div>
          </div>

          <p v-if="payload.dataset.error" class="alert alert--danger">
            出题失败：{{ payload.dataset.error }}
          </p>

          <el-tabs v-model="activeTab" class="detail__tabs">
            <el-tab-pane :label="`条目 ${payload.items.length}`" name="items">
              <div class="table-wrap">
                <el-table :data="payload.items" row-key="id">
                  <el-table-column type="index" label="#" width="56" />
                  <el-table-column label="问题" min-width="240" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.question }}</template>
                  </el-table-column>
                  <el-table-column label="参考 SQL" min-width="320">
                    <template #default="{ row }">
                      <code class="sql">{{ row.reference }}</code>
                    </template>
                  </el-table-column>
                  <template #empty>
                    <el-empty class="empty-cell" description="这条数据集没有条目" :image-size="96" />
                  </template>
                </el-table>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`评测结果 ${payload.results.length}`" name="results">
              <el-empty
                v-if="!payload.results.length"
                class="empty-cell"
                description="还没有评测结果，点右上角「评估数据集」跑一轮"
                :image-size="96"
              />

              <el-collapse v-else v-model="openResults">
                <el-collapse-item
                  v-for="(result, index) in payload.results"
                  :key="result.id"
                  :name="result.id"
                >
                  <template #title>
                    <div class="row-head">
                      <span class="tag" :class="meanTag(result.mean)">
                        {{ meanLabel(result.mean) }}
                      </span>
                      <span class="cell-name">{{ roundLabel(index) }}</span>
                      <span class="muted-12">{{ result.items.length }} 条</span>
                      <span class="mono">{{ result.id }}</span>
                    </div>
                  </template>

                  <p v-if="result.error" class="alert alert--danger">整轮失败：{{ result.error }}</p>

                  <div v-if="result.report" class="md" v-html="renderMarkdown(result.report)" />
                  <p v-else class="muted-12">这一轮没有解读报告。</p>

                  <el-table :data="result.items" row-key="id" size="small">
                    <el-table-column label="参考 SQL" min-width="240">
                      <template #default="{ row }">
                        <code class="sql">{{ row.reference }}</code>
                      </template>
                    </el-table-column>
                    <el-table-column label="生成 SQL" min-width="240">
                      <template #default="{ row }">
                        <code v-if="row.answer" class="sql">{{ row.answer }}</code>
                        <span v-else class="muted">未生成</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="判定" width="90">
                      <template #default="{ row }">
                        <span class="tag" :class="itemScoreTag(row)">
                          {{ itemScoreLabel(row) }}
                        </span>
                      </template>
                    </el-table-column>
                    <el-table-column label="理由" min-width="200" show-overflow-tooltip>
                      <template #default="{ row }">
                        <span v-if="row.reason">{{ row.reason }}</span>
                        <span v-else class="muted">—</span>
                      </template>
                    </el-table-column>
                  </el-table>

                  <div class="sub-section">
                    <div class="sub-section__head">
                      <span class="sub-section__title">本轮检索到的表上下文</span>
                      <span class="muted-12">{{ result.retrieved_contexts?.length ?? 0 }} 条</span>
                    </div>
                    <ContextTables :contexts="result.retrieved_contexts" numbered />
                  </div>
                </el-collapse-item>
              </el-collapse>
            </el-tab-pane>

            <el-tab-pane
              :label="`参考上下文 ${payload.dataset.reference_contexts.length}`"
              name="contexts"
            >
              <p class="muted-12 tab-hint">
                出题与判定所用的库上下文（表 / 字段 / 取值），由新建数据集时指定的表一并落库。
              </p>
              <ContextTables :contexts="payload.dataset.reference_contexts" default-open />
            </el-tab-pane>
          </el-tabs>
        </template>

        <el-empty v-else class="empty-state" description="从左侧选择数据集" :image-size="112" />
      </section>
    </div>

    <el-dialog
      v-model="taskOpen"
      :title="taskTitle"
      width="640px"
      :close-on-click-modal="false"
    >
      <div class="form-stack">
        <label class="field">
          <span class="field__label">
            {{ taskMode === 'create' ? '业务库（出题取材）' : '业务库（检索智能体在该库答题）' }}
          </span>
          <el-select
            v-model="taskDatabaseId"
            placeholder="选择业务库"
            filterable
            @change="onTaskDatabaseChange"
          >
            <el-option
              v-for="item in databases"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
        </label>

        <label v-if="taskMode === 'create'" class="field">
          <span class="field__label">出题所用的表（至少 1 张，可多选）</span>
          <el-select
            v-model="taskTableIds"
            multiple
            filterable
            remote
            reserve-keyword
            :remote-method="loadTaskTables"
            :loading="loadingTables"
            :disabled="!taskDatabaseId"
            placeholder="按表名搜索后选择"
            @change="onTableChange"
          >
            <el-option v-for="item in tableOptions" :key="item.id" :label="item.name" :value="item.id">
              <div class="option-row">
                <span class="cell-name">{{ item.name }}</span>
                <span class="tag" :class="tableRoleTag(item.role)">
                  {{ tableRoleLabel(item.role) }}
                </span>
              </div>
            </el-option>
          </el-select>
          <span class="muted-12">已选 {{ taskTableIds.length }} 张表</span>
        </label>

        <label v-if="taskMode === 'create'" class="field">
          <span class="field__label">问题数量</span>
          <el-input-number v-model="taskSize" :min="1" :precision="0" :step="1" />
        </label>

        <p class="muted-12">
          {{
            taskMode === 'create'
              ? '一轮出题会跑多次大模型请求（生成问题与 SQL、校验纠正），耗时较长；投递后立即返回、不用在这里等，进度去「任务」面板看，跑完停在落库、不在这一步评测。'
              : '按数据集编号取出问题、参考 SQL 与库上下文，在该业务库上逐条答题并判等效性；每投递一次就是一个独立任务、各落一条结果，因此可以反复评估，出题所用的表不用再选。投递后立即返回，进度去「任务」面板看。'
          }}
        </p>
      </div>

      <template #footer>
        <div class="dialog-foot">
          <el-button @click="taskOpen = false">取消</el-button>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!canStart"
            @click="submitTask"
          >
            {{ taskMode === 'create' ? '投递出题任务' : '投递评测任务' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.split {
  display: grid;
  flex: 1;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 24px;
}

.split__side {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.split__side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px 12px;
  border-bottom: 1px solid var(--hairline);
}

.split__side-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
}

.split__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.table-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 120px;
  max-height: 60vh;
  overflow-y: auto;
  padding-top: 8px;
}

.table-item {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 36px;
  padding: 0 12px;
  border: none;
  border-radius: var(--radius-lg);
  background: transparent;
  color: var(--text);
  font-family: var(--font-body);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition:
    background-color 140ms ease,
    color 140ms ease;
}

.table-item:hover {
  background: var(--surface);
}

.table-item--active {
  background: var(--tint-lavender);
  color: var(--accent);
  font-weight: 500;
}

.table-item__name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.table-list__empty {
  flex: none;
  margin: 0;
  padding: 12px;
}

.detail__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.detail__meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.detail__title {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.01em;
}

.detail__line {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.detail__tabs {
  margin-top: 24px;
}

.tab-hint {
  margin: 0 0 12px;
}

.row-head {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  overflow: hidden;
}

.sub-section {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--hairline);
}

.sub-section__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.sub-section__title {
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
}

/* 解读报告是 Markdown 正文，按 api_docs.md 给定的受限子集渲染 */
.md {
  margin: 0 0 16px;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text);
}

.md :deep(h4),
.md :deep(h5),
.md :deep(h6) {
  margin: 16px 0 8px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
}

.md :deep(p) {
  margin: 0 0 8px;
}

.md :deep(ul),
.md :deep(ol) {
  margin: 0 0 8px;
  padding-left: 20px;
}

.md :deep(li) {
  margin: 2px 0;
}

.md :deep(strong) {
  font-weight: 600;
  color: var(--ink);
}

.md :deep(code) {
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--ink);
}

.sql {
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.5;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.alert {
  display: block;
  margin: 0 0 16px;
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  font-size: 13px;
}

.alert--danger {
  background: #fdeaea;
  color: var(--danger);
}

.option-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 1024px) {
  .split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
