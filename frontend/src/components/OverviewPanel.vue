<script setup>
// 运营看板：状态/优先级/品类分布 + SLA 分桶 + 满意度。
//
// 诚实边界：这是**当前库内、当前租户**的一次聚合，不是时间序列 —— 没有落库的指标采样
// 就没有趋势，所以这里不画折线。要趋势得先有采样表（另一个工程）。
import { computed } from 'vue'
import Icon from './Icon.vue'
import Stat from './Stat.vue'
import Distribution from './Distribution.vue'
import { state, loadOverview } from '../store.js'
import { stateLabel, priorityLabel, bucketLabel, fmtTime } from '../meta.js'

const o = computed(() => state.overview.data)
const toneOf = { ok: 'var(--ok)', warning: 'var(--warn)', overdue: 'var(--danger)' }

const stateItems = computed(() => {
  if (!o.value || !state.meta) return []
  return state.meta.states.map((s) => ({
    key: s, label: `${stateLabel(s)}（${s}）`, value: o.value.tickets.by_state[s] || 0,
    tone: `var(--state-${s})`,
  }))
})
const prioItems = computed(() => {
  if (!o.value || !state.meta) return []
  return state.meta.priorities.map((p) => ({
    key: p, label: priorityLabel(p), value: o.value.tickets.by_priority[p] || 0, tone: `var(--${p.toLowerCase()})`,
  }))
})
const catItems = computed(() => {
  if (!o.value || !state.meta) return []
  return state.meta.categories.map((c) => ({ key: c, label: c, value: o.value.tickets.by_category[c] || 0 }))
})
const slaItems = computed(() => {
  if (!o.value) return []
  return Object.keys(o.value.sla).map((k) => ({
    key: k, label: bucketLabel(k), value: o.value.sla[k], tone: toneOf[k],
  }))
})
const ratingItems = computed(() => {
  if (!o.value) return []
  return [5, 4, 3, 2, 1].map((n) => ({
    key: String(n), label: `${n} ★`, value: o.value.reviews.by_rating[String(n)] || 0, tone: 'var(--warn)',
  }))
})
</script>

<template>
  <div class="card">
    <div class="card-head">
      <h3>运营看板</h3>
      <span class="hint">租户内聚合 · <code>/api/v1/admin/overview</code></span>
      <span class="spacer" />
      <button class="btn ghost sm" :disabled="state.overview.loading" @click="loadOverview">
        <Icon name="refresh" :class="{ spin: state.overview.loading }" />刷新
      </button>
    </div>

    <div v-if="state.overview.error" class="empty">
      <Icon name="alert" /><br />{{ state.overview.error }}<br />
      <span class="mute2">运营聚合是坐席只读端点：需要在右上角填入凭据（agent 或 viewer）。</span>
    </div>
    <template v-else-if="o">
      <div class="grid cols-4">
        <Stat k="工单总数" :v="o.tickets.total" s="当前租户" />
        <Stat k="推进中" :v="o.active_total" s="new / in_triage / processing / pending_user" />
        <Stat k="待办队列" :v="o.queue.escalated" s="已转人工 / 超时升级" :tone="o.queue.escalated ? 'warn' : ''" />
        <Stat k="SLA 已超时" :v="o.sla.overdue" s="调度器已/将升级为 escalated" :tone="o.sla.overdue ? 'danger' : 'ok'" />
        <Stat k="SLA 临近" :v="o.sla.warning" s="剩余不足 20% 时限" :tone="o.sla.warning ? 'warn' : ''" />
        <Stat k="平均满意度" :v="o.reviews.avg === null ? '—' : o.reviews.avg + ' ★'"
              :s="o.reviews.count ? o.reviews.count + ' 条评价' : '暂无评价'" />
      </div>

      <div class="grid cols-2" style="margin-top: 14px">
        <div><div class="mute2" style="margin-bottom: 6px">按状态</div><Distribution :items="stateItems" /></div>
        <div><div class="mute2" style="margin-bottom: 6px">按优先级（时限 P1 30m / P2 4h / P3 24h）</div>
          <Distribution :items="prioItems" /></div>
        <div><div class="mute2" style="margin-bottom: 6px">按品类</div><Distribution :items="catItems" /></div>
        <div><div class="mute2" style="margin-bottom: 6px">SLA 分桶（与调度器同口径）</div>
          <Distribution :items="slaItems" /></div>
        <div><div class="mute2" style="margin-bottom: 6px">满意度分布</div><Distribution :items="ratingItems" empty-text="还没有用户评价" /></div>
      </div>

      <div class="divider" />
      <div class="mute2">
        生成于 {{ fmtTime(o.generated_at) }}（后端时钟，naive UTC）。
        SLA 分桶口径与 <code>biz/sla.py::scan_sla</code> 一致：只有
        new / in_triage / processing / pending_user 参与计时；resolved 已处理完、终态无需计时。
        这是**一次聚合**，不是时间序列 —— 没有采样就没有趋势，这里不画折线。
      </div>
    </template>
  </div>
</template>
