/**
 * 模型管理接口的数据结构（见 api_docs.md「模型管理」）。
 *
 * 三类模型（对话 / 嵌入 / 重排）登记在一张表里，靠 `type` 区分；
 * `api_key` 明文存库、响应里也明文返回（管理面要能复制走），编号是雪花 ID，按字符串保存。
 */

/** 模型用途 */
export type ModelType = 'chat' | 'embedding' | 'rerank'

/** 一条模型登记（响应结构固定，见 `controllers.models.model_dump`） */
export interface ModelConfig {
  /** 雪花 ID */
  id: string
  name: string
  type: ModelType
  /** chat / embedding 填 OpenAI 兼容端点的 base；rerank 填完整请求地址 */
  base_url: string | null
  /** 服务方的模型标识 */
  model_name: string
  /** 明文密钥，本地端点可为 null */
  api_key: string | null
  /** 向量维度，embedding 必填 */
  dimension: number | null
  /** 重排的任务说明字段名，留空表示不发 */
  instruct_field: string | null
  /** 其余请求参数（温度、max_tokens、超时等） */
  extra: Record<string, unknown> | null
  enabled: boolean
  /** 每种用途最多一条默认，保存时自动清掉同用途的其他默认 */
  is_default: boolean
  description: string | null
}

/** 新增模型登记 */
export interface ModelCreate {
  name: string
  type: ModelType
  model_name: string
  base_url?: string | null
  api_key?: string | null
  dimension?: number | null
  instruct_field?: string | null
  extra?: Record<string, unknown> | null
  enabled?: boolean
  is_default?: boolean
  description?: string | null
}

/** 修改模型登记：不提交即保持原值，`api_key` 提交 null / 空串即清空 */
export type ModelUpdate = Partial<ModelCreate>
