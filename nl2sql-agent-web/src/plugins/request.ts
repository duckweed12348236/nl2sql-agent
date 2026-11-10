/**
 * 通用请求工具：基于 fetch 封装 get / post / put / delete。
 *
 * 雪花 ID 是服务端生成的有符号 64 位整数，可能超出 JS 的安全整数范围（2^53 - 1）：
 * - 响应侧：先读原始文本，把超出安全范围的整数字面量整体加引号再 JSON.parse，
 *   ID 以字符串原样保留（直接走 `response.json()` 会被静默截断）；
 * - 请求侧：这些字段在后端是 int，用 `rawIntegerFields` 指明后按不带引号的整数输出。
 */

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ""

const INTEGER_PATTERN = /^-?\d+$/
const NUMBER_PATTERN = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y
const MAX_SAFE_INTEGER = BigInt(Number.MAX_SAFE_INTEGER)
const MIN_SAFE_INTEGER = BigInt(Number.MIN_SAFE_INTEGER)

export type QueryParams = Record<string, string | number | boolean | null | undefined>

export interface RequestOptions {
  /** 查询参数；值为 null / undefined 的键会被忽略 */
  params?: QueryParams
  /** 附加请求头，同名时覆盖默认值 */
  headers?: Record<string, string>
  /** 用于取消请求 */
  signal?: AbortSignal
  /**
   * 按整数原样序列化的字段名。
   * 雪花 ID 在前端以字符串保存（避免精度丢失），但后端这些字段声明为 int，
   * 传字符串会走类型转换，这里直接把引号去掉输出成 JSON 整数。
   */
  rawIntegerFields?: string[]
  /**
   * 2xx 响应头到手时立刻回调一次（流式请求用它读 `X-Session-Id` 这类响应头，
   * 此时帧还没开始读）。
   */
  onResponse?: (response: Response) => void
}

/** 非 2xx 响应统一抛出该错误，`payload` 为解析后的响应体 */
export class RequestError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(status: number, message: string, payload?: unknown) {
    super(message)
    this.name = "RequestError"
    this.status = status
    this.payload = payload
  }
}

function isUnsafeInteger(literal: string): boolean {
  if (!INTEGER_PATTERN.test(literal)) return false
  const value = BigInt(literal)
  return value > MAX_SAFE_INTEGER || value < MIN_SAFE_INTEGER
}

/** 扫描 JSON 文本，把超出安全范围的整数字面量加引号，字符串内的数字不受影响 */
function quoteBigIntegers(text: string): string {
  let output = ""
  let index = 0
  let inString = false

  while (index < text.length) {
    const char = text.charAt(index)

    if (inString) {
      if (char === "\\") {
        output += text.slice(index, index + 2)
        index += 2
        continue
      }
      if (char === "\"") inString = false
      output += char
      index += 1
      continue
    }

    if (char === "\"") {
      inString = true
      output += char
      index += 1
      continue
    }

    if (char === "-" || (char >= "0" && char <= "9")) {
      NUMBER_PATTERN.lastIndex = index
      const matched = NUMBER_PATTERN.exec(text)
      if (matched) {
        const [literal = ""] = matched
        output += isUnsafeInteger(literal) ? `"${literal}"` : literal
        index = NUMBER_PATTERN.lastIndex
        continue
      }
    }

    output += char
    index += 1
  }

  return output
}

/**
 * 解析 JSON 文本，超出安全范围的整数按字符串保留。
 * 响应体之外，SSE 的单帧也要走这里：帧里的 `session_id` 就是雪花 ID，
 * 直接 `JSON.parse` 会被静默取整，再回传就成了另一个编号（2026-10 出现的
 * 「第二轮次对话报会话不存在」即此故）。
 */
export function parseJSONPreservingIds(text: string): unknown {
  return JSON.parse(quoteBigIntegers(text))
}

/** JSON 解析失败的标记，用于区分「空响应体」与「不是 JSON」 */
const PARSE_FAILED = Symbol("json-parse-failed")

function parsePayload(text: string): unknown {
  try {
    return parseJSONPreservingIds(text)
  } catch {
    return PARSE_FAILED
  }
}

