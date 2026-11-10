<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import {
  createModel,
  deleteModel,
  listModels,
  setModelDefault,
  updateModel,
} from '@/api/models'
import { errorMessage } from '@/plugins/request'
import type { ModelConfig, ModelCreate, ModelType, ModelUpdate } from '@/types/model'

/**
 * 模型管理：对话 / 嵌入 / 重排三类模型共用一套登记。
 * 运行期按各用途「默认且启用」的那条取用（默认登记有 5 秒短缓存），
 * 写操作之后服务端会清实例缓存并按新的默认对话模型重编一次检索图，改完一两秒内生效；
 * `api_key` 明文存库、明文返回，所以这里能直接复制。
 */

/** 其余请求参数的一行：参数名 + 参数值（值按字面量解析类型） */
interface ExtraRow {
  key: string
  value: string
}

interface ModelForm {
  type: ModelType
  name: string
  model_name: string
  base_url: string
  api_key: string
  dimension: string
  instruct_field: string
  /** 其余请求参数，逐行编辑 */
  extraRows: ExtraRow[]
  enabled: boolean
  is_default: boolean
  description: string
}

const TYPE_LABEL: Record<ModelType, string> = {
  chat: '对话',
  embedding: '向量',
  rerank: '重排',
}

const TYPE_TAG: Record<ModelType, string> = {
  chat: 'tag--sky',
  embedding: 'tag--mint',
  rerank: 'tag--peach',
}

const loading = ref(false)
const models = ref<ModelConfig[]>([])

/** 用途与启用状态走服务端过滤，关键字在本地匹配 */
const typeFilter = ref<'' | ModelType>('')
const enabledFilter = ref<'' | 'true' | 'false'>('')
const keyword = ref('')

const dialogOpen = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const editingModel = ref<ModelConfig | null>(null)
const submitting = ref(false)
const deletingId = ref('')
const defaultingId = ref('')
const togglingId = ref('')

const formRef = ref<FormInstance>()
const form = reactive<ModelForm>({
  type: 'chat',
  name: '',
  model_name: '',
  base_url: '',
  api_key: '',
  dimension: '',
  instruct_field: '',
  extraRows: [],
  enabled: true,
  is_default: false,
  description: '',
})

const rules: FormRules<ModelForm> = {
  name: [
    { required: true, message: '请填写登记名称', trigger: 'blur' },
    { max: 128, message: '名称最长 128 个字符', trigger: 'blur' },
  ],
  model_name: [
    { required: true, message: '请填写模型标识', trigger: 'blur' },
    { max: 255, message: '模型标识最长 255 个字符', trigger: 'blur' },
  ],
}

const filtered = computed<ModelConfig[]>(() => {
  const search = keyword.value.trim().toLowerCase()
  if (!search) return models.value
  return models.value.filter(
    item =>
      item.name.toLowerCase().includes(search) ||
      item.model_name.toLowerCase().includes(search) ||
      (item.base_url ?? '').toLowerCase().includes(search),
  )
})

const emptyText = computed<string>(() => {
  if (keyword.value.trim()) return '没有匹配的模型登记'
  if (typeFilter.value || enabledFilter.value) return '这个筛选条件下还没有登记'
  return '还没有登记模型，先加一条对话模型'
})

const isEmbedding = computed<boolean>(() => form.type === 'embedding')
const isRerank = computed<boolean>(() => form.type === 'rerank')

function typeLabel(type: ModelType): string {
  return TYPE_LABEL[type]
}

function typeTag(type: ModelType): string {
  return TYPE_TAG[type]
}

/** 参数值按字面量解析：数字与 true / false / null 走 JSON 类型，其余按字符串 */
function toExtraValue(text: string): unknown {
  const trimmed = text.trim()
  if (!trimmed) return ''
  if (trimmed === 'null') return null
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false

  const numeric = Number(trimmed)
  if (!Number.isNaN(numeric)) return numeric
  return trimmed
}

/** 逐行收集其余参数；参数名为空的行忽略，全空时返回 null（表示不设置） */
function buildExtra(rows: ExtraRow[]): Record<string, unknown> | null {
  const entries = rows
    .map(row => ({ key: row.key.trim(), value: row.value }))
    .filter(row => Boolean(row.key))
  if (!entries.length) return null

  const extra: Record<string, unknown> = {}
  for (const entry of entries) extra[entry.key] = toExtraValue(entry.value)
  return extra
}

/** 重复的参数名会导致后一行覆盖前一行，提交前先拦下来 */
function duplicateKey(rows: ExtraRow[]): string {
  const seen = new Set<string>()
  for (const row of rows) {
    const key = row.key.trim()
    if (!key) continue
    if (seen.has(key)) return key
    seen.add(key)
  }
  return ''
}

