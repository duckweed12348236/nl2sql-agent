<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import {
  deleteTable,
  deleteTables,
  listColumns,
  listDatabases,
  listTables,
  updateColumn,
  updateTable,
} from '@/api/modeling'
import { errorMessage } from '@/plugins/request'
import type {
  Column,
  ColumnRole,
  ColumnUpdate,
  Database,
  Table,
  TableRole,
  TableUpdate,
} from '@/types/modeling'
import { columnRoleLabel, columnRoleTag, tableRoleLabel, tableRoleTag } from '@/utils/roles'

interface TableDraft {
  role: TableRole | ''
  description: string
}

interface ColumnDraft {
  role: ColumnRole | ''
  description: string
  alias: string[]
}

const route = useRoute()
const router = useRouter()

const databases = ref<Database[]>([])
const loadingDatabases = ref(false)
const activeDatabaseId = ref('')

const tables = ref<Table[]>([])
const loadingTables = ref(false)
const activeTableId = ref('')
/** 按表名搜索：交给服务端子串匹配，不在前端过滤 */
const tableKeyword = ref('')
const requestedTableId = ref(
  typeof route.query.table_id === 'string' ? route.query.table_id : '',
)

const columns = ref<Column[]>([])
const loadingColumns = ref(false)

const tableDraft = reactive<TableDraft>({ role: '', description: '' })
const savingTable = ref(false)
const deletingTable = ref(false)

/** 批量删除：勾选的表编号（一次最多 200 张，接口上限） */
const selectedTableIds = ref<string[]>([])
const deletingBatch = ref(false)

const drawerOpen = ref(false)
const editingColumnId = ref('')
const savingColumn = ref(false)
const columnDraft = reactive<ColumnDraft>({
  role: '',
  description: '',
  alias: [],
})

const activeTable = computed<Table | null>(
  () => tables.value.find(item => item.id === activeTableId.value) ?? null,
)

const draftColumn = computed<Column | null>(
  () => columns.value.find(item => item.id === editingColumnId.value) ?? null,
)

const tableDirty = computed<boolean>(() => {
  const origin = activeTable.value
  if (!origin) return false
  return Object.keys(buildTableUpdate(origin, tableDraft)).length > 0
})

const columnDirty = computed<boolean>(() => {
  const origin = draftColumn.value
  if (!origin || !drawerOpen.value) return false
  return Object.keys(buildColumnUpdate(origin, columnDraft)).length > 0
})

function sameAlias(a: string[] | null, b: string[] | null): boolean {
  if (a === null || b === null) return a === b
  return a.length === b.length && a.every((item, index) => item === b[index])
}

/** 只提交发生变化的字段：未提交保持原值，显式 null 表示清空 */
function buildTableUpdate(origin: Table, draft: TableDraft): TableUpdate {
  const payload: TableUpdate = {}

  const role = draft.role || null
  if (role !== (origin.role ?? null)) payload.role = role

  const description = draft.description.trim() || null
  if (description !== (origin.description ?? null)) payload.description = description

  return payload
}

/** 字段可改的只有角色、描述与别名；类型与取值采集是采集口径的登记，不提交 */
function buildColumnUpdate(origin: Column, draft: ColumnDraft): ColumnUpdate {
  const payload: ColumnUpdate = {}

  const role = draft.role || null
  if (role !== (origin.role ?? null)) payload.role = role

  const description = draft.description.trim() || null
  if (description !== (origin.description ?? null)) payload.description = description

  const alias = draft.alias.length ? draft.alias : null
  if (!sameAlias(alias, origin.alias ?? null)) payload.alias = alias

  return payload
}

function resetTableDraft(table: Table): void {
  tableDraft.role = table.role ?? ''
  tableDraft.description = table.description ?? ''
}

function syncQuery(): void {
  router.replace({
    name: 'table',
    query: {
      database_id: activeDatabaseId.value || undefined,
      table_id: activeTableId.value || undefined,
    },
  })
}

