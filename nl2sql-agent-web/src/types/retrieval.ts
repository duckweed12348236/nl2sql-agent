import type { TableRole } from '@/types/modeling'

/**
 * 检索问答接口的数据结构（见 api_docs.md「检索问答」）。
 *
 * 链路是调度型智能体：主智能体把活派给指标口径 / 找表 / 写 SQL 三个子智能体，
 * 图每跑完一步产出一帧。帧是**裁剪过的进度快照**——只有 model 说的话与几样业务字段，
 * 没有节点状态、也没有字段级上下文，故这里不做「第几帧是生成 SQL」的假设。
 */

/** 到该帧为止的累计 Token 用量 */
export interface RetrievalToken {
  prompt: number
  completion: number
  total: number
}

/** 定稿的指标口径（不含编号） */
export interface RetrievalMetric {
  name: string
  description: string | null
  alias: string[] | null
}

/** 最终选中的表（不含字段与取值） */
export interface RetrievalTable {
  id: string
  name: string
  role: TableRole | null
  description: string | null
}

/** 一条链路上报（SSE 一帧）；各业务字段都是「算到现在的最新值」，整体替换即可 */
export interface RetrievalStep {
  /** 本轮到该帧为止的全部模型消息文本（子智能体回话 + 主智能体答复） */
  messages: string[]
  /** `messages` 的最后一条，可直接当「当前进度」显示 */
  current_message: string
  /** 最后一次失败的原因，成功时清空 */
  error: string | null
  metrics: RetrievalMetric[]
  tables: RetrievalTable[]
  /** 通过校验的那条语句；纠错时会被清空 */
  sql: string | null
  /** 执行成功的结果集，没执行或失败时为 null */
  data: unknown
  /** 到该帧为止的累计 Token 用量 */
  tokens: RetrievalToken | null
  /** 到该帧为止的耗时（秒） */
  elapsed: number
  session_id: string | null
}

/** 提问入参 */
export interface RetrievalQuestion {
  /** 待查询的自然语言问题；接着上一轮追问或回答反问时也走它 */
  content: string
  /** 本次检索的业务库编号（雪花 ID），不跨库 */
  database_id: string
  /** 会话编号：不带即开新会话；带上已有会话的编号即这一会话的下一轮 */
  session_id?: string | null
}

/**
 * 一轮问答的渲染模型：历史轮次由会话详情（`GET /retrieval/session/{id}`）的 `outputs` 归纳而来
 * （服务端落模型消息 / 耗时 / SQL / 结果 / 表，不落指标口径与失败原因），
 * 正在跑的那一轮由 SSE 帧在本地拼出来，两者同构、共用同一个轮次卡片。
 */
export interface TurnRecord {
  /** 本地轮次编号（服务端不给轮次编号之外的标识用途） */
  id: string
  /** 所属会话编号；提交失败、还没拿到会话时可能为空 */
  session_id: string
  /** 用户输入：问题本身，或接着上一轮说的话 */
  content: string
  /** 模型消息（整轮的可读过程） */
  messages: string[]
  metrics: RetrievalMetric[]
  tables: RetrievalTable[]
  /** 最终 SQL */
  sql: string
  /** 查询结果原样 */
  data: unknown
  /** 整轮失败原因（最后一帧的 `error`） */
  failure: string
  /** 反问语句：以「需要澄清：」开头的那句，有值说明这一轮停在这里等补充 */
  clarification: string
  /** 累计 Token 用量 */
  token: RetrievalToken | null
  /** 整轮耗时（秒） */
  elapsed: number
  /** 开始时间（ISO） */
  started_at: string
}

/** 会话详情里某一轮的产出（整轮结束时整体写回，正在跑的那一轮还是空对象） */
export interface TurnOutputs {
  /** 模型输出的文本，按产生顺序（同 SSE 帧的 `messages`），可能为空数组 */
  messages: string[]
  /** 这一轮的耗时（秒） */
  elapsed: number
  /** 通过校验的那条语句 */
  sql: string | null
  /** SQL 执行结果集 */
  data: unknown
  /** 最终选中的表 */
  tables: RetrievalTable[]
}

/** 会话详情里的一轮（产出与用量在整轮结束时整体写回，跑着的那一轮 outputs 还是空对象） */
export interface SessionTurn {
  /** 轮次编号（雪花 ID） */
  id: string
  /** 轮次序号，同一会话内从 1 开始 */
  round: number
  created_at: string
  /** 用户这一轮的原始输入 */
  input: string
  /** 这一轮的 Token 用量（取最后一帧的 `tokens.total`） */
  tokens: number
  outputs: TurnOutputs
}

/** GET /retrieval/session/{session_id} 的出参 */
export interface SessionDetail {
  id: string
  thread_id: string | null
  created_at: string
  /** 会话累计 Token 用量（各轮之和） */
  tokens: number
  turns: SessionTurn[]
}

/** 会话列表里的一条（GET /retrieval/session，不含轮次明细） */
export interface RetrievalSession {
  /** 本系统的会话编号（雪花 ID） */
  id: string
  /** 智能体那边的会话编号，服务端内部使用 */
  thread_id: string | null
  created_at: string
  /** 这条会话下的轮次数 */
  turns: number
  /** 最近一轮的创建时间，还没有轮次时为 null */
  last_turn_at: string | null
}
