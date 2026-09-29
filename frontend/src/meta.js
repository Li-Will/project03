// 业务元数据（状态机 / SLA 时限与阈值 / 枚举）——**唯一来源是后端 /api/v1/meta**。
//
// 本文件只做两件渲染层的事：
//   1) 缓存与读取；
//   2) "业务值 → 中文文案 + 语义色"的映射（枚举来自后端，配色本来就是渲染层的事）。
// 任何"时限是多少 / 阈值是多少 / 哪些状态可达"的问题，都必须问 meta，不许在这里写死。

export const STATE_LABEL = {
  new: '新单',
  in_triage: '受理中',
  processing: '处理中',
  pending_user: '待用户',
  resolved: '已解决',
  closed: '已关闭',
  escalated: '已转人工',
  rejected: '已拒绝',
}

export const PRIORITY_LABEL = { P1: 'P1 紧急', P2: 'P2 较急', P3: 'P3 普通' }

export const BUCKET_LABEL = {
  ok: '期限内',
  warning: '临近超时',
  overdue: '已超时',
  no_deadline: '未计时',
  resolved: '已解决待关闭',
  terminal: '终态',
}

export const INTENT_LABEL = {
  faq: 'FAQ 咨询',
  ticket: '报障',
  complaint: '投诉',
  chat: '寒暄',
  need_human: '要求人工',
}

export function stateLabel(v) { return STATE_LABEL[v] || v || '—' }
export function priorityLabel(v) { return PRIORITY_LABEL[v] || v || '—' }
export function bucketLabel(v) { return BUCKET_LABEL[v] || v || '—' }
export function intentLabel(v) { return INTENT_LABEL[v] || v || '—' }

/** 优先级 → SLA 时限（秒）；来自 meta，取不到就退化为 null（界面显示"未知"，不瞎猜）。 */
export function slaTotalSeconds(priority, meta) {
  const m = meta && meta.sla_deadlines_seconds
  if (!m) return null
  const v = m[priority]
  return typeof v === 'number' ? v : null
}

/** 秒 → "12m 30s" / "3h 5m" 这种坐席一眼能读的格式。 */
export function humanDuration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return '—'
  const neg = sec < 0
  let s = Math.abs(Math.round(sec))
  const d = Math.floor(s / 86400); s -= d * 86400
  const h = Math.floor(s / 3600); s -= h * 3600
  const m = Math.floor(s / 60); s -= m * 60
  let out
  if (d) out = `${d}d ${h}h`
  else if (h) out = `${h}h ${m}m`
  else if (m) out = `${m}m ${s}s`
  else out = `${s}s`
  return (neg ? '-' : '') + out
}

/**
 * SLA 剩余与分桶（**纯展示计算**）。
 * 规则来自后端 meta（时限 + warn_ratio），这里只是把它套到"当前时间"上 ——
 * 权威判定在服务端调度器（biz/sla.py scan_sla）：它到点会真的把工单升级为 escalated，
 * 界面上的红色倒计时只是让人**提前**看到，不产生任何副作用。
 */
export function slaView(row, meta, nowMs) {
  const total = slaTotalSeconds(row.priority, meta)
  if (!row.sla_deadline) {
    return { bucket: notTimed(row.state, meta) || 'no_deadline', remain: null, ratio: null, total }
  }
  const deadline = new Date(`${row.sla_deadline}Z`).getTime()   // 后端一律 naive UTC → 补 Z
  const now = nowMs
  const remain = (deadline - now) / 1000
  const warnRatio = (meta && meta.sla_warn_ratio) ?? 0.8
  const scope = notTimed(row.state, meta)
  if (scope) return { bucket: scope, remain, ratio: 1, total, deadline }
  if (remain <= 0) return { bucket: 'overdue', remain, ratio: 1, total, deadline }
  const used = total ? 1 - remain / total : null
  if (used !== null && used >= warnRatio) return { bucket: 'warning', remain, ratio: used, total, deadline }
  return { bucket: 'ok', remain, ratio: used ?? 0, total, deadline }
}

// 不参与超时计时的状态：终态 + resolved（已处理完只待关闭）。
// **escalated 不在此列** —— 它是最需要人盯的一类，标成"不参与计时"等于骗运营。
function notTimed(state, meta) {
  if (!meta) return null
  if ((meta.terminal_states || []).includes(state)) return 'terminal'
  if (state === 'resolved') return 'resolved'
  return null
}

/** UTC naive 字符串 → 本地时间文本。 */
export function fmtTime(iso) {
  if (!iso) return '—'
  const t = new Date(`${iso}Z`)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`
}
export function fmtClock(iso) {
  if (!iso) return '—'
  const t = new Date(`${iso}Z`)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`
}
