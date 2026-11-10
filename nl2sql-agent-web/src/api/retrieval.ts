import { buildPath, del, get, parseJSONPreservingIds, stream } from '@/plugins/request'
import { toItem, toList } from '@/api/shape'
import type { TableRole } from '@/types/modeling'
import type {
  RetrievalMetric,
  RetrievalQuestion,
  RetrievalSession,
  RetrievalStep,
  RetrievalTable,
  RetrievalToken,
  SessionDetail,
  SessionTurn,
  TurnOutputs,
} from '@/types/retrieval'

/**
 * 检索问答：提问 → SQL → 执行，整条链路的进度以 SSE 逐帧回来。
 * 帧是智能体裁剪过的进度快照（消息 + 指标 / 表 / SQL / 结果 / Token / 耗时），
 * 没有节点状态，也没有字段级上下文。
 */

function toNumber(raw: unknown): number {
  if (typeof raw === 'number') return raw
  if (typeof raw === 'string' && raw.trim()) return Number(raw)
  return 0
}

function toId(raw: unknown): string | null {
  return raw === undefined || raw === null ? null : String(raw)
}

function toStringList(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === 'string') : []
}

/** 累计 Token 用量；缺字段或不是对象时按 null（该帧没带） */
function toToken(raw: unknown): RetrievalToken | null {
  if (raw === null || typeof raw !== 'object') return null
  const record = raw as Record<string, unknown>
  return {
    prompt: toNumber(record.prompt),
    completion: toNumber(record.completion),
    total: toNumber(record.total),
  }
}

function toMetrics(raw: unknown): RetrievalMetric[] {
  if (!Array.isArray(raw)) return []

  return raw
    .map(item => {
      const record = item !== null && typeof item === 'object' ? (item as Record<string, unknown>) : {}
      return {
        name: typeof record.name === 'string' ? record.name : '',
        description: typeof record.description === 'string' ? record.description : null,
        alias: Array.isArray(record.alias) ? record.alias.map(String) : null,
      }
    })
    .filter(item => Boolean(item.name))
}

function toRole(raw: unknown): TableRole | null {
  return raw === 'fact' || raw === 'dim' ? raw : null
}

function toTables(raw: unknown): RetrievalTable[] {
  if (!Array.isArray(raw)) return []

  return raw
    .map(item => {
      const record = item !== null && typeof item === 'object' ? (item as Record<string, unknown>) : {}
      return {
        id: toId(record.id) ?? '',
        name: typeof record.name === 'string' ? record.name : '',
        role: toRole(record.role),
        description: typeof record.description === 'string' ? record.description : null,
      }
    })
    .filter(item => Boolean(item.name))
}

/** SSE 帧只做宽松归一：缺字段按空值处理 */
function toStep(raw: unknown): RetrievalStep {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}

  return {
    messages: toStringList(record.messages),
    current_message: typeof record.current_message === 'string' ? record.current_message : '',
    error: typeof record.error === 'string' ? record.error : null,
    metrics: toMetrics(record.metrics),
    tables: toTables(record.tables),
    sql: typeof record.sql === 'string' ? record.sql : null,
    data: record.data ?? null,
    tokens: toToken(record.tokens),
    elapsed: toNumber(record.elapsed),
    session_id: toId(record.session_id),
  }
}

export interface AskOptions {
  /** 中断这条流 */
  signal?: AbortSignal
  /** 新会话的编号随响应头 `X-Session-Id` 先回来，读第一帧之前就能拿到 */
  onSession?: (session_id: string) => void
}

/**
 * 提问：跑一轮检索，每收到一帧就回调一次。
 * 流正常关闭、或 `signal` 中断即返回；整轮失败不以 HTTP 错误码体现，
 * 而是以 `error` 非空的帧收尾（参数校验失败才是 `422` JSON 错误体，
 * `session_id` 查不到时在开流之前返回 `404`）。
 */
export function askQuestion(
  payload: RetrievalQuestion,
  onStep: (step: RetrievalStep) => void,
  options: AskOptions = {},
): Promise<void> {
  return stream(
    buildPath('retrieval'),
    payload,
    data => {
      try {
        // 帧里的 session_id 是雪花 ID，必须按字符串保留，否则回传的是被取整的编号
        onStep(toStep(parseJSONPreservingIds(data)))
      } catch {
        // 单帧不是合法 JSON 就跳过，不打断整条流
      }
    },
    {
      rawIntegerFields: ['database_id', 'session_id'],
      signal: options.signal,
      onResponse: response => {
        const id = response.headers.get('X-Session-Id')
        if (id) options.onSession?.(id)
      },
    },
  )
}

/** 列出会话：按编号倒序，新的在前；轮次明细不在列表里 */
export async function listSessions(limit = 100): Promise<RetrievalSession[]> {
  const rows = toList<Record<string, unknown>>(
    await get<unknown>(buildPath('retrieval', 'session'), { limit }),
  )

  return rows.map(row => ({
    id: String(row.id ?? ''),
    thread_id: toId(row.thread_id),
    created_at: typeof row.created_at === 'string' ? row.created_at : '',
    turns: toNumber(row.turns),
    last_turn_at: typeof row.last_turn_at === 'string' ? row.last_turn_at : null,
  }))
}

/** 会话详情的轮次产出：整轮结束时整体写回，缺字段按空值处理 */
function toOutputs(raw: unknown): TurnOutputs {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}

  return {
    messages: toStringList(record.messages),
    elapsed: toNumber(record.elapsed),
    sql: typeof record.sql === 'string' ? record.sql : null,
    data: record.data ?? null,
    tables: toTables(record.tables),
  }
}

function toSessionTurn(raw: unknown): SessionTurn {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}

  return {
    id: toId(record.id) ?? '',
    round: toNumber(record.round),
    created_at: typeof record.created_at === 'string' ? record.created_at : '',
    input: typeof record.input === 'string' ? record.input : '',
    tokens: toNumber(record.tokens),
    outputs: toOutputs(record.outputs),
  }
}

/**
 * 会话详情：会话本身 + 名下全部轮次（按 `round` 升序）。
 * 产出与用量是整轮结束时整体写回的，正在跑的那一轮 `outputs` 还是空对象。
 */
export async function getSessionDetail(session_id: string): Promise<SessionDetail> {
  const record = toItem<Record<string, unknown>>(
    await get<unknown>(buildPath('retrieval', 'session', session_id)),
  )

  return {
    id: toId(record.id) ?? '',
    thread_id: toId(record.thread_id),
    created_at: typeof record.created_at === 'string' ? record.created_at : '',
    tokens: toNumber(record.tokens),
    turns: Array.isArray(record.turns) ? record.turns.map(toSessionTurn) : [],
  }
}

/** 删除会话：先清检索记忆，再删登记；记忆清不干净时返回 503、登记原样保留 */
export function deleteSession(session_id: string): Promise<void> {
  return del<void>(buildPath('retrieval', 'session', session_id))
}