/** 已有参数摊成一行一项，非基础类型按 JSON 文本回填 */
function toExtraRows(extra: Record<string, unknown> | null): ExtraRow[] {
  if (!extra) return []
  return Object.entries(extra).map(([key, value]) => ({
    key,
    value: value !== null && typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''),
  }))
}

function addExtraRow(): void {
  form.extraRows.push({ key: '', value: '' })
}

function removeExtraRow(index: number): void {
  form.extraRows.splice(index, 1)
}

/** 密钥明文返回：直接复制到别处用 */
async function copyKey(apiKey: string | null): Promise<void> {
  if (!apiKey) return
  try {
    await navigator.clipboard.writeText(apiKey)
    ElMessage.success('API Key 已复制')
  } catch {
    ElMessage.warning('当前浏览器不允许访问剪贴板')
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    models.value = await listModels({
      type: typeFilter.value || undefined,
      enabled: enabledFilter.value === '' ? undefined : enabledFilter.value === 'true',
    })
  } catch (error) {
    models.value = []
    ElMessage.error(errorMessage(error, '模型列表加载失败'))
  } finally {
    loading.value = false
  }
}

function resetForm(): void {
  form.type = 'chat'
  form.name = ''
  form.model_name = ''
  form.base_url = ''
  form.api_key = ''
  form.dimension = ''
  form.instruct_field = ''
  form.extraRows = []
  form.enabled = true
  form.is_default = false
  form.description = ''
}

function openCreate(): void {
  dialogMode.value = 'create'
  editingId.value = ''
  editingModel.value = null
  resetForm()
  dialogOpen.value = true
}

function openEdit(model: ModelConfig): void {
  dialogMode.value = 'edit'
  editingId.value = model.id
  editingModel.value = model
  form.type = model.type
  form.name = model.name
  form.model_name = model.model_name
  form.base_url = model.base_url ?? ''
  form.api_key = model.api_key ?? ''
  form.dimension = model.dimension === null ? '' : String(model.dimension)
  form.instruct_field = model.instruct_field ?? ''
  form.extraRows = toExtraRows(model.extra)
  form.enabled = model.enabled
  form.is_default = model.is_default
  form.description = model.description ?? ''
  dialogOpen.value = true
}

/** 新增的请求体：用途特有的项只在该用途下提交 */
function buildCreate(): ModelCreate {
  return {
    name: form.name.trim(),
    type: form.type,
    model_name: form.model_name.trim(),
    base_url: form.base_url.trim() || null,
    api_key: form.api_key.trim() || null,
    dimension: isEmbedding.value ? Number(form.dimension) : null,
    instruct_field: isRerank.value ? form.instruct_field.trim() || null : null,
    extra: buildExtra(form.extraRows),
    enabled: form.enabled,
    is_default: form.is_default,
    description: form.description.trim() || null,
  }
}

function sameExtra(a: Record<string, unknown> | null, b: Record<string, unknown> | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** 修改的请求体：只提交有变化的字段（未提交即保持原值） */
function buildUpdate(origin: ModelConfig): ModelUpdate {
  const payload: ModelUpdate = {}

  if (form.type !== origin.type) payload.type = form.type

  const name = form.name.trim()
  if (name !== origin.name) payload.name = name

  const modelName = form.model_name.trim()
  if (modelName !== origin.model_name) payload.model_name = modelName

  const baseUrl = form.base_url.trim() || null
  if (baseUrl !== origin.base_url) payload.base_url = baseUrl

  const dimension = isEmbedding.value ? Number(form.dimension) : null
  if (dimension !== origin.dimension) payload.dimension = dimension

  const instructField = isRerank.value ? form.instruct_field.trim() || null : null
  if (instructField !== origin.instruct_field) payload.instruct_field = instructField

  const extra = buildExtra(form.extraRows)
  if (!sameExtra(extra, origin.extra)) payload.extra = extra

  if (form.enabled !== origin.enabled) payload.enabled = form.enabled
  if (form.is_default !== origin.is_default) payload.is_default = form.is_default

  const description = form.description.trim() || null
  if (description !== origin.description) payload.description = description

  // 密钥明文回填：改动才提交，清空即提交 null（服务端视为清空）
  const apiKey = form.api_key.trim() || null
  if (apiKey !== origin.api_key) payload.api_key = apiKey

  return payload
}

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  if (isEmbedding.value && !form.dimension.trim()) {
    ElMessage.warning('向量模型必须填写 dimension（向量维度，与向量库集合一致）')
    return
  }

  const repeated = duplicateKey(form.extraRows)
  if (repeated) {
    ElMessage.warning(`其余参数里的「${repeated}」重复了`)
    return
  }

  if (dialogMode.value === 'create') {
    submitting.value = true
    try {
      await createModel(buildCreate())
      ElMessage.success('模型已登记')
      dialogOpen.value = false
      await load()
    } catch (error) {
      ElMessage.error(errorMessage(error, '新增模型失败'))
    } finally {
      submitting.value = false
    }
    return
  }

  const origin = editingModel.value
  if (!origin) return

  const payload = buildUpdate(origin)
  if (!Object.keys(payload).length) {
    ElMessage.info('没有可修改的字段')
    return
  }

  submitting.value = true
  try {
    await updateModel(editingId.value, payload)
    ElMessage.success('模型已更新')
    dialogOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '更新模型失败'))
  } finally {
    submitting.value = false
  }
}

