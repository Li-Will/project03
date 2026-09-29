// 工作台状态：一个 reactive 对象 + 一组动作（不引 Pinia —— 单页单用户场景，够用）。
//
// 三个约定：
//  1) **业务规则不在前端**：状态机、时限、阈值都从 /api/v1/meta 读（见 meta.js）；
//  2) **状态真值不在前端**：工单状态的真值在数据库，界面每次操作后回查，不做乐观改写
//     （乐观更新在"状态机拒绝非法迁移"的系统里会直接骗人）；
//  3) 倒计时只是展示：到点由服务端调度器真升级，前端不产生任何副作用。
import { reactive, computed } from 'vue'
import { api, chatStream, getApiKey, setApiKey, ApiError } from './api.js'
import { toast } from './toast.js'

const THEME_KEY = 'ta_workbench_theme'
const CUSTOMER_KEY = 'ta_workbench_customer'

export const VIEWS = [
  { id: 'chat', label: '在线客服', hint: '用户侧入口' },
  { id: 'queue', label: '待办队列', hint: '转人工 / 超时升级' },
  { id: 'tickets', label: '工单列表', hint: '筛选与详情' },
  { id: 'machine', label: '状态机', hint: '8 态与合法迁移' },
  { id: 'overview', label: '运营看板', hint: '分布 / SLA / 评价' },
]

export const state = reactive({
  view: 'chat',
  theme: 'auto',
  sidebarOpen: false,
  apiKey: getApiKey(),
  customerName: (typeof localStorage !== 'undefined' && localStorage.getItem(CUSTOMER_KEY)) || '演示用户',
  meta: null,
  metaError: null,
  identity: null,        // {tenant, actor, role, auth_required}
  identityError: null,
  runtime: { version: '', ready: null, problems: [] },
  queue: { items: [], loading: false, error: null, updatedAt: null },
  tickets: { items: [], loading: false, error: null, filters: { state: '', priority: '', order: 'created', limit: 20 } },
  detail: { id: null, data: null, loading: false, error: null, reply: '', sending: false },
  overview: { data: null, loading: false, error: null },
  machineFocus: null,    // 状态机聚焦的状态（点击节点切换）
  chat: { messages: [], busy: false, lastTicketId: null, stream: null, abort: null },
  nowMs: Date.now(),
})

export const isStaff = computed(() => !!state.identity)
export const canReply = computed(() => state.identity && state.identity.role === 'agent')

// ---------- 主题 ----------

export function applyTheme() {
  const t = state.theme
  if (typeof document !== 'undefined') document.documentElement.setAttribute('data-theme', t)
}
export function setTheme(t) {
  state.theme = t
  try { localStorage.setItem(THEME_KEY, t) } catch { /* 忽略 */ }
  applyTheme()
}
export function initTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    if (saved) state.theme = saved
  } catch { /* 忽略 */ }
  applyTheme()
}

// ---------- 每秒时钟（SLA 倒计时）----------
// SSR 下没有卸载钩子：定时器必须带 window 守卫，否则 Node 进程不退出（CI 会挂住）。

export function startClock() {
  if (typeof window === 'undefined') return () => {}
  const id = setInterval(() => { state.nowMs = Date.now() }, 1000)
  return () => clearInterval(id)
}

// ---------- 基础加载 ----------

export async function loadMeta() {
  try {
    state.meta = await api.meta()
    state.metaError = null
  } catch (e) {
    state.metaError = describe(e)
  }
}

export async function loadRuntime() {
  try {
    state.runtime.version = state.meta ? state.meta.version : ''
    const ready = await api.ready()
    state.runtime.ready = ready.status === 'ready'
    state.runtime.problems = []
  } catch (e) {
    state.runtime.ready = false
    state.runtime.problems = (e instanceof ApiError && e.status === 503)
      ? safeProblems(e)
      : [describe(e)]
  }
}

function safeProblems(e) {
  try { return JSON.parse(e.detail).problems || [] } catch { return [e.detail] }
}

export async function loadIdentity() {
  try {
    state.identity = await api.me()
    state.identityError = null
  } catch (e) {
    state.identity = null
    state.identityError = describe(e)
  }
}

export function saveKey(k) {
  const v = (k || '').trim()
  setApiKey(v)
  state.apiKey = v
  state.identity = null
  if (v) toast.ok('已保存坐席凭据（仅存于本会话）')
  else toast.warn('已清除坐席凭据：坐席端点将不可用')
  return refreshStaffData()
}

export function setCustomerName(name) {
  state.customerName = (name || '').trim() || '访客'
  try { localStorage.setItem(CUSTOMER_KEY, state.customerName) } catch { /* 忽略 */ }
}

async function refreshStaffData() {
  await loadIdentity()
  await Promise.all([loadQueue(), loadTickets(), loadOverview()])
}

// ---------- 用户侧：对话 ----------

export function newChat() {
  state.chat.messages = []
  state.chat.lastTicketId = null
}

export async function sendChat(text) {
  const t = (text || '').trim()
  if (!t || state.chat.busy) return
  state.chat.messages.push({ role: 'user', text: t, at: Date.now() })
  const bot = { role: 'bot', text: '', at: Date.now(), pending: true, steps: [], citations: [], type: null }
  state.chat.messages.push(bot)
  state.chat.busy = true

  const progress = []
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  state.chat.abort = ctrl
  try {
    await chatStream({
      text: t,
      customerName: state.customerName,
      signal: ctrl ? ctrl.signal : undefined,
      onEvent: ({ event, data }) => {
        if (event === 'intent') {
          bot.intent = data.intent
          bot.confidence = data.confidence
        } else if (event === 'node') {
          progress.push(data)
          bot.steps = progress.slice()
        } else if (event === 'done') {
          bot.pending = false
          applyDone(bot, data)
        } else if (event === 'error') {
          bot.pending = false
          bot.text = `诊断链路异常：${data.detail || '未知错误'}`
          bot.error = true
        }
      },
    })
    if (bot.pending) { bot.pending = false; bot.text = bot.text || '（未收到最终结果）' }
  } catch (e) {
    bot.pending = false
    bot.error = true
    bot.text = `请求失败：${describe(e)}`
  } finally {
    state.chat.busy = false
    state.chat.stream = null
    state.chat.abort = null
    if (bot.type === 'escalated' || bot.type === 'ticket') {
      await Promise.all([loadTicketDetailIfOpen(bot.ticketId), loadQueue(), loadOverview()])
    }
  }
}

