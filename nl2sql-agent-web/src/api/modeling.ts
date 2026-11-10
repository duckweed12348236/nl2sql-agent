import { buildPath, del, get, post, put, request } from '@/plugins/request'
import { toItem, toList } from '@/api/shape'
import type {
  Column,
  ColumnUpdate,
  Database,
  DatabaseCreate,
  DatabaseUpdate,
  Metric,
  MetricCreate,
  MetricUpdate,
  Relation,
  RelationCreate,
  RelationUpdate,
  Table,
  TableBatchDeleteResult,
  TableUpdate,
} from '@/types/modeling'

/** 元数据管理接口前缀（`controllers/collection.py`） */
const PREFIX = 'collection'

/** 列出已登记的业务库，新登记的在前 */
export async function listDatabases(): Promise<Database[]> {
  return toList<Database>(await get<unknown>(buildPath(PREFIX, 'database')))
}

/** 新增业务库；编号由服务端生成，`collected` 从「未采集」开始 */
export async function createDatabase(payload: DatabaseCreate): Promise<Database> {
  return toItem<Database>(await post<unknown>(buildPath(PREFIX, 'database'), payload))
}

/**
 * 修改业务库；已采集的库登记锁死，除黑名单外的字段都会拿到 409。
 * 黑名单请走 `setDatabaseBlacklist`（专用入口，已采集的库也能改）。
 */
export async function updateDatabase(
  database_id: string,
  payload: DatabaseUpdate,
): Promise<Database> {
  return toItem<Database>(
    await put<unknown>(buildPath(PREFIX, 'database', database_id), payload),
  )
}

/** 设置采集黑名单：整组替换、不做增量合并，空列表表示清空 */
export async function setDatabaseBlacklist(
  database_id: string,
  tables: string[],
): Promise<Database> {
  return toItem<Database>(
    await put<unknown>(buildPath(PREFIX, 'database', database_id, 'blacklist'), { tables }),
  )
}

/** 删除业务库：连同表、字段、指标与向量集合一起删 */
export function deleteDatabase(database_id: string): Promise<void> {
  return del<void>(buildPath(PREFIX, 'database', database_id))
}

/** 发起整库采集（后台任务），返回任务编号；进度轮询 `GET /evaluation/task` */
export async function collectDatabase(database_id: string): Promise<string> {
  const record = toItem<Record<string, unknown>>(
    await post<unknown>(buildPath(PREFIX, 'database', database_id, 'collect')),
  )

  return record.task_id === undefined || record.task_id === null ? '' : String(record.task_id)
}

/**
 * 列出业务库里**实际存在**的表名（读业务库的 `information_schema`，不是已登记的表），
 * 供挑选采集黑名单用；`name` 为表名关键字、不区分大小写的子串匹配。
 */
export async function listDatabaseTables(
  database_id: string,
  name?: string,
): Promise<string[]> {
  return toList<string>(
    await get<unknown>(buildPath(PREFIX, 'database', database_id, 'tables'), { name }),
  )
}

/** 列出某业务库下已登记的表，`name` 为表名关键字、子串匹配；按表名排序 */
export async function listTables(database_id: string, name?: string): Promise<Table[]> {
  return toList<Table>(await get<unknown>(buildPath(PREFIX, 'table'), { database_id, name }))
}

/** 修改表：不涉及向量库，但改动描述会让该表字段的向量文本过期 */
export async function updateTable(table_id: string, payload: TableUpdate): Promise<Table> {
  return toItem<Table>(await put<unknown>(buildPath(PREFIX, 'table', table_id), payload))
}

/** 删除表：字段、关联与向量一起清掉，业务库里的表本身不动 */
export function deleteTable(table_id: string): Promise<void> {
  return del<void>(buildPath(PREFIX, 'table', table_id))
}

/**
 * 批量删除表（1–200 个，可跨库，重复的自动去重）：
 * 一次事务删完登记、再按库清向量；查不到的编号落在 `missing`、不影响其余表。
 */
export async function deleteTables(table_ids: string[]): Promise<TableBatchDeleteResult> {
  const record = toItem<Record<string, unknown>>(
    await request<unknown>('DELETE', buildPath(PREFIX, 'table'), { table_ids }),
  )

  return {
    deleted: Array.isArray(record.deleted) ? record.deleted.map(String) : [],
    missing: Array.isArray(record.missing) ? record.missing.map(String) : [],
  }
}

/** 列出某张表下的字段 */
export async function listColumns(table_id: string): Promise<Column[]> {
  return toList<Column>(await get<unknown>(buildPath(PREFIX, 'column'), { table_id }))
}

/** 修改字段：会同步向量库，并清掉挂在这个字段上的关联 */
export async function updateColumn(column_id: string, payload: ColumnUpdate): Promise<Column> {
  return toItem<Column>(await put<unknown>(buildPath(PREFIX, 'column', column_id), payload))
}

/** 列出字段间的关联；`column_id` 更具体，两个都给时以它为准 */
export async function listRelations(
  params: { column_id?: string; table_id?: string } = {},
): Promise<Relation[]> {
  return toList<Relation>(await get<unknown>(buildPath(PREFIX, 'relation'), params))
}

/** 新增关联：一次写好两个方向，两端需已登记且同属一个业务库 */
export async function createRelation(payload: RelationCreate): Promise<Relation> {
  return toItem<Relation>(await post<unknown>(buildPath(PREFIX, 'relation'), payload))
}

/** 更新关联：按两端定位，把某一端换成另一个字段 */
export async function updateRelation(payload: RelationUpdate): Promise<Relation> {
  return toItem<Relation>(await put<unknown>(buildPath(PREFIX, 'relation'), payload))
}

/** 删除关联：两端一起删，避免留下半条关系 */
export function deleteRelation(source_id: string, target_id: string): Promise<void> {
  return del<void>(buildPath(PREFIX, 'relation'), { params: { source_id, target_id } })
}

/** 列出指标；`database_id` 不传则列出全部库的指标（含没有所属库的老指标） */
export async function listMetrics(database_id?: string): Promise<Metric[]> {
  return toList<Metric>(await get<unknown>(buildPath(PREFIX, 'metric'), { database_id }))
}

/** 新增指标：登记后立刻写进该库的指标向量集合 */
export async function createMetric(payload: MetricCreate): Promise<Metric> {
  return toItem<Metric>(
    await post<unknown>(buildPath(PREFIX, 'metric'), payload, {
      rawIntegerFields: ['database_id'],
    }),
  )
}

/** 修改指标：改名不牵动引用，三个可改字段都会重新向量化 */
export async function updateMetric(metric_id: string, payload: MetricUpdate): Promise<Metric> {
  return toItem<Metric>(await put<unknown>(buildPath(PREFIX, 'metric', metric_id), payload))
}

/** 删除指标，返回 204 无响应体 */
export function deleteMetric(metric_id: string): Promise<void> {
  return del<void>(buildPath(PREFIX, 'metric', metric_id))
}
