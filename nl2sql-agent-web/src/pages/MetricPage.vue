<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { createMetric, deleteMetric, listDatabases, listMetrics, updateMetric } from '@/api/modeling'
import { errorMessage } from '@/plugins/request'
import type { Database, Metric, MetricCreate, MetricUpdate } from '@/types/modeling'

interface MetricForm {
  /** 所属业务库：只有新增时可选，登记后不可修改 */
  database_id: string
  name: string
  description: string
  alias: string[]
}

const loading = ref(false)
const metrics = ref<Metric[]>([])
const keyword = ref('')

const databases = ref<Database[]>([])
const loadingDatabases = ref(false)
/** 空串表示不过滤：列出全部库的指标（含没有所属库的老指标） */
const databaseId = ref('')

const dialogOpen = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const editingMetric = ref<Metric | null>(null)
const submitting = ref(false)
const deletingId = ref('')

const formRef = ref<FormInstance>()
const form = reactive<MetricForm>({ database_id: '', name: '', description: '', alias: [] })

const rules: FormRules<MetricForm> = {
  database_id: [{ required: true, message: '请选择所属业务库', trigger: 'change' }],
  name: [
    { required: true, message: '请填写指标名称', trigger: 'blur' },
    { max: 128, message: '名称最长 128 个字符', trigger: 'blur' },
  ],
}

const filtered = computed<Metric[]>(() => {
  const search = keyword.value.trim().toLowerCase()
  if (!search) return metrics.value
  return metrics.value.filter(
    item =>
      item.name.toLowerCase().includes(search) ||
      (item.description ?? '').toLowerCase().includes(search) ||
      (item.alias ?? []).some(alias => alias.toLowerCase().includes(search)),
  )
})

const emptyText = computed<string>(() => {
  if (keyword.value.trim()) return '没有匹配的指标'
  return databaseId.value ? '这个业务库下还没有指标' : '还没有登记指标'
})

/** 归属库名称：老指标没有所属库，按未归属展示 */
function databaseName(id: string | null): string {
  if (!id) return '未归属'
  const target = databases.value.find(item => item.id === id)
  return target ? target.name : id
}

async function loadDatabases(): Promise<void> {
  loadingDatabases.value = true
  try {
    databases.value = await listDatabases()
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  } finally {
    loadingDatabases.value = false
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    metrics.value = await listMetrics(databaseId.value || undefined)
  } catch (error) {
    metrics.value = []
    ElMessage.error(errorMessage(error, '指标列表加载失败'))
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  dialogMode.value = 'create'
  editingId.value = ''
  editingMetric.value = null
  form.database_id = databaseId.value || databases.value[0]?.id || ''
  form.name = ''
  form.description = ''
  form.alias = []
  dialogOpen.value = true
}

function openEdit(metric: Metric): void {
  dialogMode.value = 'edit'
  editingId.value = metric.id
  editingMetric.value = metric
  form.database_id = metric.database_id ?? ''
  form.name = metric.name
  form.description = metric.description ?? ''
  form.alias = [...(metric.alias ?? [])]
  dialogOpen.value = true
}

function sameAlias(a: string[] | null, b: string[] | null): boolean {
  if (a === null || b === null) return a === b
  return a.length === b.length && a.every((item, index) => item === b[index])
}

function buildUpdate(origin: Metric): MetricUpdate {
  const payload: MetricUpdate = {}

  const name = form.name.trim()
  if (name !== origin.name) payload.name = name

  const description = form.description.trim() || null
  if (description !== (origin.description ?? null)) payload.description = description

  const alias = form.alias.length ? form.alias : null
  if (!sameAlias(alias, origin.alias ?? null)) payload.alias = alias

  return payload
}

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  if (dialogMode.value === 'create') {
    if (!form.database_id) {
      ElMessage.warning('请先选择指标所属的业务库')
      return
    }

    const payload: MetricCreate = { database_id: form.database_id, name: form.name.trim() }
    const description = form.description.trim()
    if (description) payload.description = description
    if (form.alias.length) payload.alias = form.alias

    submitting.value = true
    try {
      await createMetric(payload)
      ElMessage.success('指标已新增')
      dialogOpen.value = false
      await load()
    } catch (error) {
      ElMessage.error(errorMessage(error, '新增指标失败'))
    } finally {
      submitting.value = false
    }
    return
  }

  const origin = editingMetric.value
  if (!origin) return

  const payload = buildUpdate(origin)
  if (!Object.keys(payload).length) {
    ElMessage.info('没有可修改的字段')
    return
  }

  submitting.value = true
  try {
    await updateMetric(editingId.value, payload)
    ElMessage.success('指标已更新')
    dialogOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '指标更新失败'))
  } finally {
    submitting.value = false
  }
}