function replaceColumn(updated: Column): void {
  const index = columns.value.findIndex(item => item.id === updated.id)
  if (index >= 0) columns.value.splice(index, 1, updated)
}

async function loadDatabases(): Promise<void> {
  loadingDatabases.value = true
  try {
    databases.value = await listDatabases()

    const requested = typeof route.query.database_id === 'string' ? route.query.database_id : ''
    const target = databases.value.find(item => item.id === requested) ?? databases.value[0]

    if (target) {
      await selectDatabase(target.id)
    } else {
      activeDatabaseId.value = ''
      activeTableId.value = ''
      tables.value = []
      columns.value = []
    }
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  } finally {
    loadingDatabases.value = false
  }
}

async function selectDatabase(id: string): Promise<void> {
  if (!id) return
  activeDatabaseId.value = id
  activeTableId.value = ''
  columns.value = []
  requestedTableId.value = ''
  tableKeyword.value = ''
  selectedTableIds.value = []
  syncQuery()
  await loadTables()
}

let searchTimer: ReturnType<typeof setTimeout> | null = null

/** 表名搜索：服务端子串匹配，输入停顿后再查 */
function onTableKeyword(): void {
  if (searchTimer !== null) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchTimer = null
    void loadTables()
  }, 300)
}

async function loadTables(): Promise<void> {
  if (!activeDatabaseId.value) return
  loadingTables.value = true
  try {
    tables.value = await listTables(
      activeDatabaseId.value,
      tableKeyword.value.trim() || undefined,
    )

    const preferred = requestedTableId.value || activeTableId.value
    requestedTableId.value = ''
    const target = tables.value.find(item => item.id === preferred) ?? tables.value[0]

    if (target && target.id === activeTableId.value) {
      // 当前选中的表仍在结果里：只刷新草稿，不重拉字段
      resetTableDraft(target)
    } else if (target) {
      selectTable(target)
    } else {
      activeTableId.value = ''
      columns.value = []
      syncQuery()
    }
  } catch (error) {
    tables.value = []
    activeTableId.value = ''
    columns.value = []
    ElMessage.error(errorMessage(error, '表列表加载失败'))
  } finally {
    loadingTables.value = false
    // 列表变了，勾选里的表若已不在结果中就摘掉
    selectedTableIds.value = selectedTableIds.value.filter(id =>
      tables.value.some(item => item.id === id),
    )
  }
}

function selectTable(table: Table): void {
  activeTableId.value = table.id
  resetTableDraft(table)
  syncQuery()
  void loadColumns(table.id)
}

async function loadColumns(table_id: string): Promise<void> {
  loadingColumns.value = true
  try {
    columns.value = await listColumns(table_id)
  } catch (error) {
    columns.value = []
    ElMessage.error(errorMessage(error, '字段列表加载失败'))
  } finally {
    loadingColumns.value = false
  }
}

async function reload(): Promise<void> {
  if (!databases.value.length) {
    await loadDatabases()
    return
  }
  requestedTableId.value = activeTableId.value
  await loadTables()
}

async function saveTable(): Promise<void> {
  const origin = activeTable.value
  if (!origin) return

  const payload = buildTableUpdate(origin, tableDraft)
  if (!Object.keys(payload).length) {
    ElMessage.info('没有可修改的字段')
    return
  }

  savingTable.value = true
  try {
    const updated = await updateTable(origin.id, payload)
    const index = tables.value.findIndex(item => item.id === updated.id)
    if (index >= 0) tables.value.splice(index, 1, updated)
    resetTableDraft(updated)
    ElMessage.success('表已更新')
  } catch (error) {
    ElMessage.error(errorMessage(error, '表更新失败'))
  } finally {
    savingTable.value = false
  }
}

