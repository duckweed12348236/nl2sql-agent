<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import TaskPanel from '@/components/TaskPanel.vue'
import {
  collectDatabase,
  createDatabase,
  deleteDatabase,
  listDatabaseTables,
  listDatabases,
  setDatabaseBlacklist,
  updateDatabase,
} from '@/api/modeling'
import { useTasks } from '@/composables/useTasks'
import { errorMessage } from '@/plugins/request'
import type { Database, DatabaseCreate, DatabaseUpdate } from '@/types/modeling'

const router = useRouter()
const { settledTick, openPanel } = useTasks()

const loading = ref(false)
const databases = ref<Database[]>([])
const keyword = ref('')
const collectingId = ref('')
const deletingId = ref('')

/* ---------------- 新增 / 修改 ---------------- */

interface DatabaseForm {
  name: string
  /** 连接地址含账号口令，只写不读：改的时候留空表示不修改 */
  url: string
  type: 'mysql'
  /** 附加配置，按 JSON 文本编辑 */
  extra: string
}

const dialogOpen = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const submitting = ref(false)
const form = reactive<DatabaseForm>({ name: '', url: '', type: 'mysql', extra: '' })

/* ---------------- 采集黑名单 ---------------- */

const blacklistOpen = ref(false)
const blacklistTarget = ref<Database | null>(null)
/** 抽屉里正在编辑的黑名单（整组，提交前不动原值） */
const blacklistTables = ref<string[]>([])
/** 业务库里实际存在的表名，供下拉挑选 */
const blacklistOptions = ref<string[]>([])
const loadingBlacklistOptions = ref(false)
const savingBlacklist = ref(false)

const filtered = computed<Database[]>(() => {
  const search = keyword.value.trim().toLowerCase()
  if (!search) return databases.value
  return databases.value.filter(
    item => item.name.toLowerCase().includes(search) || item.id.includes(search),
  )
})

const emptyText = computed<string>(() =>
  databases.value.length ? '没有匹配的业务库' : '还没有登记业务库',
)

const editing = computed<Database | null>(
  () => databases.value.find(item => item.id === editingId.value) ?? null,
)

/** 已采集的库登记锁死，服务端一律 `409`，这里直接禁掉入口 */
const editable = computed<boolean>(() => dialogMode.value === 'create' || !editing.value?.collected)

async function load(): Promise<void> {
  loading.value = true
  try {
    databases.value = await listDatabases()
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  } finally {
    loading.value = false
  }
}

/**
 * 附加配置按 JSON 文本编辑：空串表示清空（`null`），
 * 不是 JSON 对象时返回 `undefined`（调用方提示后中止）。
 */
function parseExtra(text: string): Record<string, unknown> | null | undefined {
  const trimmed = text.trim()
  if (!trimmed) return null

  try {
    const value: unknown = JSON.parse(trimmed)
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return value as Record<string, unknown>
    }
  } catch {
    // 落到下面统一按非法处理
  }

  return undefined
}

function openCreate(): void {
  dialogMode.value = 'create'
  editingId.value = ''
  form.name = ''
  form.url = ''
  form.type = 'mysql'
  form.extra = ''
  dialogOpen.value = true
}

function openEdit(item: Database): void {
  dialogMode.value = 'edit'
  editingId.value = item.id
  form.name = item.name
  form.url = ''
  form.type = item.type ?? 'mysql'
  form.extra = item.extra ? JSON.stringify(item.extra, null, 2) : ''
  dialogOpen.value = true
}

function buildUpdate(origin: Database): DatabaseUpdate {
  const payload: DatabaseUpdate = {}

  const name = form.name.trim()
  if (name && name !== origin.name) payload.name = name

  // 连接地址只写不读：输入了就整体替换，留空即不改
  const url = form.url.trim()
  if (url) payload.url = url

  if (form.type !== origin.type) payload.type = form.type

  const extra = parseExtra(form.extra)
  if (extra !== undefined && JSON.stringify(extra) !== JSON.stringify(origin.extra ?? null)) {
    payload.extra = extra
  }

  return payload
}

