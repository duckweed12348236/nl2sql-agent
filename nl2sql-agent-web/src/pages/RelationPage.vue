<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import {
  createRelation,
  deleteRelation,
  listColumns,
  listDatabases,
  listRelations,
  listTables,
  updateRelation,
} from '@/api/modeling'
import { errorMessage } from '@/plugins/request'
import type {
  Column,
  Database,
  Relation,
  RelationCreate,
  RelationUpdate,
  Table,
} from '@/types/modeling'

/**
 * 关联关系：字段间的表间关系，按业务库划分。
 *
 * 关系是双向的（中间表存正反两行），列表按无序对去重、同一层关系只回一条；
 * 新增时一次写好两个方向，换端时换掉某一端、另一端不动。
 */

interface RelationDraft {
  /** 新增：一端 */
  fromTableId: string
  fromColumnId: string
  /** 新增：另一端 */
  toTableId: string
  toColumnId: string
  /** 换端：换上去的字段 */
  fieldTableId: string
  fieldColumnId: string
}

const route = useRoute()
const router = useRouter()

const databases = ref<Database[]>([])
const loadingDatabases = ref(false)
const activeDatabaseId = ref('')

const tables = ref<Table[]>([])
const loadingTables = ref(false)
/** 空串表示不限表：列出该业务库的全部关联 */
const tableFilter = ref('')
const keyword = ref('')

const relations = ref<Relation[]>([])
const loadingRelations = ref(false)

/** 各表的字段缓存：换端 / 新增关联时要跨表挑字段 */
const columnCache = reactive<Record<string, Column[]>>({})

const dialogOpen = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingRelation = ref<Relation | null>(null)
/** 换端时被替换的那一端（另一端保持不动） */
const replaceEnd = ref<'source' | 'target'>('target')
const submitting = ref(false)
const deletingKey = ref('')
const form = reactive<RelationDraft>({
  fromTableId: '',
  fromColumnId: '',
  toTableId: '',
  toColumnId: '',
  fieldTableId: '',
  fieldColumnId: '',
})

const activeDatabase = computed<Database | null>(
  () => databases.value.find(item => item.id === activeDatabaseId.value) ?? null,
)

/** 换端时保持不动的那一端，给用户一个明确的对照 */
const fixedEnd = computed<string>(() => {
  const origin = editingRelation.value
  if (!origin) return ''
  return replaceEnd.value === 'target' ? origin.source : origin.target
})

const filtered = computed<Relation[]>(() => {
  const search = keyword.value.trim().toLowerCase()

  return relations.value.filter(item => {
    if (activeDatabaseId.value && !belongsToDatabase(item, activeDatabaseId.value)) return false
    if (tableFilter.value && !touchesTable(item, tableFilter.value)) return false
    if (!search) return true
    return (
      item.source.toLowerCase().includes(search) ||
      item.target.toLowerCase().includes(search) ||
      item.source_id.toLowerCase().includes(search) ||
      item.target_id.toLowerCase().includes(search)
    )
  })
})

const emptyText = computed<string>(() => {
  if (!activeDatabaseId.value) return '先选择一个业务库'
  if (keyword.value.trim()) return '没有匹配的关联'
  if (tableFilter.value) return '这张表还没有关联'
  return '这个业务库还没有登记关联'
})

/** 关系两端同属一个业务库，按表编号前缀判断归属 */
function belongsToDatabase(row: Relation, database_id: string): boolean {
  return (
    row.source_id.startsWith(`${database_id}.`) || row.target_id.startsWith(`${database_id}.`)
  )
}

function touchesTable(row: Relation, table_id: string): boolean {
  return tableIdOf(row.source_id) === table_id || tableIdOf(row.target_id) === table_id
}

/** 字段编号形如 `<表编号>.<字段名>`，表编号形如 `<库编号>.<表名>` */
function tableIdOf(column_id: string): string {
  const index = column_id.lastIndexOf('.')
  return index > 0 ? column_id.slice(0, index) : column_id
}

