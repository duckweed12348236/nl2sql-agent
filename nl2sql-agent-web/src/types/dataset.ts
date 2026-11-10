import type { ColumnRole, TableRole } from '@/types/modeling'

/** 任务进度帧的执行状态（注意与任务的 `status` 不是同一套取值） */
export type StepStatus = 'running' | 'success' | 'failed' | 'error'

/** 任务状态：不是 `running` 即代表本轮结束 */
export type TaskStatus = 'running' | 'success' | 'error'

/** 任务类型：出题 / 评测 / 整库采集 */
export type TaskType = 'create_dataset' | 'evaluate_dataset' | 'collect_database'

/**
 * 任务进度帧（`outputs.steps` 的元素），只有四个字段。
 * 与检索问答的帧不是同一套结构——那边推的是智能体的进度快照。
 */
export interface Step {
  message: string
  status: StepStatus
  error: string | null
  /** 数据集编号（雪花 ID）；出题阶段从「数据集已保存」那帧起带上 */
  dataset_id: string | null
}

/**
 * 任务输出：进度帧与产出同层（`steps` 之外是产出）。
 * 出题是数据集编号，评测是结果编号与等效率，采集是业务库编号与是否采集完成。
 */
export interface TaskOutputs {
  dataset_id: string | null
  result_id: string | null
  mean: number | null
  database_id: string | null
  collected: boolean | null
}

/** 投递时的入参：出题带 `table_ids` / `size`，评测带 `dataset_id`；两者都带 `thread_id`、`database_id` */
export interface TaskInputs {
  thread_id: string | null
  database_id: string | null
  table_ids: string[]
  size: number | null
  dataset_id: string | null
}

/** 任务快照（GET /evaluation/task 的元素，类型与入参、输出都在顶层） */
export interface TaskSnapshot {
  /** 任务编号（雪花 ID） */
  id: string
  user_id: number
  type: TaskType | ''
  status: TaskStatus
  /** 失败原因，仅 `status` 为 `error` 时有值 */
  error: string | null
  inputs: TaskInputs
  /** 到此刻为止的进度帧，取自 `outputs.steps`，全量返回、整体替换即可 */
  steps: Step[]
  outputs: TaskOutputs
}

/** POST /evaluation/dataset 的出参（202） */
export interface CreateDatasetResult {
  task_id: string
  /** 本轮会话编号，出题落库后记在数据集上 */
  thread_id: string
}

/** POST /evaluation 的出参（202） */
export interface EvaluateDatasetResult {
  task_id: string
}

/** 列表里的数据集 */
export interface DatasetBrief {
  id: string
  name: string
}

/** 库上下文字段：表 / 字段 / 取值 */
export interface ContextColumn {
  id: string
  name: string
  type: string | null
  description: string | null
  alias: string[] | null
  role: ColumnRole | null
  /** 采集到的取值（部分样本） */
  values: string[] | null
  /** 与这个字段关联的字段，形如 `<表名>.<字段名>`；没有时为空数组或 null */
  related_columns: string[] | null
}

/** 库上下文里的表 */
export interface ContextTable {
  id: string
  name: string
  role: TableRole | null
  description: string | null
  columns: ContextColumn[]
}

/** 数据集本体 */
export interface DatasetDetail {
  id: string
  /** 出题那一轮的会话编号，只作为来源标记 */
  thread_id: string | null
  name: string
  /** 出题与判定所用的库上下文（新建数据集时按 table_ids 落库） */
  reference_contexts: ContextTable[]
  /** 出题失败原因 */
  error: string | null
}

/** 问题与参考 SQL */
export interface DatasetItem {
  id: string
  question: string
  reference: string
  dataset_id: string
}

/** 逐条评测明细 */
export interface ResultItem {
  id: string
  reference: string
  /** 检索生成的 SQL，未生成或没通过校验时为 null */
  answer: string | null
  /** 等效 1.0 / 不等效或未生成 0.0 */
  score: number
  /** 得分理由或失败原因 */
  reason: string | null
  result_id: string
}

/** 一轮评测结果 */
export interface EvaluationResult {
  id: string
  /** 平均等效率 */
  mean: number | null
  /** 该轮的解读报告，Markdown 正文（见 api_docs.md「评测结果报告是 Markdown 正文」） */
  report: string | null
  /** 本轮检索智能体检索到的表上下文：逐条问题的依次排开，不去重 */
  retrieved_contexts: ContextTable[] | null
  /** 整轮失败原因 */
  error: string | null
  dataset_id: string
  items: ResultItem[]
}

/** GET /evaluation/dataset/{dataset_id} 的响应体 */
export interface DatasetPayload {
  dataset: DatasetDetail
  items: DatasetItem[]
  results: EvaluationResult[]
}

/** 新建数据集（出题）入参 */
export interface DatasetCreate {
  /** 出题所用的表编号（Table.id），1–10 个 */
  table_ids: string[]
  size: number
  /** 出题所用业务库编号（雪花 ID），请求层按整数原样输出 */
  database_id: string
}

/** 评估数据集入参 */
export interface DatasetEvaluation {
  dataset_id: string
  database_id: string
}