async function save(): Promise<void> {
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning('请填写业务库名称')
    return
  }
  if (name.length > 128) {
    ElMessage.warning('业务库名称最长 128 个字符')
    return
  }

  if (dialogMode.value === 'create') {
    if (!form.url.trim()) {
      ElMessage.warning('请填写连接地址')
      return
    }
  }

  const extra = parseExtra(form.extra)
  if (extra === undefined) {
    ElMessage.warning('附加配置需要是一个 JSON 对象，留空表示清空')
    return
  }

  const origin = editing.value
  let payload: DatabaseCreate | DatabaseUpdate

  if (dialogMode.value === 'create') {
    payload = { name, url: form.url.trim(), type: form.type, extra }
  } else if (origin) {
    payload = buildUpdate(origin)
    if (!Object.keys(payload).length) {
      ElMessage.info('没有可修改的字段')
      return
    }
  } else {
    return
  }

  submitting.value = true
  try {
    if (dialogMode.value === 'create') {
      await createDatabase(payload as DatabaseCreate)
      ElMessage.success('业务库已登记')
    } else if (origin) {
      await updateDatabase(origin.id, payload)
      ElMessage.success('业务库已更新')
    }

    dialogOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '业务库保存失败'))
  } finally {
    submitting.value = false
  }
}

async function remove(item: Database): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '名下已登记的表、字段与指标会一起删除，向量库里该库的字段、取值与指标集合也一并清掉；之后要再用得重新登记并采集。',
      `删除业务库「${item.name}」`,
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

  deletingId.value = item.id
  try {
    await deleteDatabase(item.id)
    ElMessage.success('业务库已删除')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '业务库删除失败'))
  } finally {
    deletingId.value = ''
  }
}

/** 采集是后台任务：投递即返回，进度去任务面板看 */
async function collect(item: Database): Promise<void> {
  collectingId.value = item.id
  try {
    await collectDatabase(item.id)
    ElMessage.success('采集任务已投递，可以在任务面板里看进度')
    openPanel()
  } catch (error) {
    ElMessage.error(errorMessage(error, '采集任务投递失败'))
  } finally {
    collectingId.value = ''
  }
}

function openTables(item: Database): void {
  router.push({ name: 'table', query: { database_id: item.id } })
}

/* ---------------- 采集黑名单：整组替换 ---------------- */

function replaceDatabase(updated: Database): void {
  const index = databases.value.findIndex(item => item.id === updated.id)
  if (index >= 0) databases.value.splice(index, 1, updated)
}

/** 读业务库里实际存在的表名；关键字为空即列出全部，供拉黑时挑选 */
async function searchBlacklistTables(name: string): Promise<void> {
  const target = blacklistTarget.value
  if (!target) return

  loadingBlacklistOptions.value = true
  try {
    blacklistOptions.value = await listDatabaseTables(target.id, name.trim() || undefined)
  } catch (error) {
    blacklistOptions.value = []
    ElMessage.error(errorMessage(error, '业务库表名加载失败'))
  } finally {
    loadingBlacklistOptions.value = false
  }
}

function openBlacklist(item: Database): void {
  blacklistTarget.value = item
  blacklistTables.value = [...(item.blacklist ?? [])]
  blacklistOptions.value = []
  blacklistOpen.value = true
  void searchBlacklistTables('')
}

async function saveBlacklist(): Promise<void> {
  const target = blacklistTarget.value
  if (!target) return

  savingBlacklist.value = true
  try {
    const updated = await setDatabaseBlacklist(target.id, blacklistTables.value)
    replaceDatabase(updated)
    ElMessage.success('采集黑名单已保存')
    blacklistOpen.value = false
  } catch (error) {
    ElMessage.error(errorMessage(error, '采集黑名单保存失败'))
  } finally {
    savingBlacklist.value = false
  }
}

async function copyId(id: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(id)
    ElMessage.success('编号已复制')
  } catch {
    ElMessage.warning('当前浏览器不允许访问剪贴板')
  }
}