function relationKey(row: Relation): string {
  return `${row.source_id}|${row.target_id}`
}

function columnsOf(table_id: string): Column[] {
  return columnCache[table_id] ?? []
}

async function ensureTableColumns(table_id: string): Promise<void> {
  if (!table_id || columnCache[table_id]) return
  try {
    columnCache[table_id] = await listColumns(table_id)
  } catch (error) {
    ElMessage.error(errorMessage(error, '字段列表加载失败'))
  }
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
      tables.value = []
      relations.value = []
    }
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  } finally {
    loadingDatabases.value = false
  }
}

async function loadTables(): Promise<void> {
  if (!activeDatabaseId.value) {
    tables.value = []
    return
  }

  loadingTables.value = true
  try {
    tables.value = await listTables(activeDatabaseId.value)
  } catch (error) {
    tables.value = []
    ElMessage.error(errorMessage(error, '表列表加载失败'))
  } finally {
    loadingTables.value = false
  }
}

async function loadRelations(): Promise<void> {
  loadingRelations.value = true
  try {
    relations.value = await listRelations()
  } catch (error) {
    relations.value = []
    ElMessage.error(errorMessage(error, '关联列表加载失败'))
  } finally {
    loadingRelations.value = false
  }
}

async function selectDatabase(id: string): Promise<void> {
  if (!id) return

  const requestedTable = typeof route.query.table_id === 'string' ? route.query.table_id : ''
  const keepTable =
    requestedTable && requestedTable.startsWith(`${id}.`) ? requestedTable : ''

  activeDatabaseId.value = id
  tableFilter.value = keepTable
  keyword.value = ''
  for (const key of Object.keys(columnCache)) delete columnCache[key]

  await Promise.all([loadTables(), loadRelations()])
}

function onDatabaseChange(id: string): void {
  void selectDatabase(id)
}

async function reload(): Promise<void> {
  await Promise.all([loadTables(), loadRelations()])
}

/* ---------------- 新增 / 换端 / 删除 ---------------- */

function onFromTableChange(): void {
  form.fromColumnId = ''
  void ensureTableColumns(form.fromTableId)
}

function onToTableChange(): void {
  form.toColumnId = ''
  void ensureTableColumns(form.toTableId)
}

function onFieldTableChange(): void {
  form.fieldColumnId = ''
  void ensureTableColumns(form.fieldTableId)
}

/** 换端默认落在原先那一端所在的表上，减少一次选择 */
function resetFieldEnd(): void {
  const origin = editingRelation.value
  if (!origin) return
  form.fieldColumnId = ''
  form.fieldTableId = tableIdOf(replaceEnd.value === 'target' ? origin.target_id : origin.source_id)
  void ensureTableColumns(form.fieldTableId)
}

function openCreate(): void {
  const defaultTableId = tableFilter.value || activeTableId() || ''
  dialogMode.value = 'create'
  editingRelation.value = null
  replaceEnd.value = 'target'
  form.fromTableId = defaultTableId
  form.fromColumnId = ''
  form.toTableId = defaultTableId
  form.toColumnId = ''
  form.fieldTableId = defaultTableId
  form.fieldColumnId = ''
  dialogOpen.value = true
  void ensureTableColumns(defaultTableId)
}

/** 未指定表时落到第一张表，新增关联总得从某张表出发 */
function activeTableId(): string {
  return tables.value[0]?.id ?? ''
}

function openEdit(row: Relation): void {
  dialogMode.value = 'edit'
  editingRelation.value = row
  replaceEnd.value = 'target'
  dialogOpen.value = true
  resetFieldEnd()
}

function onReplaceEndChange(): void {
  resetFieldEnd()
}

