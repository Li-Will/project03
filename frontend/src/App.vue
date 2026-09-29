<script setup>
// 工作台装配：顶栏 + 左导航 + 五个面板 + 详情抽屉 + toast。
//
// 这里只做三件事：选面板、挂抽屉、处理键盘。所有业务判断都在 store.js / api.js 里，
// 组件不直接 fetch —— 免得同一份规则散落成"N 个组件各自理解后端"。
import { onMounted, onBeforeUnmount, computed } from 'vue'
import TopBar from './components/TopBar.vue'
import Sidebar from './components/Sidebar.vue'
import UserChat from './components/UserChat.vue'
import QueuePanel from './components/QueuePanel.vue'
import TicketTable from './components/TicketTable.vue'
import StateMachine from './components/StateMachine.vue'
import OverviewPanel from './components/OverviewPanel.vue'
import Drawer from './components/Drawer.vue'
import TicketDetail from './components/TicketDetail.vue'
import { state, initTheme, startClock, refreshAll, closeDetail } from './store.js'
import { toasts, dismiss } from './toast.js'

let stopClock = null
let keyHandler = null

onMounted(async () => {
  initTheme()
  stopClock = startClock()
  await refreshAll()
  if (typeof window !== 'undefined') {
    keyHandler = (e) => {
      if (e.key === 'Escape') {
        if (state.detail.id) closeDetail()
        else state.sidebarOpen = false
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        state.view = 'chat'
      }
    }
    window.addEventListener('keydown', keyHandler)
  }
})

onBeforeUnmount(() => {
  if (stopClock) stopClock()
  if (keyHandler && typeof window !== 'undefined') window.removeEventListener('keydown', keyHandler)
})

const panel = computed(() => ({
  chat: UserChat, queue: QueuePanel, tickets: TicketTable, machine: StateMachine, overview: OverviewPanel,
}[state.view] || UserChat))
</script>

<template>
  <div class="shell">
    <TopBar />
    <Sidebar />
    <main class="main">
      <component :is="panel" />
    </main>
  </div>

  <Drawer v-if="state.detail.id" :title="'工单详情 #' + state.detail.id" @close="closeDetail">
    <TicketDetail />
  </Drawer>

  <div class="toasts">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.kind" @click="dismiss(t.id)">
      {{ t.message }}
    </div>
  </div>
</template>
