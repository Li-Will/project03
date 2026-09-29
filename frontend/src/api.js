// 统一请求层：凭据注入 + 结构化错误。
//
// 两个刻意的设计：
//  1) **坐席身份只从 X-API-Key 走**：human-reply 的请求体里绝不带 actor
//     （后端的 contract 是"身份从凭据来，不从请求体来"，前端不提供伪造它的入口）；
//  2) 错误保持 status：401（没凭据）/ 403（角色不够）/ 404（跨租户不存在）/ 422（状态机拒绝）
//     在前端的处置完全不同 —— 把它们统一成一句"请求失败"就等于把业务语义丢掉。

const KEY_NAME = 'ta_workbench_key'

export function getApiKey() {
  try { return sessionStorage.getItem(KEY_NAME) || '' } catch { return '' }
}
export function setApiKey(k) {
  try { k ? sessionStorage.setItem(KEY_NAME, k) : sessionStorage.removeItem(KEY_NAME) } catch { /* 无 storage 时仅存内存 */ }
}

export class ApiError extends Error {
  constructor(status, detail, path) {
    super(detail || `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.path = path
  }
}

function authHeaders(extra = {}) {
  const h = { ...extra }
  const k = getApiKey()
  if (k) h['X-API-Key'] = k
  return h
}

async function request(path, { method = 'GET', body } = {}) {
  const headers = authHeaders(body === undefined ? {} : { 'Content-Type': 'application/json' })
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  const text = await res.text()
  let data = null
  if (text) { try { data = JSON.parse(text) } catch { data = { detail: text } } }
  if (!res.ok) {
    const raw = data && (data.detail ?? data.error ?? data)
    const detail = typeof raw === 'string' ? raw : (raw ? JSON.stringify(raw) : `HTTP ${res.status}`)
    throw new ApiError(res.status, detail, path)
  }
  return data
}

export const api = {
  meta: () => request('/api/v1/meta'),
  health: () => request('/api/v1/health/live'),
  ready: () => request('/api/v1/health/ready'),

  // 用户侧（不强制鉴权；带 key 则归 key 的租户）
  chat: (text, customer_name = '访客') => request('/api/v1/chat', { method: 'POST', body: { text, customer_name } }),
  ticket: (id) => request(`/api/v1/tickets/${id}`),
  ack: (id, rating) => request(`/api/v1/tickets/${id}/ack${rating ? `?rating=${rating}` : ''}`, { method: 'POST' }),

  // 坐席侧（require_staff / require_agent）
  me: () => request('/api/v1/admin/me'),
  tickets: ({ state = '', priority = '', limit = 20, order = 'created' } = {}) => {
    const qs = new URLSearchParams()
    if (state) qs.set('state', state)
    if (priority) qs.set('priority', priority)
    if (limit) qs.set('limit', String(limit))
    if (order) qs.set('order', order)
    return request(`/api/v1/tickets?${qs}`)
  },
  escalations: (limit = 20) => request(`/api/v1/admin/escalations?limit=${limit}`),
  overview: () => request('/api/v1/admin/overview'),
  humanReply: (id, content) => request(`/api/v1/tickets/${id}/human-reply`, { method: 'POST', body: { content } }),
}

/**
 * 对话流（SSE）：用 fetch 手动读流解析，不用 EventSource ——
 * EventSource 不支持自定义请求头，带租户 key 时它会把请求打到默认租户去。
 * 事件契约：start → intent → node* → done | error（见 api/main.py 的 chat_stream）。
 */
export async function chatStream({ text, customerName = '访客', onEvent, signal }) {
  const qs = new URLSearchParams({ text, customer_name: customerName })
  const res = await fetch(`/api/v1/chat/stream?${qs}`, { headers: authHeaders(), signal })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, `流式接口不可用（HTTP ${res.status}）`, '/api/v1/chat/stream')
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  const events = []
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      let ev = 'message', data = ''
      for (const line of chunk.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      let payload = null
      try { payload = JSON.parse(data) } catch { payload = { raw: data } }
      const item = { event: ev, data: payload }
      events.push(item)
      if (onEvent) onEvent(item)
    }
  }
  return events
}
