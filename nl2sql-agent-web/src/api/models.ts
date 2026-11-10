import { buildPath, del, get, post, put } from '@/plugins/request'
import { toItem, toList } from '@/api/shape'
import type { ModelConfig, ModelCreate, ModelType, ModelUpdate } from '@/types/model'

/**
 * 模型管理接口（`controllers/models.py`，前缀 `/model`）。
 * 三类模型（对话 / 嵌入 / 重排）共用一套登记；写操作之后服务端会清实例缓存，改完即时生效。
 */

const PREFIX = 'model'

function toOptionalString(raw: unknown): string | null {
  return typeof raw === 'string' ? raw : null
}

function toNumber(raw: unknown): number | null {
  if (typeof raw === 'number') return raw
  if (typeof raw === 'string' && raw.trim()) return Number(raw)
  return null
}

function toExtra(raw: unknown): Record<string, unknown> | null {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null
  return raw as Record<string, unknown>
}

function toType(raw: unknown): ModelType {
  return raw === 'embedding' || raw === 'rerank' ? raw : 'chat'
}

/** 响应体逐字段归一：`api_key` 明文返回，其余缺字段按空值处理 */
function toModel(raw: Record<string, unknown>): ModelConfig {
  return {
    id: String(raw.id ?? ''),
    name: typeof raw.name === 'string' ? raw.name : '',
    type: toType(raw.type),
    base_url: toOptionalString(raw.base_url),
    model_name: typeof raw.model_name === 'string' ? raw.model_name : '',
    api_key: toOptionalString(raw.api_key),
    dimension: toNumber(raw.dimension),
    instruct_field: toOptionalString(raw.instruct_field),
    extra: toExtra(raw.extra),
    enabled: raw.enabled !== false,
    is_default: raw.is_default === true,
    description: toOptionalString(raw.description),
  }
}

/** 列出模型：按用途排序（chat → embedding → rerank），同用途内新登记的在前 */
export async function listModels(
  params: { type?: ModelType; enabled?: boolean } = {},
): Promise<ModelConfig[]> {
  const rows = toList<Record<string, unknown>>(await get<unknown>(buildPath(PREFIX), params))
  return rows.map(toModel)
}

/** 查看一条模型登记 */
export async function getModel(model_id: string): Promise<ModelConfig> {
  return toModel(toItem<Record<string, unknown>>(await get<unknown>(buildPath(PREFIX, model_id))))
}

/** 新增模型登记（embedding 必须带 dimension） */
export async function createModel(payload: ModelCreate): Promise<ModelConfig> {
  return toModel(
    toItem<Record<string, unknown>>(await post<unknown>(buildPath(PREFIX), payload)),
  )
}

/** 修改模型登记：未提交的字段保持原值 */
export async function updateModel(model_id: string, payload: ModelUpdate): Promise<ModelConfig> {
  return toModel(
    toItem<Record<string, unknown>>(await put<unknown>(buildPath(PREFIX, model_id), payload)),
  )
}

/** 设为该用途的默认模型：同用途其他默认一并清掉，启用状态不在这里校验 */
export async function setModelDefault(model_id: string): Promise<ModelConfig> {
  return toModel(
    toItem<Record<string, unknown>>(
      await put<unknown>(buildPath(PREFIX, model_id, 'default'), {}),
    ),
  )
}

/** 删除模型登记：只删登记，不动别的；删的正是默认时该用途可能暂时没有默认 */
export function deleteModel(model_id: string): Promise<void> {
  return del<void>(buildPath(PREFIX, model_id))
}
