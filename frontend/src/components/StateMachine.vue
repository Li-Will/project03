<script setup>
// 状态机视图：8 态 + 合法迁移 + 当前分布。
//
// **这张图不是手画的**：节点取自 meta.states，箭头取自 meta.transitions（后端
// biz/states.py 的 TRANSITIONS 表）—— 迁移表改了，这张图跟着改；不存在"文档说得对、
// 代码做另一套"的可能。点击节点可聚焦，右侧列出该状态的合法去向。
import { computed } from 'vue'
import Icon from './Icon.vue'
import Tag from './Tag.vue'
import { state, loadOverview } from '../store.js'
import { stateLabel } from '../meta.js'

const meta = computed(() => state.meta)
const byState = computed(() => (state.overview.data ? state.overview.data.tickets.by_state : {}))
const focus = computed(() => state.machineFocus || null)
const reach = computed(() => (focus.value && meta.value ? meta.value.transitions[focus.value] || [] : []))
const incoming = computed(() => {
  if (!focus.value || !meta.value) return []
  return Object.entries(meta.value.transitions)
    .filter(([, to]) => to.includes(focus.value)).map(([from]) => from)
})
const total = computed(() => state.overview.data ? state.overview.data.tickets.total : 0)
</script>

<template>
  <div class="card">
    <div class="card-head">
      <h3>工单状态机</h3>
      <span class="hint">
        {{ meta ? meta.states.length : 0 }} 态 · 迁移表来自 <code>/api/v1/meta</code> ·
        非法迁移服务端返回 422
      </span>
      <span class="spacer" />
      <button class="btn ghost sm" @click="loadOverview"><Icon name="refresh" :class="{ spin: state.overview.loading }" />刷新计数</button>
    </div>
    <div class="mute2" style="margin-bottom: 12px">
      这张图是从后端的迁移表渲染出来的，不是画上去的：节点 = <code>meta.states</code>，
      箭头 = <code>meta.transitions</code>。点击任一状态可看它的合法去向与来源。
      <template v-if="!state.overview.data">（当前计数需要坐席凭据 —— 图形本身不需要。）</template>
    </div>

    <div class="sm-grid">
      <div v-for="s in (meta ? meta.states : [])" :key="s" class="sm-node"
           :class="{ current: focus === s, terminal: meta.terminal_states.includes(s) }"
           @click="state.machineFocus = focus === s ? null : s">
        <div class="sm-name">
          <i class="dot" :style="{ background: `var(--state-${s})`, width: '8px', height: '8px', borderRadius: '50%' }" />
          {{ stateLabel(s) }}
          <span class="mute2">{{ s }}</span>
        </div>
        <div class="sm-count">
          {{ byState[s] !== undefined ? byState[s] + ' 张' : '—' }}
          <template v-if="!meta.terminal_states.includes(s)"> · {{ meta.transitions[s].length }} 条出边</template>
          <template v-else> · 终态</template>
        </div>
        <div class="sm-edges">
          <span v-for="t in meta.transitions[s]" :key="t" class="sm-mini"
                :class="{ reach: focus === s }">→ {{ t }}</span>
          <span v-if="!meta.transitions[s].length" class="sm-mini">无出边</span>
        </div>
      </div>
    </div>
  </div>

  <div v-if="focus" class="card">
    <div class="card-head">
      <h3>聚焦：{{ stateLabel(focus) }}（{{ focus }}）</h3>
      <span class="spacer" />
      <span v-if="byState[focus] !== undefined" class="tag soft">当前 {{ byState[focus] }} 张 / 共 {{ total }} 张</span>
      <button class="btn ghost sm" @click="state.machineFocus = null"><Icon name="x" />取消聚焦</button>
    </div>
    <div class="grid cols-2">
      <div>
        <div class="mute2" style="margin-bottom: 6px">可从此状态迁移到（后端 TRANSITIONS）</div>
        <div class="row wrap">
          <template v-if="reach.length">
            <Tag v-for="t in reach" :key="t" kind="state" :value="t" />
          </template>
          <span v-else class="mute2">无 —— 终态，任何进入它的迁移都会被拒（422）。</span>
        </div>
      </div>
      <div>
        <div class="mute2" style="margin-bottom: 6px">可进入此状态的来源</div>
        <div class="row wrap">
          <template v-if="incoming.length">
            <Tag v-for="t in incoming" :key="t" kind="state" :value="t" />
          </template>
          <span v-else class="mute2">无 —— 初始状态（建单时落此态）。</span>
        </div>
      </div>
    </div>
    <div class="divider" />
    <div class="mute2">
      状态真值始终在数据库（<code>tickets.state</code>）；这张表只回答"合法性"。
      每一次合法迁移会往 <code>ticket_events</code> 写一条审计（谁 / 何时 / 依据什么理由）。
    </div>
  </div>
</template>