// 采集任务收尾后，`collected` 会跟着置位，列表刷一次就同步了
watch(settledTick, () => {
  void load()
})

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader
      title="业务库"
      description="已登记的业务数据库。编号为服务端生成的雪花 ID，前端按字符串原样保存以避免精度丢失；连接地址含账号口令、只写不读。整库采集是后台任务，投递即返回，采集成功后登记锁死、不能再改——唯一的例外是采集黑名单。"
    >
      <template #actions>
        <TaskPanel />
        <el-button :loading="loading" @click="load">
          <AppIcon name="refresh" :size="16" />
          <span>刷新</span>
        </el-button>
        <el-button type="primary" @click="openCreate">
          <AppIcon name="plus" :size="16" />
          <span>新增业务库</span>
        </el-button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <el-input v-model="keyword" class="search" placeholder="按名称或编号搜索" clearable>
        <template #prefix>
          <AppIcon name="search" :size="16" />
        </template>
      </el-input>
      <span class="muted-12">共 {{ databases.length }} 个业务库</span>
    </div>

    <div v-loading="loading" class="db-grid">
      <article v-for="item in filtered" :key="item.id" class="db-card">
        <header class="db-card__head">
          <AppIcon name="database" :size="18" />
          <span class="db-card__name">{{ item.name }}</span>
        </header>

        <div class="tag-list">
          <span class="tag tag--neutral">{{ item.type ?? '未知类型' }}</span>
          <span class="tag" :class="item.collected ? 'tag--mint' : 'tag--neutral'">
            {{ item.collected ? '已采集' : '未采集' }}
          </span>
          <span v-if="item.blacklist?.length" class="tag tag--peach">
            黑名单 {{ item.blacklist.length }} 张
          </span>
        </div>

        <div class="db-card__id">
          <span class="mono" :title="item.id">{{ item.id }}</span>
          <el-button link type="primary" @click="copyId(item.id)">
            <AppIcon name="copy" :size="14" />
            <span>复制编号</span>
          </el-button>
        </div>

        <p v-if="item.extra" class="db-card__extra mono" :title="JSON.stringify(item.extra)">
          {{ JSON.stringify(item.extra) }}
        </p>

        <p v-if="item.blacklist?.length" class="db-card__blacklist" :title="item.blacklist.join('、')">
          不采集：{{ item.blacklist.join('、') }}
        </p>

        <footer>
          <el-button
            link
            type="primary"
            :loading="collectingId === item.id"
            @click="collect(item)"
          >
            <AppIcon name="collect" :size="14" />
            <span>{{ item.collected ? '重新采集' : '开始采集' }}</span>
          </el-button>
          <el-button link type="primary" @click="openTables(item)">
            <span>查看表与字段</span>
            <AppIcon name="arrow-right" :size="14" />
          </el-button>
          <el-button link type="primary" @click="openBlacklist(item)">
            <span>采集黑名单</span>
          </el-button>
          <el-button
            v-if="!item.collected"
            link
            type="primary"
            @click="openEdit(item)"
          >
            <AppIcon name="edit" :size="14" />
            <span>编辑</span>
          </el-button>
          <span v-else class="muted-12 db-card__locked" title="已采集的库登记锁死，只有采集黑名单还能改">
            已锁定
          </span>
          <el-button
            link
            type="danger"
            :loading="deletingId === item.id"
            @click="remove(item)"
          >
            <AppIcon name="trash" :size="14" />
            <span>删除</span>
          </el-button>
        </footer>
      </article>
    </div>

    <el-empty
      v-if="!loading && !filtered.length"
      class="empty-state"
      :description="emptyText"
      :image-size="112"
    />

    <el-dialog
      v-model="dialogOpen"
      :title="dialogMode === 'create' ? '新增业务库' : '修改业务库'"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="form-stack">
        <label class="field">
          <span class="field__label">名称</span>
          <el-input v-model="form.name" maxlength="128" show-word-limit placeholder="如 交易库" />
        </label>

        <label class="field">
          <span class="field__label">
            连接地址{{ dialogMode === 'edit' ? '（留空表示不修改）' : '' }}
          </span>
          <el-input
            v-model="form.url"
            maxlength="255"
            placeholder="mysql://user:password@host:3306/dbname"
          />
        </label>

        <label class="field">
          <span class="field__label">数据库类型</span>
          <el-select v-model="form.type">
            <el-option label="MySQL" value="mysql" />
          </el-select>
        </label>

        <label class="field">
          <span class="field__label">附加配置（JSON，整组替换，留空清空）</span>
          <el-input
            v-model="form.extra"
            type="textarea"
            :rows="4"
            resize="none"
            placeholder='如 {"charset": "utf8mb4"}'
          />
        </label>

        <p class="muted-12">
          编号由服务端生成；登记后是「未采集」，整库采集成功后置位，登记随之锁死、不能再改（采集黑名单除外）。
        </p>
      </div>

      <template #footer>
        <div class="dialog-foot">
          <el-button @click="dialogOpen = false">取消</el-button>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!editable"
            @click="save"
          >
            {{ dialogMode === 'create' ? '登记' : '保存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-drawer
      v-model="blacklistOpen"
      title="采集黑名单"
      size="480px"
      :close-on-click-modal="false"
    >
      <div v-if="blacklistTarget" class="form-stack">
        <div class="bl-head">
          <span class="cell-name">{{ blacklistTarget.name }}</span>
          <span class="tag" :class="blacklistTarget.collected ? 'tag--mint' : 'tag--neutral'">
            {{ blacklistTarget.collected ? '已采集' : '未采集' }}
          </span>
        </div>

        <p class="muted-12">
          整组替换、不做增量合并：要增删就取当前值改完整体提交。表名是业务库里的名字（不是元数据库的表编号），
          落库前统一去首尾空白、去重，空列表存成 null 表示清空。已采集的库也允许改。
        </p>

        <p class="muted-12">
          在表与字段页删表时，服务端会把表名自动追加到这里（避免巡检 / 采集又把它们采回来）；黑名单只增不减，
          要恢复采集就在这里把表名去掉、整组提交。
        </p>

        <label class="field">
          <span class="field__label">不参加采集的表（可搜索业务库里实际存在的表）</span>
          <el-select
            v-model="blacklistTables"
            multiple
            filterable
            remote
            allow-create
            default-first-option
            :reserve-keyword="false"
            :remote-method="searchBlacklistTables"
            :loading="loadingBlacklistOptions"
            placeholder="按表名搜索，或直接输入表名回车新增"
            @focus="searchBlacklistTables('')"
          >
            <el-option v-for="name in blacklistOptions" :key="name" :label="name" :value="name" />
          </el-select>
        </label>

        <div class="bl-actions">
          <span class="muted-12">已选 {{ blacklistTables.length }} 张表</span>
          <el-button
            v-if="blacklistTables.length"
            link
            type="primary"
            @click="blacklistTables = []"
          >
            清空
          </el-button>
        </div>

        <p class="muted-12">
          影响范围只有后续的采集与巡检，不动已采下的登记与向量：采集时黑名单里的表不分析、不向量化；
          巡检时它们不算「新增」，已登记的会落到「已删除」，连同登记与向量一起清掉。定时巡检默认关闭，
          打开后才按周期比对结构。
        </p>
      </div>

      <template #footer>
        <div class="dialog-foot">
          <el-button @click="blacklistOpen = false">取消</el-button>
          <el-button type="primary" :loading="savingBlacklist" @click="saveBlacklist">
            保存黑名单
          </el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.db-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
  min-height: 80px;
}

.db-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 20px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--canvas);
  transition:
    border-color 160ms ease,
    background-color 160ms ease;
}

.db-card:hover {
  background: var(--surface-soft);
  border-color: var(--hairline-strong);
}

.db-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--accent);
}

.db-card__name {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 600;
  color: var(--ink);
}

.db-card__id {
  display: flex;
  align-items: center;
  gap: 8px;
}

.db-card__id .mono {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.db-card__extra,
.db-card__blacklist {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.db-card__blacklist {
  font-size: 12px;
  color: var(--muted);
}

.db-card footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  padding-top: 12px;
  border-top: 1px solid var(--hairline);
}

/* 删除靠右：与前面的操作拉开距离，避免误点 */
.db-card footer > .el-button:last-child {
  margin-left: auto;
}

.db-card__locked {
  padding: 0 8px;
}

.bl-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--hairline);
}

.bl-actions {
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
</style>
