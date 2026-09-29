<script setup>
// 左侧导航。队列徽标只在"确实拿到了队列数据"时才显示数字 ——
// 拿不到（没凭据 / 401）就显示一个点，而不是假装是 0（0 和"不知道"是两回事）。
import { computed } from 'vue'
import Icon from './Icon.vue'
import { state, setView, VIEWS, isStaff } from '../store.js'

const icons = { chat: 'chat', queue: 'hand', tickets: 'list', machine: 'branch', overview: 'gauge' }
const counts = computed(() => ({
  queue: state.queue.error ? null : state.queue.items.length,
}))

function badge(id) {
  const v = counts.value[id]
  return v === null || v === undefined ? null : v
}
</script>

<template>
  <nav class="sidebar" :class="{ open: state.sidebarOpen }">
    <button v-for="v in VIEWS" :key="v.id" class="nav-item" :class="{ active: state.view === v.id }"
            @click="setView(v.id)" :title="v.hint">
      <Icon :name="icons[v.id]" :size="16" />
      <span>{{ v.label }}</span>
      <span v-if="badge(v.id) !== null" class="badge-count">{{ badge(v.id) }}</span>
      <span v-else-if="v.id === 'queue' && state.queue.error" class="badge-count">?</span>
    </button>

    <div class="sidebar-foot">
      <div>{{ isStaff ? '坐席视图可用' : '匿名视图（无坐席凭据）' }}</div>
      <div>业务规则来自 /api/v1/meta</div>
      <div v-if="state.meta">状态 {{ state.meta.states.length }} 态 ·
        时限 {{ Object.keys(state.meta.sla_deadlines_seconds).join('/') }}</div>
    </div>
  </nav>
</template>
