/**
 * 响应形状归一化。
 *
 * 服务端有时会把结果包一层信封（如 `{ code, data }`）；这里统一解包并校验，
 * 形状对不上时抛出可读错误，避免把对象当成数组交给页面后报
 * `xxx.find is not a function` 这类难排查的问题。
 */

const LIST_KEYS = ['data', 'items', 'list', 'records', 'results', 'rows']
const ITEM_KEYS = ['data', 'result', 'item']

export function snippet(payload: unknown): string {
  if (typeof payload === 'string') return `"${payload.replace(/\s+/g, ' ').slice(0, 120)}"`
  try {
    return JSON.stringify(payload)?.slice(0, 160) ?? String(payload)
  } catch {
    return String(payload)
  }
}

/** 取列表：裸数组直接用，信封里的数组自动解包 */
export function toList<T>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[]

  if (payload !== null && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    for (const key of LIST_KEYS) {
      if (Array.isArray(record[key])) return record[key] as T[]
    }
  }

  throw new Error(`接口没有返回列表结构：${snippet(payload)}`)
}

/** 取单对象：裸对象直接用，信封里的对象自动解包；空响应体原样返回 */
export function toItem<T>(payload: unknown): T {
  if (payload === undefined) return payload as T

  let value = payload
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>
    for (const key of ITEM_KEYS) {
      const nested = record[key]
      if (nested !== null && typeof nested === 'object' && !Array.isArray(nested)) {
        value = nested
        break
      }
    }
  }

  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`接口没有返回对象结构：${snippet(payload)}`)
  }

  return value as T
}