function buildPayload(): RelationCreate | RelationUpdate | null {
  if (dialogMode.value === 'create') {
    if (!form.fromColumnId || !form.toColumnId) {
      ElMessage.warning('两端的字段都要选')
      return null
    }
    if (form.fromColumnId === form.toColumnId) {
      ElMessage.warning('字段不能与自身建立关联')
      return null
    }
    return { source_id: form.fromColumnId, target_id: form.toColumnId }
  }

  const origin = editingRelation.value
  if (!origin || !form.fieldColumnId) {
    ElMessage.warning('请选择换上去的字段')
    return null
  }

  const payload: RelationUpdate = { source_id: origin.source_id, target_id: origin.target_id }
  if (replaceEnd.value === 'target') payload.new_target_id = form.fieldColumnId
  else payload.new_source_id = form.fieldColumnId
  return payload
}

async function submit(): Promise<void> {
  const payload = buildPayload()
  if (!payload) return

  submitting.value = true
  try {
    if (dialogMode.value === 'create') await createRelation(payload as RelationCreate)
    else await updateRelation(payload as RelationUpdate)

    ElMessage.success(dialogMode.value === 'create' ? '关联已新增' : '关联已更新')
    dialogOpen.value = false
    await loadRelations()
  } catch (error) {
    ElMessage.error(errorMessage(error, '关联保存失败'))
  } finally {
    submitting.value = false
  }
}

async function remove(row: Relation): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '正反两个方向会一起删掉；之后生成 SQL 时不会再按这条关系拼 JOIN（检索仍可能重新识别出来）。',
      `删除关联「${row.source} ↔ ${row.target}」`,
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

  deletingKey.value = relationKey(row)
  try {
    await deleteRelation(row.source_id, row.target_id)
    ElMessage.success('关联已删除')
    await loadRelations()
  } catch (error) {
    ElMessage.error(errorMessage(error, '关联删除失败'))
  } finally {
    deletingKey.value = ''
  }
}