function applyDone(bot, d) {
  bot.type = d.type
  bot.ticketId = d.ticket_id ?? null
  bot.ticketState = d.state ?? null
  bot.category = d.category ?? null
  bot.priority = d.priority ?? null
  bot.citations = d.citations || []
  bot.confidence = d.confidence ?? bot.confidence
  if (d.type === 'faq') bot.text = d.reply || ''
  else if (d.type === 'ticket') bot.text = d.reply || '（方案为空）'
  else if (d.type === 'escalated') bot.text = d.reply || '已转人工'
  else bot.text = d.reply || JSON.stringify(d)
  if (d.diagnosis_steps && d.diagnosis_steps.length) bot.steps = d.diagnosis_steps
  state.chat.lastTicketId = bot.ticketId ?? state.chat.lastTicketId
  bot.reason = d.reason || null
}

export async function ackTicket(ticketId, rating) {
  try {
    const r = await api.ack(ticketId, rating)
    toast.ok(`工单 #${ticketId} → ${r.state}（满意度已记录）`)
    await refreshTicketEverywhere(ticketId)
    return r
  } catch (e) {
    toast.error(`确认失败：${describe(e)}`)
    throw e
  }
}

// ---------- 坐席侧 ----------

export async function loadQueue() {
  state.queue.loading = true
  try {
    const r = await api.escalations(50)
    state.queue.items = r.items || []
    state.queue.error = null
    state.queue.updatedAt = Date.now()
    state.queue.tenant = r.tenant
  } catch (e) {
    state.queue.items = []
    state.queue.error = describe(e)
  } finally {
    state.queue.loading = false
  }
}

export async function loadTickets() {
  state.tickets.loading = true
  const f = state.tickets.filters
  try {
    const r = await api.tickets(f)
    state.tickets.items = r.items || []
    state.tickets.error = null
  } catch (e) {
    state.tickets.items = []
    state.tickets.error = describe(e)
  } finally {
    state.tickets.loading = false
  }
}

export async function loadTicketDetail(id) {
  if (!id) return
  state.detail.id = id
  state.detail.loading = true
  try {
    state.detail.data = await api.ticket(id)
    state.detail.error = null
  } catch (e) {
    state.detail.data = null
    state.detail.error = describe(e)
  } finally {
    state.detail.loading = false
  }
}

export function closeDetail() {
  state.detail.id = null
  state.detail.data = null
  state.detail.error = null
  state.detail.reply = ''
}

async function loadTicketDetailIfOpen(id) {
  if (state.detail.id && (!id || state.detail.id === id)) await loadTicketDetail(state.detail.id)
}

export async function sendHumanReply() {
  const id = state.detail.id
  const content = (state.detail.reply || '').trim()
  if (!id || !content || state.detail.sending) return
  state.detail.sending = true
  try {
    // actor 不传：坐席身份由 X-API-Key 决定（后端只认凭据）
    const r = await api.humanReply(id, content)
    state.detail.reply = ''
    toast.ok(`已回复工单 #${id}（当前状态 ${r.state}）`)
    await refreshTicketEverywhere(id)
  } catch (e) {
    toast.error(humanReplyError(e))
  } finally {
    state.detail.sending = false
  }
}

function humanReplyError(e) {
  if (e instanceof ApiError) {
    if (e.status === 401) return '需要坐席凭据：请先在右上角填入 X-API-Key'
    if (e.status === 403) return `当前角色无权回复（${state.identity ? state.identity.role : '未知'}）：写操作仅限 agent`
    if (e.status === 404) return '工单不存在，或不属于当前租户（跨租户一律 404）'
    if (e.status === 422) return `状态机拒绝：${e.detail}`
  }
  return `回复失败：${describe(e)}`
}

export async function refreshTicketEverywhere(id) {
  await Promise.all([loadTicketDetailIfOpen(id), loadQueue(), loadTickets(), loadOverview()])
}

export async function loadOverview() {
  state.overview.loading = true
  try {
    state.overview.data = await api.overview()
    state.overview.error = null
  } catch (e) {
    state.overview.data = null
    state.overview.error = describe(e)
  } finally {
    state.overview.loading = false
  }
}

export async function refreshAll() {
  await loadMeta()
  await loadRuntime()
  await loadIdentity()
  await Promise.all([loadQueue(), loadTickets(), loadOverview()])
  if (state.chat.lastTicketId) await loadTicketDetailIfOpen(state.chat.lastTicketId)
}

// ---------- 杂项 ----------

export function setView(v) {
  state.view = v
  state.sidebarOpen = false
  if (v === 'overview' || v === 'queue' || v === 'tickets') loadOverviewSilently(v)
}

function loadOverviewSilently(v) {
  if (v === 'queue') loadQueue()
  if (v === 'tickets') loadTickets()
  if (v === 'overview') loadOverview()
}

export function describe(e) {
  if (e instanceof ApiError) return e.detail || `HTTP ${e.status}`
  return e && e.message ? e.message : String(e)
}
