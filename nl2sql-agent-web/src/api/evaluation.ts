import { toItem, toList } from '@/api/shape'
import { buildPath, del, get, post } from '@/plugins/request'
import type {
  CreateDatasetResult,
  DatasetBrief,
  DatasetCreate,
  DatasetEvaluation,
  DatasetPayload,
  EvaluateDatasetResult,
  Step,
  TaskInputs,
  TaskOutputs,
  TaskSnapshot,
  TaskType,
} from '@/types/dataset'
import type { Table } from '@/types/modeling'

/** 出题与评测都是后台任务：投递只返回任务编号，进度靠 listTasks 轮询 */

function toStep(raw: unknown): Step {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const status = record.status

  return {
    message: typeof record.message === 'string' ? record.message : '',
    status:
      status === 'success' || status === 'failed' || status === 'error' ? status : 'running',
    error: typeof record.error === 'string' ? record.error : null,
    dataset_id:
      record.dataset_id === undefined || record.dataset_id === null
        ? null
        : String(record.dataset_id),
  }
}

function toType(raw: unknown): TaskType | '' {
  return raw === 'create_dataset' || raw === 'evaluate_dataset' || raw === 'collect_database'
    ? raw
    : ''
}

/** 雪花 ID 字段统一归一为字符串 */
function toId(raw: unknown): string | null {
  return raw === undefined || raw === null ? null : String(raw)
}

function toOutputs(raw: unknown): TaskOutputs {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}

  return {
    dataset_id: toId(record.dataset_id),
    result_id: toId(record.result_id),
    mean: typeof record.mean === 'number' ? record.mean : null,
    database_id: toId(record.database_id),
    collected: typeof record.collected === 'boolean' ? record.collected : null,
  }
}

function toInputs(raw: unknown): TaskInputs {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}

  return {
    thread_id: toId(record.thread_id),
    database_id: toId(record.database_id),
    table_ids: Array.isArray(record.table_ids) ? record.table_ids.map(String) : [],
    size: typeof record.size === 'number' ? record.size : null,
    dataset_id: toId(record.dataset_id),
  }
}

/** 任务记录：类型与入参、输出都在顶层，进度帧在 `outputs.steps` 里 */
function toTask(raw: unknown): TaskSnapshot {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const outputs =
    record.outputs !== null && typeof record.outputs === 'object'
      ? (record.outputs as Record<string, unknown>)
      : {}
  const status = record.status

  return {
    id: toId(record.id) ?? '',
    user_id: typeof record.user_id === 'number' ? record.user_id : 0,
    type: toType(record.type),
    status: status === 'success' || status === 'error' ? status : 'running',
    error: typeof record.error === 'string' ? record.error : null,
    inputs: toInputs(record.inputs),
    steps: Array.isArray(outputs.steps) ? outputs.steps.map(toStep) : [],
    outputs: toOutputs(outputs),
  }
}

/** 列出数据集，新数据集在前 */
export async function listDatasets(): Promise<DatasetBrief[]> {
  const rows = toList<Record<string, unknown>>(
    await get<unknown>(buildPath('evaluation', 'dataset')),
  )

  return rows.map(row => ({
    id: String(row.id ?? ''),
    name: typeof row.name === 'string' ? row.name : '',
  }))
}

/** 查看数据集：本体、条目与历次评测结果 */
export async function getDataset(dataset_id: string): Promise<DatasetPayload> {
  return toItem<DatasetPayload>(await get<unknown>(buildPath('evaluation', 'dataset', dataset_id)))
}

/** 删除数据集：条目与历次评测结果级联删除；检索侧的会话记录属于另一条业务线，不受影响 */
export function deleteDataset(dataset_id: string): Promise<void> {
  return del<void>(buildPath('evaluation', 'dataset', dataset_id))
}

/** 列出某业务库下的表（供出题选表），`name` 为表名关键字、子串匹配 */
export async function listDatasetTables(database_id: string, name = ''): Promise<Table[]> {
  const keyword = name.trim()

  return toList<Table>(
    await get<unknown>(buildPath('evaluation', 'table'), {
      database_id,
      name: keyword || undefined,
    }),
  )
}

/** 投递出题任务（后台跑，立即返回任务编号与会话编号） */
export async function createDataset(payload: DatasetCreate): Promise<CreateDatasetResult> {
  const record = toItem<Record<string, unknown>>(
    await post<unknown>(buildPath('evaluation', 'dataset'), payload, {
      rawIntegerFields: ['database_id'],
    }),
  )

  return {
    task_id: toId(record.task_id) ?? '',
    thread_id: toId(record.thread_id) ?? '',
  }
}

/** 投递评测任务（后台跑，立即返回任务编号） */
export async function evaluateDataset(payload: DatasetEvaluation): Promise<EvaluateDatasetResult> {
  const record = toItem<Record<string, unknown>>(
    await post<unknown>(buildPath('evaluation'), payload, {
      rawIntegerFields: ['dataset_id', 'database_id'],
    }),
  )

  return { task_id: toId(record.task_id) ?? '' }
}

/** 轮询任务进度：当前用户最近的任务，新的在前（最多 20 条，记录最多留 3 天） */
export async function listTasks(): Promise<TaskSnapshot[]> {
  return toList<unknown>(await get<unknown>(buildPath('evaluation', 'task'))).map(toTask)
}