/** 启用 / 停用：直接提交，改完服务端清实例缓存 */
async function toggleEnabled(model: ModelConfig, value: boolean): Promise<void> {
  togglingId.value = model.id
  try {
    await updateModel(model.id, { enabled: value })
    ElMessage.success(value ? '已启用' : '已停用')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '启用状态更新失败'))
  } finally {
    togglingId.value = ''
  }
}

/** 设为该用途的默认：同用途其他默认会被清掉 */
async function makeDefault(model: ModelConfig): Promise<void> {
  defaultingId.value = model.id
  try {
    await setModelDefault(model.id)
    ElMessage.success(`已设为${typeLabel(model.type)}模型的默认`)
    if (!model.enabled) ElMessage.warning('这条登记已停用，运行期会按启用状态回落')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '设为默认失败'))
  } finally {
    defaultingId.value = ''
  }
}

async function remove(model: ModelConfig): Promise<void> {
  const lastChat =
    model.type === 'chat' && model.enabled
      ? ' 这是启用中的对话模型：删光后刷新运行期会失败（500），记得留一条可用的。'
      : ''

  try {
    await ElMessageBox.confirm(
      `只删登记，不动别的：删的正是该用途的默认时，该用途可能暂时没有默认，运行期会退到最新登记的一条并记告警。${lastChat}`,
      `删除模型「${model.name}」`,
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

  deletingId.value = model.id
  try {
    await deleteModel(model.id)
    ElMessage.success('模型已删除')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '删除模型失败'))
  } finally {
    deletingId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader
      title="模型"
      description="对话 / 嵌入 / 重排三类模型共用一套登记，运行期按各用途「默认且启用」的那条取用（默认登记有 5 秒短缓存），检索、采集与评测都吃这份登记。写操作之后服务端会清实例缓存并按新的默认对话模型重编一次检索图，改完一两秒内生效；密钥明文存库、明文返回，可直接复制。注意别把启用的对话模型删光：刷新运行期时要按它重编图，一条不剩会让这次写操作报错。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <AppIcon name="refresh" :size="16" />
          <span>刷新</span>
        </el-button>
        <el-button type="primary" @click="openCreate">
          <AppIcon name="plus" :size="16" />
          <span>新建模型</span>
        </el-button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <el-select v-model="typeFilter" class="type-select" placeholder="全部用途" @change="load">
        <el-option label="全部用途" value="" />
        <el-option label="对话 chat" value="chat" />
        <el-option label="向量 embedding" value="embedding" />
        <el-option label="重排 rerank" value="rerank" />
      </el-select>
      <el-select v-model="enabledFilter" class="state-select" placeholder="全部状态" @change="load">
        <el-option label="全部状态" value="" />
        <el-option label="启用" value="true" />
        <el-option label="停用" value="false" />
      </el-select>
      <el-input v-model="keyword" class="search" placeholder="按名称、模型标识或接口地址搜索" clearable>
        <template #prefix>
          <AppIcon name="search" :size="16" />
        </template>
      </el-input>
      <span class="muted-12">共 {{ models.length }} 条登记</span>
    </div>

    <div v-if="loading || filtered.length" class="table-wrap">
      <el-table v-loading="loading" :data="filtered" row-key="id">
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="cell-name">{{ row.name }}</span>
              <span v-if="row.description" class="muted-12">{{ row.description }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="用途" width="90">
          <template #default="{ row }">
            <span class="tag" :class="typeTag(row.type)">{{ typeLabel(row.type) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="模型标识" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono" :title="row.model_name">{{ row.model_name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="维度" width="80">
          <template #default="{ row }">
            <span v-if="row.dimension" class="mono">{{ row.dimension }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="密钥" width="130">
          <template #default="{ row }">
            <div v-if="row.api_key" class="key-cell">
              <span class="tag tag--mint">已配置</span>
              <el-tooltip content="复制密钥" placement="top">
                <el-button link type="primary" class="key-cell__copy" @click="copyKey(row.api_key)">
                  <AppIcon name="copy" :size="14" />
                </el-button>
              </el-tooltip>
            </div>
            <span v-else class="tag tag--neutral">未配置</span>
          </template>
        </el-table-column>
        <el-table-column label="默认" width="80">
          <template #default="{ row }">
            <span v-if="row.is_default" class="tag tag--accent">默认</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="togglingId === row.id"
              @change="(value: boolean) => toggleEnabled(row, value)"
            />
          </template>
        </el-table-column>
        <el-table-column label="编号" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono" :title="row.id">{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              type="primary"
              :disabled="row.is_default"
              :loading="defaultingId === row.id"
              @click="makeDefault(row)"
            >
              设为默认
            </el-button>
            <el-button link type="danger" :loading="deletingId === row.id" @click="remove(row)">
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
      :title="dialogMode === 'create' ? '新建模型' : '编辑模型'"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <div class="form-grid">
          <el-form-item label="用途" prop="type">
            <el-select v-model="form.type" placeholder="选择用途">
              <el-option label="对话 chat" value="chat" />
              <el-option label="向量 embedding" value="embedding" />
              <el-option label="重排 rerank" value="rerank" />
            </el-select>
          </el-form-item>

          <el-form-item label="登记名称" prop="name">
            <el-input v-model="form.name" maxlength="128" placeholder="如 本地千问（名称可以重复）" />
          </el-form-item>

          <el-form-item label="模型标识" prop="model_name">
            <el-input
              v-model="form.model_name"
              maxlength="255"
              placeholder="如 Qwen/Qwen3-32B"
            />
          </el-form-item>

          <el-form-item label="接口地址">
            <el-input
              v-model="form.base_url"
              maxlength="255"
              :placeholder="isRerank ? '重排填完整请求地址' : 'OpenAI 兼容端点的 base，如 http://host:8000/v1'"
            />
          </el-form-item>

          <el-form-item :label="isEmbedding ? '向量维度（必填）' : '向量维度'">
            <el-input
              v-model="form.dimension"
              :disabled="!isEmbedding"
              placeholder="与向量库集合的维度一致"
            />
          </el-form-item>

          <el-form-item v-if="isRerank" label="任务说明字段名">
            <el-input
              v-model="form.instruct_field"
              maxlength="64"
              placeholder="留空表示不发，如 instruction"
            />
          </el-form-item>

          <el-form-item label="API Key（明文存库，本地端点可留空）">
            <el-input
              v-model="form.api_key"
              type="password"
              show-password
              maxlength="255"
              placeholder="留空表示不设置；清空已有密钥就删掉内容"
            />
          </el-form-item>
        </div>

        <el-form-item label="其余请求参数（如温度、max_tokens、超时）">
          <div class="extra">
            <div v-for="(row, index) in form.extraRows" :key="index" class="extra__row">
              <el-input v-model="row.key" class="extra__key" placeholder="参数名，如 temperature" />
              <el-input v-model="row.value" class="extra__value" placeholder="参数值，如 0.7" />
              <el-button link type="danger" @click="removeExtraRow(index)">
                <AppIcon name="trash" :size="14" />
              </el-button>
            </div>

            <el-button link type="primary" class="extra__add" @click="addExtraRow">
              <AppIcon name="plus" :size="14" />
              <span>添加参数</span>
            </el-button>

            <p class="muted-12 extra__hint">
              不填参数名即忽略该行；数字与 true / false / null 按字面量提交，其余按字符串。
            </p>
          </div>
        </el-form-item>

        <el-form-item label="备注">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="如 检索与出题用的主模型" />
        </el-form-item>

        <div class="switch-row">
          <div class="field field--row">
            <span class="field__label">启用（停用后运行期不会取用）</span>
            <el-switch v-model="form.enabled" />
          </div>
          <div class="field field--row">
            <span class="field__label">设为该用途的默认（同用途其他默认会被清掉）</span>
            <el-switch v-model="form.is_default" />
          </div>
        </div>
      </el-form>

      <template #footer>
        <div class="dialog-foot">
          <el-button @click="dialogOpen = false">取消</el-button>
          <el-button type="primary" :loading="submitting" @click="submit">
            {{ dialogMode === 'create' ? '登记' : '保存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.type-select {
  width: 160px;
}

.state-select {
  width: 130px;
}

.cell-stack {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.key-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* 表格单元格默认 overflow: hidden，按钮的内边距会被裁掉，这里压成图标大小 */
.key-cell__copy {
  padding: 0;
  min-height: 0;
  height: 20px;
}

.extra {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.extra__row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.extra__key {
  width: 220px;
  flex: none;
}

.extra__value {
  flex: 1;
  min-width: 0;
}

.extra__add {
  align-self: flex-start;
}

.extra__hint {
  margin: 0;
}

.switch-row {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 8px;
}

.dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
