<script setup>
// 工单列表：状态/优先级/排序筛选 + 全量工单表。
//
// 状态与优先级的**取值来自 /api/v1/meta**（本组件的下拉是 meta.states / meta.priorities 渲染的），
// 不是写死的常量 —— 否则后端加一个状态，界面就会漏掉它。
import { computed } from 'vue'
import Icon from './Icon.vue'
import Tag from './Tag.vue'
import SlaBar from './SlaBar.vue'
import { state, loadTickets, loadTicketDetail } from '../store.js'
import { fmtTime, intentLabel, slaView } from '../meta.js'

const f = computed(() => state.tickets.filters)
const states = computed(() => (state.meta ? state.meta.states : []))
const priorities = computed(() => (state.meta ? state.meta.priorities : []))
const items = computed(() => state.tickets.items)

async function apply() { await loadTickets() }
function reset() {
  state.tickets.filters = { state: '', priority: '', order: 'created', limit: 20 }
  apply()
}
function toggleState(v) {
  const cur = f.value.state ? f.value.state.split(',') : []
  const i = cur.indexOf(v)
  i >= 0 ? cur.splice(i, 1) : cur.push(v)
  f.value.state = cur.join(',')
  apply()
}
</script>

<template>
  <div class="card">
    <div class="card-head">
      <h3>工单列表</h3>
      <span class="hint">租户内全量 · 后端 <code>/api/v1/tickets</code></span>
      <span class="spacer" />
      <button class="btn ghost sm" :disabled="state.tickets.loading" @click="loadTickets">
        <Icon name="refresh" :class="{ spin: state.tickets.loading }" />刷新
      </button>
    </div>

    <div class="row wrap" style="margin-bottom: 12px">
      <span class="mute2">状态：</span>
      <button class="preset" :class="{ active: !f.state }" @click="f.state = ''; apply()">全部</button>
      <button v-for="s in states" :key="s" class="preset" :class="{ active: (f.state || '').split(',').includes(s) }"
              @click="toggleState(s)">{{ s }}</button>
    </div>
    <div class="row wrap" style="margin-bottom: 12px">
      <span class="mute2">优先级：</span>
      <select v-model="f.priority" class="select" style="width: 130px" @change="apply">
        <option value="">全部</option>
        <option v-for="p in priorities" :key="p" :value="p">{{ p }}</option>
      </select>
      <span class="mute2">排序：</span>
      <select v-model="f.order" class="select" style="width: 150px" @change="apply">
        <option value="created">最新创建</option>
        <option value="updated">最近更新</option>
        <option value="sla">SLA 死限（近→远）</option>
      </select>
      <span class="mute2">条数：</span>
      <select v-model.number="f.limit" class="select" style="width: 100px" @change="apply">
        <option :value="20">20</option><option :value="50">50</option><option :value="100">100</option>
      </select>
      <span class="spacer" />
      <button class="btn ghost sm" @click="reset"><Icon name="x" />清空筛选</button>
    </div>

    <div v-if="state.tickets.error" class="empty">
      <Icon name="alert" /><br />{{ state.tickets.error }}<br />
      <span class="mute2">该端点是坐席只读端点（agent / viewer）：需在右上角填入凭据。</span>
    </div>
    <div v-else-if="!items.length" class="empty">没有匹配的工单。先去「在线客服」发一条消息造一张？</div>
    <table v-else class="table">
      <thead>
        <tr><th>工单</th><th>状态</th><th>优先级</th><th>品类</th><th>意图</th><th>处理人</th>
            <th style="min-width: 170px">SLA</th><th>更新时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="t in items" :key="t.id" class="clickable" :class="{ selected: state.detail.id === t.id }"
            @click="loadTicketDetail(t.id)">
          <td><strong>#{{ t.id }}</strong> <span v-if="t.has_graph" class="tag soft" title="有一条 LangGraph 编排记录">图</span></td>
          <td><Tag kind="state" :value="t.state" /></td>
          <td><Tag kind="prio" :value="t.priority" /></td>
          <td>{{ t.category || '—' }}</td>
          <td class="mute2">{{ intentLabel(t.intent) }}</td>
          <td class="mute2">{{ t.assignee || '—' }}</td>
          <td><SlaBar :row="t" :meta="state.meta" :show-text="slaView(t, state.meta, state.nowMs).bucket !== 'ok' " /></td>
          <td class="mute2">{{ fmtTime(t.updated_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div class="mute2" style="margin-top: 10px">
      共 {{ items.length }} 条（上限 {{ f.limit }}）。列表**不暴露**图的内部线程标识，只标出"有编排记录"。
    </div>
  </div>
</template>