async function remove(metric: Metric): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '先删元数据库登记，再按编号删掉该库指标集合里的点；向量库清不动只记日志、不影响删除结果。',
      `删除指标「${metric.name}」`,
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

  deletingId.value = metric.id
  try {
    await deleteMetric(metric.id)
    ElMessage.success('指标已删除')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '指标删除失败'))
  } finally {
    deletingId.value = ''
  }
}

onMounted(async () => {
  await loadDatabases()
  await load()
})
</script>

<template>
  <div class="page">
    <PageHeader
      title="指标"
      description="指标按业务库划分，检索时只在所属库的集合里召回。归属在登记时确定、之后不可修改；指标编号与名字无关，改名不牵动任何引用，三个可改字段都会重新向量化。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <AppIcon name="refresh" :size="16" />
          <span>刷新</span>
        </el-button>
        <el-button type="primary" :disabled="!databases.length" @click="openCreate">
          <AppIcon name="plus" :size="16" />
          <span>新建指标</span>
        </el-button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <el-select
        v-model="databaseId"
        class="db-select"
        placeholder="全部业务库"
        filterable
        clearable
        :loading="loadingDatabases"
        :disabled="!databases.length"
        @change="load"
      >
        <el-option label="全部业务库" value="" />
        <el-option
          v-for="item in databases"
          :key="item.id"
          :label="item.name"
          :value="item.id"
        />
      </el-select>
      <el-input v-model="keyword" class="search" placeholder="按名称、描述或别名搜索" clearable>
        <template #prefix>
          <AppIcon name="search" :size="16" />
        </template>
      </el-input>
      <span class="muted-12">共 {{ metrics.length }} 个指标</span>
      <span v-if="!databases.length" class="muted-12">· 未取到业务库，新增指标暂不可用</span>
    </div>

    <div v-if="loading || filtered.length" class="table-wrap">
      <el-table v-loading="loading" :data="filtered" row-key="id">
        <el-table-column label="指标名称" min-width="160">
          <template #default="{ row }">
            <span class="cell-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="所属业务库" min-width="150">
          <template #default="{ row }">
            <span v-if="row.database_id" class="tag tag--neutral">
              {{ databaseName(row.database_id) }}
            </span>
            <span v-else class="tag tag--peach">未归属</span>
          </template>
        </el-table-column>
        <el-table-column label="描述" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.description">{{ row.description }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="别名" min-width="180">
          <template #default="{ row }">
            <div v-if="row.alias?.length" class="tag-list">
              <span v-for="alias in row.alias" :key="alias" class="tag tag--neutral">{{ alias }}</span>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="编号" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono" :title="row.id">{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              type="danger"
              :loading="deletingId === row.id"
              @click="remove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="emptyText" :image-size="72" />
        </template>
      </el-table>
    </div>

    <el-empty v-else class="empty-state" :description="emptyText" :image-size="112" />

    <el-dialog
      v-model="dialogOpen"
      :title="dialogMode === 'create' ? '新建指标' : '编辑指标'"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item v-if="dialogMode === 'create'" label="所属业务库" prop="database_id">
          <el-select v-model="form.database_id" placeholder="选择所属业务库" filterable>
            <el-option
              v-for="item in databases"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
        </el-form-item>

        <div v-else class="field">
          <span class="field__label">所属业务库（登记后不可修改）</span>
          <p class="readonly-value">{{ databaseName(editingMetric?.database_id ?? null) }}</p>
        </div>

        <el-form-item label="指标名称" prop="name">
          <el-input v-model="form.name" maxlength="128" show-word-limit placeholder="如 GMV" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="如 成交总额" />
        </el-form-item>
        <el-form-item label="别名" prop="alias">
          <el-select
            v-model="form.alias"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
            placeholder="输入后回车新增，如 销售额"
          />
        </el-form-item>
      </el-form>

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

.field {
  margin-bottom: 18px;
}

.readonly-value {
  margin: 0;
  font-size: 13px;
  color: var(--text);
}

.dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
