/**
 * 检索会话的本地记忆。
 *
 * 轮次明细由 `GET /retrieval/session/{session_id}` 提供，前端不再自己存一份；
 * 这里只记住「最后停留的会话与业务库」，刷新页面时据此回到原来的对话。
 * 浏览器不给写（隐私模式、配额满）时静默放弃，不影响问答。
 */

const ACTIVE_KEY = 'text2sql.retrieval.active.v1'

/** 最后停留的会话与业务库，刷新后据此回显 */
interface Active {
  session_id: string
  database_id: string
}

export function loadActive(): Active {
  try {
    const text = localStorage.getItem(ACTIVE_KEY)
    if (!text) return { session_id: '', database_id: '' }

    const parsed: unknown = JSON.parse(text)
    if (parsed === null || typeof parsed !== 'object') {
      return { session_id: '', database_id: '' }
    }

    const record = parsed as Partial<Active>
    return {
      session_id: typeof record.session_id === 'string' ? record.session_id : '',
      database_id: typeof record.database_id === 'string' ? record.database_id : '',
    }
  } catch {
    return { session_id: '', database_id: '' }
  }
}

export function saveActive(active: Active): void {
  try {
    localStorage.setItem(ACTIVE_KEY, JSON.stringify(active))
  } catch {
    // 写不进去就算了
  }
}
