<script setup>
// 待办队列：转人工 + SLA 超时升级的工单。
//
// 顺序**不重排**：后端 list_escalations 已按 sla_deadline 升序返回（最紧急的在最上面），
// 前端再"优化"一次排序就会与调度器的判据不一致 —— 排序换算法可以，判据不换。
import { computed } from 'vue'
import Icon from './Icon.vue'
import Tag from './Tag.vue'
import SlaBar from './SlaBar.vue'
import { state, loadQueue, loadTicketDetail } from '../store.js'
import { fmtTime, humanDuration, slaView } from '../meta.js'

const items = computed(() => state.queue.items)
const overdue = computed(() => items.value.filter((r) => slaView(r, state.meta, state.nowMs).bucket === 'overdue').length)
const warning = computed(() => items.value.filter((r) => slaView(r, state.meta, state.nowMs).bucket === 'warning').length)

function waited(row) {
  const created = new Date(`${row.created_at}Z`).getTime()
  return humanDuration((state.nowMs - created) / 1000)
}
</script>

<template>
  <div class="card">
    <div class="card-head">
      <h3>待办队列</h3>
      <span class="hint">按 SLA 死限升序 · 后端 <code>/api/v1/admin/escalations</code></span>
      <span class="spacer" />
      <span v-if="overdue" class="tag sla" data-k="overdue"><i class="dot" />超时 {{ overdue }}</span>
      <span v-if="warning" class="tag sla" data-k="warning"><i class="dot" />临近 {{ warning }}</span>
      <button class="btn ghost sm" :disabled="state.queue.loading" @click="loadQueue">
        <Icon name="refresh" :class="{ spin: state.queue.loading }" />刷新
      </button>
    </div>

    <div v-if="state.queue.error" class="empty">
      <Icon name="alert" /><br />
      {{ state.queue.error }}<br />
      <span class="mute2">坐席端点在 <code>REQUIRE_AUTH=true</code> 时要求 X-API-Key —— 右上角「坐席凭据」可填入。</span>
    </div>
    <div v-else-if="!items.length" class="empty">
      队列为空。转人工或 SLA 超时升级的工单会出现在这里 ——
      在「在线客服」里发「我要转人工」就能造一张。
    </div>
    <table v-else class="table">
      <thead>
        <tr>
          <th>工单</th><th>品类</th><th>优先级</th><th style="min-width: 190px">SLA 剩余</th>
          <th>已等待</th><th>死限</th><th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in items" :key="r.ticket_id" class="clickable" @click="loadTicketDetail(r.ticket_id)">
          <td><strong>#{{ r.ticket_id }}</strong></td>
          <td>{{ r.category || '—' }}</td>
          <td><Tag kind="prio" :value="r.priority" /></td>
          <td><SlaBar :row="r" :meta="state.meta" /></td>
          <td class="num">{{ waited(r) }}</td>
          <td class="mute2">{{ fmtTime(r.sla_deadline) }}</td>
          <td><Icon name="chevron-right" class="mute" /></td>
        </tr>
      </tbody>
    </table>
    <div v-if="state.queue.updatedAt" class="mute2" style="margin-top: 10px">
      更新于 {{ fmtTime(new Date(state.queue.updatedAt).toISOString()) }}（本地时钟）
    </div>
  </div>
</template>
