<script setup>
// 顶栏：就绪状态 + 坐席身份 + 凭据入口 + 主题。
//
// 凭据只进 sessionStorage（见 api.js）：关掉标签页就没了 —— 本工作台是演示/运维界面，
// 不该在磁盘上留一份长期有效的坐席 key。
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import Drawer from './Drawer.vue'
import { state, isStaff, canReply, saveKey, setTheme } from '../store.js'

const showKey = ref(false)
const keyDraft = ref('')
const customerDraft = ref('')

const themeIcon = computed(() => (state.theme === 'light' ? 'sun' : state.theme === 'dark' ? 'moon' : 'sliders'))
const themeText = computed(() => ({ auto: '跟随系统', light: '浅色', dark: '深色' }[state.theme]))
const identityText = computed(() => {
  if (!state.identity) return state.identityError ? '未识别身份' : '加载中…'
  const i = state.identity
  return `${i.tenant} · ${i.actor} · ${i.role}`
})

function openKey() {
  keyDraft.value = state.apiKey || ''
  customerDraft.value = state.customerName
  showKey.value = true
}
async function submitKey() {
  await saveKey(keyDraft.value)
  showKey.value = false
}
function cycleTheme() {
  const order = ['auto', 'dark', 'light']
  setTheme(order[(order.indexOf(state.theme) + 1) % order.length])
}
</script>

<template>
  <header class="topbar">
    <button class="btn ghost sm side-toggle" @click="state.sidebarOpen = !state.sidebarOpen" title="菜单">
      <Icon name="menu" />
    </button>
    <div class="brand">
      <Icon name="ticket" :size="20" />
      <span>Ticket Agent<small> · 客服工单工作台</small></span>
    </div>
    <span class="spacer" />

    <span class="tag soft" :title="state.runtime.problems.join('；') || '依赖就绪'">
      <i class="dot" :style="{ background: state.runtime.ready ? 'var(--ok)' : 'var(--danger)' }" />
      {{ state.runtime.ready ? '就绪' : '依赖异常' }}
    </span>
    <span class="tag soft" :title="state.meta ? '业务规则来自 /api/v1/meta' : '未加载'">
      <Icon name="info" :size="13" /> v{{ state.runtime.version || '—' }}
    </span>
    <span class="tag soft" :title="isStaff ? '来自 X-API-Key' : '未带坐席凭据：坐席端点不可用'">
      <Icon :name="isStaff ? 'shield' : 'user'" :size="13" />
      {{ identityText }}
      <template v-if="isStaff && !canReply">（只读）</template>
    </span>

    <button class="btn ghost sm" :title="'主题：' + themeText" @click="cycleTheme">
      <Icon :name="themeIcon" /><span style="font-size: 12.5px">{{ themeText }}</span>
    </button>
    <button class="btn sm" @click="openKey"><Icon name="key" />坐席凭据</button>
  </header>

  <Drawer v-if="showKey" title="坐席凭据（X-API-Key）" narrow @close="showKey = false">
    <label class="field">
      <span>X-API-Key</span>
      <input v-model="keyDraft" class="input" type="password" placeholder="留空 = 匿名（坐席端点不可用）"
             @keyup.enter="submitKey" />
    </label>
    <label class="field" style="margin-top: 10px">
      <span>客户名（用户侧对话归属的客户）</span>
      <input v-model="customerDraft" class="input" placeholder="演示用户" />
    </label>
    <div class="row" style="margin-top: 14px">
      <button class="btn primary" @click="submitKey"><Icon name="check" />保存并刷新</button>
      <button class="btn ghost" @click="showKey = false">取消</button>
    </div>

    <div class="divider" />
    <div class="mute2" style="line-height: 1.75">
      <p style="margin: 0 0 8px">
        <strong>身份从凭据来，不从请求体来。</strong>凭据解析在后端 <code>api/auth.py</code>：
        key 的格式是 <code>key:租户:坐席名[:角色]</code>，角色缺省为 agent，
        viewer 只能读不能写。
      </p>
      <p style="margin: 0 0 8px">
        未启用鉴权（<code>REQUIRE_AUTH=false</code>，默认）时：坐席端点接受匿名调用，
        身份记为 <code>local-agent</code> —— 服务端启动时会打一条 WARNING，不假装安全。
      </p>
      <p style="margin: 0">
        凭据只存在本标签页的 <code>sessionStorage</code>，关掉即失效；不会写入磁盘。
      </p>
    </div>
  </Drawer>
</template>