// 从表与字段页带 query 跳过来时切换业务库
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
      title="关联关系"
      description="字段间的表间关系。关联是双向的，一次提交两个方向都查得到；两端必须已登记、不是同一个字段、且同属一个业务库（跨库无法 JOIN）。手工补的关系与检索识别出来的关系等价，下一次检索就会把它们交给模型拼 JOIN。"
    >
      <template #actions>
        <el-button :loading="loadingTables || loadingRelations" @click="reload">
          <AppIcon name="refresh" :size="16" />
          <span>刷新</span>
        </el-button>
        <el-button type="primary" :disabled="!tables.length" @click="openCreate">
          <AppIcon name="plus" :size="16" />
          <span>新增关联</span>
        </el-button>
      </template>
    </PageHeader>

    <el-empty
      v-if="!databases.length && !loadingDatabases"
      class="empty-state"
      description="还没有可用的业务库，请先登记业务库并采集"
      :image-size="112"
    >
      <el-button type="primary" @click="router.push({ name: 'database' })">去业务库页面</el-button>
    </el-empty>

    <template v-else>
      <div class="toolbar">
        <el-select
          v-model="activeDatabaseId"
          class="db-select"
          placeholder="选择业务库"
          filterable
          :loading="loadingDatabases"
          :disabled="!databases.length"
          @change="onDatabaseChange"
        >
          <el-option
            v-for="item in databases"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>

        <el-select
          v-model="tableFilter"
          class="table-select"
          placeholder="全部表"
          filterable
          clearable
          :loading="loadingTables"
          :disabled="!tables.length"
        >
          <el-option label="全部表" value="" />
          <el-option
            v-for="item in tables"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>

        <el-input v-model="keyword" class="search" placeholder="按表名或字段编号搜索" clearable>
          <template #prefix>
            <AppIcon name="search" :size="16" />
          </template>
        </el-input>

        <span class="muted-12">共 {{ filtered.length }} 条关联</span>
        <span v-if="activeDatabase && !activeDatabase.collected" class="muted-12">
          · 该库还没采集过，先采集才有字段可选
        </span>
      </div>

      <div class="table-wrap">
        <el-table
          v-loading="loadingRelations"
          :data="filtered"
          :row-key="relationKey"
        >
          <el-table-column label="一端" min-width="200">
            <template #default="{ row }">
              <span class="cell-name">{{ row.source }}</span>
            </template>
          </el-table-column>
          <el-table-column label="另一端" min-width="200">
            <template #default="{ row }">
              <span class="cell-name">{{ row.target }}</span>
            </template>
          </el-table-column>
          <el-table-column label="字段编号" min-width="280" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="mono">{{ row.source_id }} ↔ {{ row.target_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="130" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openEdit(row)">换端</el-button>
              <el-button
                link
                type="danger"
                :loading="deletingKey === relationKey(row)"
                @click="remove(row)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty class="empty-cell" :description="emptyText" :image-size="96" />
          </template>
        </el-table>
      </div>

      <p class="muted-12 hint">
        列表按无序对去重，同一层关系只回一条。改动字段的描述或别名会清掉挂在该字段上的关联，下一次检索按新语义重新识别。
      </p>
    </template>

    <el-dialog
      v-model="dialogOpen"
      :title="dialogMode === 'create' ? '新增关联' : '更换关联的一端'"
      width="620px"
      :close-on-click-modal="false"
    >
      <div class="form-stack">
        <template v-if="dialogMode === 'create'">
          <div class="form-grid">
            <div class="field">
              <span class="field__label">一端（表 / 字段）</span>
              <el-select v-model="form.fromTableId" filterable @change="onFromTableChange">
                <el-option
                  v-for="item in tables"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
                />
              </el-select>
              <el-select v-model="form.fromColumnId" filterable placeholder="选择字段">
                <el-option
                  v-for="column in columnsOf(form.fromTableId)"
                  :key="column.id"
                  :label="column.name"
                  :value="column.id"
                />
              </el-select>
            </div>

            <div class="field">
              <span class="field__label">另一端（表 / 字段）</span>
              <el-select v-model="form.toTableId" filterable @change="onToTableChange">
                <el-option
                  v-for="item in tables"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
                />
              </el-select>
              <el-select v-model="form.toColumnId" filterable placeholder="选择字段">
                <el-option
                  v-for="column in columnsOf(form.toTableId)"
                  :key="column.id"
                  :label="column.name"
                  :value="column.id"
                />
              </el-select>
            </div>
          </div>
        </template>

        <template v-else>
          <p class="muted-12">原关联：{{ editingRelation?.source }} ↔ {{ editingRelation?.target }}</p>

          <label class="field">
            <span class="field__label">换掉哪一端</span>
            <el-radio-group v-model="replaceEnd" @change="onReplaceEndChange">
              <el-radio-button value="source">一端</el-radio-button>
              <el-radio-button value="target">另一端</el-radio-button>
            </el-radio-group>
          </label>

          <p class="muted-12">另一端保持不动：{{ fixedEnd }}</p>

          <div class="form-grid">
            <label class="field">
              <span class="field__label">换上去的表</span>
              <el-select v-model="form.fieldTableId" filterable @change="onFieldTableChange">
                <el-option
                  v-for="item in tables"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
                />
              </el-select>
            </label>
            <label class="field">
              <span class="field__label">换上去的字段</span>
              <el-select v-model="form.fieldColumnId" filterable placeholder="选择字段">
                <el-option
                  v-for="column in columnsOf(form.fieldTableId)"
                  :key="column.id"
                  :label="column.name"
                  :value="column.id"
                />
              </el-select>
            </label>
          </div>
        </template>

        <p class="muted-12">
          两端必须已登记、不是同一个字段、且同属一个业务库；换端前后其实是同一对字段时服务端原样返回、不做多余写库。
        </p>
      </div>

      <template #footer>
        <div class="dialog-foot">
          <el-button @click="dialogOpen = false">取消</el-button>
          <el-button type="primary" :loading="submitting" @click="submit">
            {{ dialogMode === 'create' ? '新增' : '保存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.db-select {
  width: 220px;
}

.table-select {
  width: 200px;
}

.hint {
  margin: 12px 0 0;
}

.dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
