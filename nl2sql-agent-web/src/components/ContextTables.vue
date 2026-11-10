<script setup lang="ts">
import { computed, ref } from 'vue'
import { columnRoleLabel, columnRoleTag, tableRoleLabel, tableRoleTag } from '@/utils/roles'
import type { ContextTable } from '@/types/dataset'

const props = defineProps<{
  /** 库上下文（表 / 字段 / 取值），可能缺省 */
  contexts: ContextTable[] | null
  /** 检索结果是逐条问题依次排开、不去重的，用序号区分同名的表 */
  numbered?: boolean
  /** 默认展开第一条 */
  defaultOpen?: boolean
}>()

const tables = computed<ContextTable[]>(() => props.contexts ?? [])
const open = ref<number[]>(props.defaultOpen && tables.value.length ? [0] : [])
</script>

<template>
  <p v-if="!tables.length" class="muted-12">没有留下表上下文。</p>

  <el-collapse v-else v-model="open">
    <el-collapse-item v-for="(table, index) in tables" :key="index" :name="index">
      <template #title>
        <div class="row-head">
          <span v-if="numbered" class="mono">#{{ index + 1 }}</span>
          <span class="cell-name">{{ table.name }}</span>
          <span class="tag" :class="tableRoleTag(table.role)">{{ tableRoleLabel(table.role) }}</span>
          <span class="muted-12">{{ table.columns.length }} 个字段</span>
        </div>
      </template>

      <p v-if="table.description" class="description">{{ table.description }}</p>

      <el-table :data="table.columns" row-key="id" size="small">
        <el-table-column label="字段名" min-width="140">
          <template #default="{ row }">
            <span class="cell-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" min-width="130">
          <template #default="{ row }">
            <span class="mono">{{ row.type ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="90">
          <template #default="{ row }">
            <span class="tag" :class="columnRoleTag(row.role)">{{ columnRoleLabel(row.role) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="别名" min-width="150">
          <template #default="{ row }">
            <div v-if="row.alias?.length" class="tag-list">
              <span v-for="alias in row.alias" :key="alias" class="tag tag--neutral">
                {{ alias }}
              </span>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="关联字段" min-width="170">
          <template #default="{ row }">
            <div v-if="row.related_columns?.length" class="tag-list">
              <span v-for="related in row.related_columns" :key="related" class="tag tag--sky">
                {{ related }}
              </span>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="取值" min-width="200">
          <template #default="{ row }">
            <div v-if="row.values?.length" class="tag-list">
              <span v-for="value in row.values" :key="value" class="tag tag--neutral">
                {{ value }}
              </span>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.row-head {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  overflow: hidden;
}

.description {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text);
  white-space: pre-wrap;
}
</style>
