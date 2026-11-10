import { marked } from 'marked'
import DOMPurify from 'dompurify'

/**
 * 模型输出的消息是 markdown 文本：渲染成 HTML 并消毒后交给 `v-html`。
 * 允许常见元素（标题、列表、加粗、行内代码、链接等），脚本类内容一律剔除。
 */

marked.setOptions({ gfm: true, breaks: true })

export function renderMarkdown(text: string): string {
  if (!text.trim()) return ''

  const html = marked.parse(text, { async: false })
  return DOMPurify.sanitize(html)
}