/** 删的是登记与向量，业务库里的表本身不动 */
async function removeTable(): Promise<void> {
  const origin = activeTable.value
  if (!origin) return

  try {
    await ElMessageBox.confirm(
      '表下的字段、字段间的关联与向量库里的字段点 / 取值点会一起清掉；业务库里的表本身不动，服务端会把表名自动写进该库的采集黑名单，避免下一轮巡检 / 采集又把它当新增表采回来。要恢复采集，去「业务库 → 采集黑名单」把表名去掉。',
      `删除表「${origin.name}」`,
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

  deletingTable.value = true
  try {
    await deleteTable(origin.id)
    ElMessage.success('表已删除')
    activeTableId.value = ''
    columns.value = []
    requestedTableId.value = ''
    selectedTableIds.value = selectedTableIds.value.filter(id => id !== origin.id)
    await loadTables()
  } catch (error) {
    ElMessage.error(errorMessage(error, '表删除失败'))
  } finally {
    deletingTable.value = false
  }
}

/** 批量删除：勾选 / 取消勾选一张表，超过接口上限（200）时不再接受 */
function toggleTable(table_id: string, checked: boolean): void {
  if (!checked) {
    selectedTableIds.value = selectedTableIds.value.filter(id => id !== table_id)
    return
  }

  if (selectedTableIds.value.length >= 200) {
    ElMessage.warning('一次最多删除 200 张表')
    return
  }

  if (!selectedTableIds.value.includes(table_id)) {
    selectedTableIds.value = [...selectedTableIds.value, table_id]
  }
}

function clearSelection(): void {
  selectedTableIds.value = []
}

/** 批量删除：查不到的编号不算失败，按 `deleted` / `missing` 分别提示 */
async function removeSelectedTables(): Promise<void> {
  const ids = [...selectedTableIds.value]
  if (!ids.length) return

  try {
    await ElMessageBox.confirm(
      `将删除 ${ids.length} 张表的登记：表下字段、字段间关联与向量库里的字段点 / 取值点会一起清掉。业务库里的表本身不动，这批表名会被自动写进各自业务库的采集黑名单，巡检与采集都会跳过它们；要恢复采集，去「业务库 → 采集黑名单」把表名去掉。`,
      `批量删除 ${ids.length} 张表`,
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

  deletingBatch.value = true
  try {
    const result = await deleteTables(ids)
    const deleted = new Set(result.deleted)

    if (deleted.has(activeTableId.value)) {
      activeTableId.value = ''
      columns.value = []
    }
    selectedTableIds.value = selectedTableIds.value.filter(id => !deleted.has(id))
    requestedTableId.value = ''

    if (result.missing.length) {
      ElMessage.warning(
        `已删除 ${result.deleted.length} 张表，另有 ${result.missing.length} 张在元数据库里查不到（可能已被删过）`,
      )
    } else {
      ElMessage.success(`已删除 ${result.deleted.length} 张表`)
    }

    await loadTables()
  } catch (error) {
    ElMessage.error(errorMessage(error, '批量删除表失败'))
  } finally {
    deletingBatch.value = false
  }
}

function openColumn(row: Column): void {
  editingColumnId.value = row.id
  columnDraft.role = row.role ?? ''
  columnDraft.description = row.description ?? ''
  columnDraft.alias = [...(row.alias ?? [])]
  drawerOpen.value = true
}

async function saveColumn(): Promise<void> {
  const origin = draftColumn.value
  if (!origin) return

  const payload = buildColumnUpdate(origin, columnDraft)
  if (!Object.keys(payload).length) {
    ElMessage.info('没有可修改的字段')
    return
  }

  savingColumn.value = true
  try {
    const updated = await updateColumn(origin.id, payload)
    replaceColumn(updated)
    drawerOpen.value = false
    ElMessage.success('字段已更新')
  } catch (error) {
    ElMessage.error(errorMessage(error, '字段更新失败'))
  } finally {
    savingColumn.value = false
  }
}

/** 关联关系在独立页面管理，这里只带上下文跳转过去 */
function openRelations(): void {
  router.push({
    name: 'relation',
    query: {
      database_id: activeDatabaseId.value || undefined,
      table_id: activeTableId.value || undefined,
    },
  })
}

// 任务面板跳转过来（同一个路由带不同 query）时切换业务库
watch(
  () => route.query.database_id,
  async value => {
    if (typeof value !== 'string' || !value || value === activeDatabaseId.value) return
    const target = databases.value.find(item => item.id === value)
    if (target) await selectDatabase(target.id)
  },
)

onMounted(loadDatabases)
</script>

<template>
  <div class="page">
    <PageHeader
      title="表与字段"
      description="表编号形如 <库编号>.<表名>，字段编号形如 <表编号>.<字段名>；名称与归属不可修改。字段只可改角色、描述与别名，类型与取值采集来自采集登记。改动字段的描述或别名会清掉挂在该字段上的关联，下一次检索按新语义重新识别。"
    >
      <template #actions>
        <el-select
          v-model="activeDatabaseId"
          class="db-select"
          placeholder="选择业务库"
          filterable
          :loading="loadingDatabases"
          :disabled="!databases.length"
          @change="selectDatabase"
        >
          <el-option
            v-for="item in databases"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
        <el-button :loading="loadingTables" @click="reload">
          <AppIcon name="refresh" :size="16" />
          <span>刷新</span>
        </el-button>
        <el-button :disabled="!activeTableId" @click="openRelations">
          <AppIcon name="relation" :size="16" />
          <span>关联关系</span>
        </el-button>
      </template>
    </PageHeader>

    <el-empty
      v-if="!databases.length && !loadingDatabases"
      class="empty-state"
      description="还没有可用的业务库，请先确认后端已登记业务库"
      :image-size="112"
    >
      <el-button type="primary" @click="router.push({ name: 'database' })">去业务库页面</el-button>
    </el-empty>

    <div v-else class="split">
      <aside class="split__side">
        <div class="split__side-head">
          <span class="split__side-title">表</span>
          <span class="muted-12">{{ tables.length }}</span>
        </div>
        <el-input
          v-model="tableKeyword"
          class="side-search"
          placeholder="按表名搜索"
          clearable
          @input="onTableKeyword"
          @clear="onTableKeyword"
        >
          <template #prefix>
            <AppIcon name="search" :size="16" />
          </template>
        </el-input>
        <div v-loading="loadingTables" class="table-list">
          <div
            v-for="item in tables"
            :key="item.id"
            class="table-row"
            :class="{ 'table-row--active': item.id === activeTableId }"
          >
            <el-checkbox
              class="table-row__check"
              :model-value="selectedTableIds.includes(item.id)"
              @change="toggleTable(item.id, Boolean($event))"
              @click.stop
            />
            <button type="button" class="table-item" @click="selectTable(item)">
              <span class="table-item__name">{{ item.name }}</span>
              <span class="tag" :class="tableRoleTag(item.role)">
                {{ tableRoleLabel(item.role) }}
              </span>
            </button>
          </div>
          <p v-if="!tables.length && !loadingTables" class="table-list__empty muted-12">
            {{ tableKeyword.trim() ? '没有匹配的表' : '该业务库下还没有表' }}
          </p>
        </div>

        <div v-if="selectedTableIds.length" class="batch-bar">
          <span class="muted-12 batch-bar__count">已选 {{ selectedTableIds.length }} 张</span>
          <el-button link type="danger" :loading="deletingBatch" @click="removeSelectedTables">
            批量删除
          </el-button>
          <el-button link @click="clearSelection">取消</el-button>
        </div>
      </aside>

      <section class="split__main">
        <template v-if="activeTable">
          <div class="detail">
            <div class="detail__head">
              <div>
                <h2 class="detail__title">{{ activeTable.name }}</h2>
                <p class="mono detail__id" :title="activeTable.id">{{ activeTable.id }}</p>
              </div>
              <div class="detail__actions">
                <el-button
                  plain
                  type="danger"
                  :loading="deletingTable"
                  @click="removeTable"
                >
                  <AppIcon name="trash" :size="16" />
                  <span>删除表</span>
                </el-button>
                <el-button :disabled="!tableDirty" @click="resetTableDraft(activeTable)">
                  撤销
                </el-button>
                <el-button
                  type="primary"
                  :disabled="!tableDirty"
                  :loading="savingTable"
                  @click="saveTable"
                >
                  保存
                </el-button>
              </div>
            </div>

            <div class="form-grid">
              <label class="field">
                <span class="field__label">表类型</span>
                <el-select v-model="tableDraft.role" placeholder="未设置">
                  <el-option label="未设置" value="" />
                  <el-option label="事实表" value="fact" />
                  <el-option label="维度表" value="dim" />
                </el-select>
              </label>
              <label class="field">
                <span class="field__label">表描述</span>
                <el-input
                  v-model="tableDraft.description"
                  type="textarea"
                  :rows="2"
                  resize="none"
                  placeholder="表在业务上的含义，留空表示清空"
                />
              </label>
            </div>
          </div>

          <div class="section">
            <div class="section__head">
              <h3 class="section__title">字段</h3>
              <span class="muted-12">{{ columns.length }} 个字段</span>
              <el-button
                link
                type="primary"
                :loading="loadingColumns"
                @click="loadColumns(activeTable.id)"
              >
                刷新
              </el-button>
            </div>

            <div class="table-wrap">
              <el-table v-loading="loadingColumns" :data="columns" row-key="id">
                <el-table-column label="字段名" min-width="150">
                  <template #default="{ row }">
                    <span class="cell-name">{{ row.name }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="类型" min-width="140">
                  <template #default="{ row }">
                    <span v-if="row.type" class="mono">{{ row.type }}</span>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="角色" width="100">
                  <template #default="{ row }">
                    <span class="tag" :class="columnRoleTag(row.role)">
                      {{ columnRoleLabel(row.role) }}
                    </span>
                  </template>
                </el-table-column>
                <el-table-column label="描述" min-width="180" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span v-if="row.description">{{ row.description }}</span>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="别名" min-width="170">
                  <template #default="{ row }">
                    <div v-if="row.alias?.length" class="tag-list">
                      <span v-for="alias in row.alias" :key="alias" class="tag tag--neutral">
                        {{ alias }}
                      </span>
                    </div>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="取值采集" width="110">
                  <template #default="{ row }">
                    <span class="tag" :class="row.synchronized ? 'tag--mint' : 'tag--neutral'">
                      {{ row.synchronized ? '已采集' : '未采集' }}
                    </span>
                  </template>
                </el-table-column>
                <el-table-column label="取值样本" min-width="200">
                  <template #default="{ row }">
                    <div v-if="row.values?.length" class="tag-list">
                      <span
                        v-for="value in row.values.slice(0, 3)"
                        :key="value"
                        class="tag tag--neutral"
                      >
                        {{ value }}
                      </span>
                      <span v-if="row.values.length > 3" class="tag tag--neutral">
                        +{{ row.values.length - 3 }}
                      </span>
                    </div>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="80" align="right">
                  <template #default="{ row }">
                    <el-button link type="primary" @click="openColumn(row)">编辑</el-button>
                  </template>
                </el-table-column>
                <template #empty>
                  <el-empty class="empty-cell" description="该表下还没有字段" :image-size="96" />
                </template>
              </el-table>
            </div>
          </div>

        </template>

        <el-empty v-else class="empty-state" description="从左侧选择一张表" :image-size="112" />
      </section>
    </div>

    <el-drawer
      v-model="drawerOpen"
      title="编辑字段"
      size="420px"
      :close-on-click-modal="false"
    >
      <div v-if="draftColumn" class="form-stack">
        <div class="drawer-meta">
          <span class="cell-name">{{ draftColumn.name }}</span>
          <span class="mono" :title="draftColumn.id">{{ draftColumn.id }}</span>
        </div>

        <div class="field">
          <span class="field__label">类型（采集登记，不可修改）</span>
          <p class="readonly-value">
            <span v-if="draftColumn.type" class="mono">{{ draftColumn.type }}</span>
            <span v-else class="muted">—</span>
          </p>
        </div>

        <label class="field">
          <span class="field__label">角色</span>
          <el-select v-model="columnDraft.role" placeholder="未设置">
            <el-option label="未设置" value="" />
            <el-option label="主键" value="primary_key" />
            <el-option label="外键" value="foreign_key" />
            <el-option label="度量" value="measure" />
            <el-option label="维度" value="dimension" />
          </el-select>
        </label>

        <label class="field">
          <span class="field__label">描述</span>
          <el-input
            v-model="columnDraft.description"
            type="textarea"
            :rows="3"
            resize="none"
            placeholder="字段在业务上的含义，留空表示清空"
          />
        </label>

        <label class="field">
          <span class="field__label">别名</span>
          <el-select
            v-model="columnDraft.alias"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
            placeholder="输入后回车新增，整组替换"
          />
        </label>

        <div class="field">
          <span class="field__label">取值采集（采集登记，不可修改）</span>
          <p class="readonly-value">
            <span class="tag" :class="draftColumn.synchronized ? 'tag--mint' : 'tag--neutral'">
              {{ draftColumn.synchronized ? '已采集' : '未采集' }}
            </span>
          </p>
        </div>

        <div class="field">
          <span class="field__label">取值样本（采样写入，不可修改）</span>
          <p v-if="draftColumn.values?.length" class="readonly-value tag-list">
            <span v-for="value in draftColumn.values" :key="value" class="tag tag--neutral">
              {{ value }}
            </span>
          </p>
          <p v-else class="readonly-value muted">还没有采到样本</p>
        </div>

        <p class="muted-12">
          可修改的只有角色、描述与别名；类型、取值采集与取值样本都由采集 / 采样写入。改描述或别名会清掉挂在这个字段上的关联。
        </p>
      </div>

      <template #footer>
        <div class="drawer-foot">
          <el-button @click="drawerOpen = false">取消</el-button>
          <el-button
            type="primary"
            :loading="savingColumn"
            :disabled="!columnDirty"
            @click="saveColumn"
          >
            保存
          </el-button>
        </div>
      </template>
    </el-drawer>

  </div>
</template>

<style scoped>
.db-select {
  width: 240px;
}

.split {
  display: grid;
  flex: 1;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 24px;
  margin-top: 24px;
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

.side-search {
  margin-top: 8px;
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

/* 一行 = 勾选框 + 表名；选中态落整行，勾选不影响当前表 */
.table-row {
  display: flex;
  flex: none;
  align-items: center;
  gap: 4px;
  padding: 0 8px 0 6px;
  border-radius: var(--radius-lg);
  transition:
    background-color 140ms ease,
    color 140ms ease;
}

.table-row:hover {
  background: var(--surface);
}

.table-row--active {
  background: var(--tint-lavender);
}

.table-row__check {
  flex: none;
  height: 36px;
}

.table-item {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 8px;
  min-width: 0;
  height: 36px;
  padding: 0 4px;
  border: none;
  background: transparent;
  color: var(--text);
  font-family: var(--font-body);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
}

.table-row--active .table-item {
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

.batch-bar {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 6px 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface-soft);
}

.batch-bar__count {
  margin-right: auto;
}

.split__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.detail__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.detail__title {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.01em;
}

.detail__id {
  margin: 4px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.detail .form-grid {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--hairline);
}

.drawer-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--hairline);
}

.drawer-meta .mono {
  overflow-wrap: anywhere;
}

.readonly-value {
  margin: 0;
  min-height: 22px;
  font-size: 13px;
  color: var(--text);
}

.drawer-foot {
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
