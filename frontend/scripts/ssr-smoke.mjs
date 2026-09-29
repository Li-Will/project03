// SSR 烟测：把工作台的几个关键状态各渲染一次，字符数过小或抛异常就失败。
//
// 为什么纳入 CI：这是前端唯一的"自动化的东西"。它兜的是白屏级错误 ——
// 模板里访问了 undefined 的深层字段、组件注册名写错、SSR 期访问了浏览器 API。
// 它**不代替**人工验收：布局、动效、真实数据下的观感，必须人打开看。
import { render, state } from '../.ssr-out/ssr-smoke.mjs'

const META = {
  version: '0.1.0',
  states: ['new', 'in_triage', 'processing', 'pending_user', 'resolved', 'closed', 'escalated', 'rejected'],
  transitions: {
    new: ['in_triage', 'escalated', 'rejected'],
    in_triage: ['processing', 'pending_user', 'escalated', 'rejected'],
    processing: ['pending_user', 'resolved', 'escalated'],
    pending_user: ['processing', 'resolved', 'escalated'],
    resolved: ['closed', 'processing'],
    escalated: ['pending_user', 'resolved', 'processing'],
    closed: [], rejected: [],
  },
  terminal_states: ['closed', 'rejected'],
  priorities: ['P1', 'P2', 'P3'],
  categories: ['云盘服务', '会议支持', '综合'],
  intents: ['faq', 'ticket', 'complaint', 'chat', 'need_human'],
  sla_deadlines_seconds: { P1: 1800, P2: 14400, P3: 86400 },
  sla_warn_ratio: 0.8, conf_direct: 0.6, conf_hint: 0.55, evidence_conf_threshold: 0.5,
  auth_required: false,
}

const TICKET_ROW = {
  id: 7, intent: 'ticket', category: '云盘服务', priority: 'P1', state: 'escalated',
  assignee: 'human', created_at: '2026-09-29T02:00:00', updated_at: '2026-09-29T02:10:00',
  sla_deadline: '2099-01-01T00:00:00', has_graph: true,
}

const DETAIL = {
  id: 7, intent: 'ticket', category: '云盘服务', priority: 'P1', state: 'escalated',
  assignee: 'human', created_at: '2026-09-29T02:00:00', sla_deadline: '2099-01-01T00:00:00',
  messages: [
    { sender: 'customer', content: '会议打不开了，一直报错卡死！', source: 'customer', at: '2026-09-29T02:00:00' },
    { sender: 'agent', content: '已为您转接人工客服，请稍候（工单 7）。', source: 'template', at: '2026-09-29T02:00:03' },
    { sender: 'system', content: 'SLA 临近提醒：剩余处理时间不足 20%', source: 'sla', at: '2026-09-29T02:24:00' },
  ],
  events: [
    { from: null, to: 'new', actor: 'system', reason: '创建工单', at: '2026-09-29T02:00:00' },
    { from: 'new', to: 'escalated', actor: 'system', reason: '证据置信度 0.320 低于阈值 0.5', at: '2026-09-29T02:00:03' },
  ],
}

const OVERVIEW = {
  tenant: 'default', generated_at: '2026-09-29T02:30:00',
  tickets: {
    total: 12,
    by_state: { new: 1, in_triage: 0, processing: 2, pending_user: 1, resolved: 3, closed: 3, escalated: 2, rejected: 0 },
    by_priority: { P1: 3, P2: 4, P3: 5 },
    by_category: { 云盘服务: 5, 会议支持: 4, 综合: 3 },
  },
  sla: { ok: 3, warning: 1, overdue: 2, no_deadline: 0, resolved: 3, terminal: 3 },
  reviews: { count: 4, avg: 4.5, by_rating: { 1: 0, 2: 0, 3: 0, 4: 2, 5: 2 } },
  queue: { escalated: 2 }, active_total: 4, auto_upgrade_pending: 1,
}

function base(s) {
  s.meta = META
  s.runtime = { version: '0.1.0', ready: true, problems: [] }
  s.identity = { tenant: 'tenantA', actor: '坐席甲', role: 'agent', key_id: 'sk-wb-', auth_required: true }
  s.customerName = '演示用户'
}

const cases = [
  ['空态（首次打开，无任何数据）', (s) => { base(s); s.overview.data = null; s.queue.items = []; s.tickets.items = [] }],
  ['待办队列（含超时行）', (s) => {
    base(s)
    s.view = 'queue'
    s.queue.items = [TICKET_ROW, { ...TICKET_ROW, id: 8, priority: 'P3', sla_deadline: '2020-01-01T00:00:00' }]
  }],
  ['工单列表（筛选 + 多状态）', (s) => {
    base(s); s.view = 'tickets'
    s.tickets.items = [TICKET_ROW, { ...TICKET_ROW, id: 9, state: 'resolved', priority: 'P2' }]
  }],
  ['状态机（聚焦 escalated）', (s) => { base(s); s.view = 'machine'; s.overview.data = OVERVIEW; s.machineFocus = 'escalated' }],
  ['运营看板（全部分布有数据）', (s) => { base(s); s.view = 'overview'; s.overview.data = OVERVIEW }],
  ['详情抽屉（审计时间线 + 只读角色）', (s) => {
    base(s); s.identity = { tenant: 'tenantA', actor: '观察者', role: 'viewer', key_id: 'sk-v', auth_required: true }
    s.detail.id = 7; s.detail.data = DETAIL
  }],
]

let failed = 0
for (const [name, setup] of cases) {
  try {
    const html = await render(setup)
    const size = html.length
    const ok = size > 900 && html.includes('id="app"') === false && !html.includes('undefined')
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(30)} ${size} 字符`)
    if (!ok) failed++
  } catch (e) {
    console.log(`FAIL  ${name.padEnd(30)} 渲染异常: ${e && e.message}`)
    failed++
  }
}

console.log(failed ? `\n${failed} 个场景失败` : '\nSSR 烟测通过（兜白屏，不代替人工验收）')
// SSR 没有卸载钩子：组件若在顶层起过定时器，Node 不会自己退出 —— 显式退出（CI 会挂住）。
process.exit(failed ? 1 : 0)