function clip(text: string): string {
  return text.replace(/\s+/g, " ").trim().slice(0, 160)
}

/** 判断响应体是不是被前端的开发服务器 / 代理用 HTML 接管了 */
function looksLikeHTML(response: Response, text: string): boolean {
  const contentType = response.headers.get("content-type") ?? ""
  if (contentType.includes("text/html")) return true
  const head = text.trim().slice(0, 40).toLowerCase()
  return head.startsWith("<!doctype html") || head.startsWith("<html")
}

function describeDetail(item: unknown): string {
  if (item === null || typeof item !== "object") return item === undefined ? "" : String(item)

  const {msg, loc} = item as { msg?: unknown; loc?: unknown }
  const message = typeof msg === "string" ? msg : ""
  const location = Array.isArray(loc) ? loc.filter((part) => part !== "body").join(".") : ""

  if (message && location) return `${location}: ${message}`
  return message
}

/** 响应体是不是后端给的错误体（FastAPI 的 `{ detail }`） */
function hasDetail(payload: unknown): boolean {
  return payload !== null && typeof payload === 'object' && "detail" in payload
}

/** 从 FastAPI 默认错误体 `{ detail }` 中提取可读信息 */
function extractMessage(payload: unknown, fallback: string): string {
  if (typeof payload === "string" && payload) return clip(payload)

  if (payload !== null && typeof payload === "object" && "detail" in payload) {
    const {detail} = payload as { detail: unknown }
    if (typeof detail === "string" && detail) return detail
    if (Array.isArray(detail)) {
      const messages = detail.map(describeDetail).filter(Boolean)
      if (messages.length) return messages.join("; ")
    }
  }

  return fallback
}

/** 从任意异常中取出可直接展示的提示文案 */
export function errorMessage(error: unknown, fallback = "请求失败，请稍后重试"): string {
  if (error instanceof Error && error.message) return error.message
  return fallback
}

/** 拼接 REST 路径并逐段转义（表 / 字段编号里的 `.` 与空格都能安全入 URL） */
export function buildPath(...segments: string[]): string {
  return `/${segments.map((segment) => encodeURIComponent(segment)).join("/")}`
}

function resolveURL(url: string, params?: QueryParams): string {
  const target = /^https?:\/\//i.test(url) ? url : `${BASE_URL}${url}`
  if (!params) return target

  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue
    search.append(key, String(value))
  }

  const query = search.toString()
  if (!query) return target
  return target.includes("?") ? `${target}&${query}` : `${target}?${query}`
}

/** 序列化请求体；`rawIntegerFields` 里的字段以不带引号的整数输出 */
function stringifyBody(body: unknown, rawIntegerFields?: string[]): string {
  let text = JSON.stringify(body)
  if (!rawIntegerFields?.length) return text

  for (const field of rawIntegerFields) {
    const pattern = new RegExp(`("${field}"\\s*:\\s*)"(-?\\d+)"`, "g")
    text = text.replace(pattern, "$1$2")
  }

  return text
}

/** 非 2xx 时按 FastAPI 错误体抛出 RequestError */
async function throwForStatus(response: Response, method: string, target: string): Promise<never> {
  const text = await response.text()
  const data = text ? parsePayload(text) : undefined
  const detail = data === PARSE_FAILED ? text : data
  const statusText = `${response.status} ${response.statusText}`.trim()

  if (data === PARSE_FAILED && looksLikeHTML(response, text)) {
    throw new RequestError(
      response.status,
      `${method} ${target} 返回的是 HTML 而不是 JSON，说明该地址没有打到后端（配置 VITE_API_BASE_URL 或在 vite.config.ts 里加 server.proxy）：${clip(text)}`,
      text,
    )
  }

  const message = extractMessage(detail, statusText)

  // 404 且没有后端的错误体：多半是开发服务器没转发这个前缀（SSE 请求还会因此直接 404）
  if (response.status === 404 && !hasDetail(data)) {
    throw new RequestError(
      response.status,
      `${method} ${target} 返回 ${message}：这个地址没有打到后端，检查 vite.config.ts 的 apiProxy 是否漏了这个前缀`,
      detail,
    )
  }

  throw new RequestError(response.status, message, detail)
}

/**
 * 发送请求，`T` 为响应体解析后的类型。
 * 空响应体（含 204）返回 `undefined`；非 2xx 抛出 `RequestError`；
 * 2xx 但响应体不是 JSON 时同样抛错，并说明指向的地址与响应片段，
 * 避免把 HTML 之类的内容当成数据往下传。
 */
export async function request<T = unknown>(
  method: string,
  url: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {...options.headers}
  const target = resolveURL(url, options.params)
  let payload: string | undefined

  if (body !== undefined) {
    headers["Content-Type"] ??= "application/json"
    payload = stringifyBody(body, options.rawIntegerFields)
  }

  const response = await fetch(target, {
    method,
    headers,
    body: payload,
    signal: options.signal,
  })

  if (!response.ok) await throwForStatus(response, method, target)

  const text = await response.text()
  if (!text) return undefined as T

  const data = parsePayload(text)
  if (data === PARSE_FAILED) {
    const reason = looksLikeHTML(response, text)
      ? `返回的是 HTML 而不是 JSON，说明该地址没有打到后端（配置 VITE_API_BASE_URL 或在 vite.config.ts 里加 server.proxy）`
      : `响应体不是合法 JSON`
    throw new RequestError(response.status, `${method} ${target} ${reason}：${clip(text)}`, text)
  }

  return data as T
}

export function get<T = unknown>(
  url: string,
  params?: QueryParams,
  options: RequestOptions = {},
): Promise<T> {
  return request<T>("GET", url, undefined, {...options, params: params ?? options.params})
}

export function post<T = unknown>(
  url: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  return request<T>("POST", url, body, options)
}

export function put<T = unknown>(
  url: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  return request<T>("PUT", url, body, options)
}

export function del<T = unknown>(url: string, options: RequestOptions = {}): Promise<T> {
  return request<T>("DELETE", url, undefined, options)
}

export {del as delete}

/** SSE 一帧 `data:` 里的一段文本（服务端一帧只发一条 data） */
function emitFrame(frame: string, onData: (data: string) => void): void {
  for (const line of frame.split("\n")) {
    if (!line.startsWith("data:")) continue
    const data = line.slice(5).trim()
    if (data) onData(data)
  }
}

/**
 * POST 一个 JSON 请求体并按 SSE 读取响应：每收到一帧 `data:` 就回调一次，
 * 流正常关闭或中途中断（`signal`）时返回。
 *
 * `EventSource` 只支持 GET，而检索问答是 POST，故这里直接读 `fetch` 的响应流
 * 自己切帧。非 2xx 与 `request` 一样抛 `RequestError`（参数校验失败仍是 JSON 错误体）。
 */
export async function stream(
  url: string,
  body: unknown,
  onData: (data: string) => void,
  options: RequestOptions = {},
): Promise<void> {
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    ...options.headers,
  }
  headers["Content-Type"] ??= "application/json"

  const target = resolveURL(url, options.params)
  const response = await fetch(target, {
    method: "POST",
    headers,
    body: stringifyBody(body, options.rawIntegerFields),
    signal: options.signal,
  })

  if (!response.ok) await throwForStatus(response, "POST", target)
  if (!response.body) throw new RequestError(response.status, `${target} 没有返回可读的流`)

  options.onResponse?.(response)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  for (;;) {
    const {done, value} = await reader.read()
    if (done) break

    buffer += decoder.decode(value, {stream: true}).replace(/\r\n/g, "\n")

    let boundary = buffer.indexOf("\n\n")
    while (boundary >= 0) {
      emitFrame(buffer.slice(0, boundary), onData)
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf("\n\n")
    }
  }

  // 收尾：流结束时缓冲区里可能还留着没有以空行结尾的一帧
  if (buffer.trim()) emitFrame(buffer, onData)
}

const http = {request, get, post, put, del, delete: del, stream, RequestError}

export default http
