<script setup>
// 工单详情：基本信息 + 状态迁移审计时间线 + 消息流 + 人工接管。
//
// 这是本项目最值得给人看的一屏：**状态迁移审计**（from → to + actor + reason + 时间）
// 是业务审计，不是运行日志 —— 谁在什么时候把单从什么状态推到什么状态、依据什么理由，
// 全部落在 ticket_events 表里（每一次合法迁移写一条，非法迁移根本不会落库）。
import { computed } from 'vue'
import Icon from './Icon.vue'
import Tag from './Tag.vue'
import SlaBar from './SlaBar.vue'
import { state, canReply, sendHumanReply, closeDetail, loadTicketDetail } from '../store.js'
import { fmtTime, intentLabel, stateLabel } from '../meta.js'

const d = computed(() => state.detail.data)
const row = computed(() => (d.value ? { ...d.value, sla_deadline: d.value.sla_deadline || null } : null))
const nextStates = computed(() => {
  if (!d.value || !state.meta) return []
  return state.meta.transitions[d.value.state] || []
})
const isTerminal = computed(() => d.value && state.meta && state.meta.terminal_states.includes(d.value.state))
const canWrite = computed(() => canReply.value && d.value && !isTerminal.value)

const senderLabel = { customer: '客户', agent: '坐席/系统', system: '系统' }
const sourceLabel = {
  customer: '用户消息', template: '模板话术', diagnosis: '诊断方案', human: '人工回复', sla: 'SLA 提醒',
}
</script>

<template>
  <div v-if="state.detail.loading" class="skeleton" style="height: 60px" />
  <div v-else-if="state.detail.error" class="empty">
    <Icon name="alert" /><br />{{ state.detail.error }}<br />
    <span class="mute2">跨租户或不存在都返回 404 —— 不泄露"这张单是否存在"。</span>
  </div>
  <div v-else-if="d">
    <div class="row wrap">
      <h3 style="margin: 0">工单 #{{ d.id }}</h3>
      <Tag kind="state" :value="d.state" />
      <Tag kind="prio" :value="d.priority" />
      <span class="tag soft">{{ intentLabel(d.intent) }}</span>
      <span class="tag soft">{{ d.category || '未分类' }}</span>
      <span class="spacer" />
      <button class="btn ghost sm" @click="loadTicketDetail(d.id)"><Icon name="refresh" /></button>
    </div>
    <div class="mute2" style="margin-top: 4px">
      创建 {{ fmtTime(d.created_at) }} · 处理人 {{ d.assignee || '—' }}
      <span class="mute2">（详情端点不返回 tenant_id：租户归属看右上角身份，跨租户本来就直接 404）</span>
    </div>

    <div class="card" style="margin-top: 14px; padding: 12px 14px; background: var(--bg-subtle)">
      <div class="row wrap">
        <strong style="font-size: 13px">状态机</strong>
        <span class="mute2">当前：{{ stateLabel(d.state) }}</span>
        <span class="spacer" />
        <template v-if="isTerminal"><Tag kind="soft" text="终态：不可再迁移" /></template>
        <template v-else>
          <span class="mute2">合法后续：</span>
          <Tag v-for="s in nextStates" :key="s" kind="state" :value="s" />
        </template>
      </div>
      <SlaBar v-if="row" :row="row" :meta="state.meta" />
      <div class="mute2" style="margin-top: 6px">
        合法迁移表来自 <code>/api/v1/meta</code>（后端 <code>biz/states.py</code> 的 TRANSITIONS），
        非法迁移在服务端直接被拒（422），前端只是提前把可选项画出来。
      </div>
    </div>

    <div class="card" style="margin-top: 14px">
      <div class="card-head"><h3>状态迁移审计</h3>
        <span class="hint">ticket_events · 谁 · 何时 · 依据什么理由</span></div>
      <div v-if="!d.events.length" class="empty">无迁移记录</div>
      <div v-else class="timeline">
        <div v-for="(e, i) in d.events" :key="i" class="tl-item" :data-s="e.to">
          <div class="tl-top">
            <Tag v-if="e.from" kind="state" :value="e.from" />
            <span v-else class="tag soft">创建</span>
            <span class="tl-arrow">→</span>
            <Tag kind="state" :value="e.to" />
            <span class="mute2">{{ e.actor }}</span>
          </div>
          <div class="tl-meta">{{ fmtTime(e.at) }}</div>
          <div class="tl-reason">{{ e.reason }}</div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top: 14px">
      <div class="card-head"><h3>消息流</h3><span class="hint">{{ d.messages.length }} 条</span></div>
      <div v-if="!d.messages.length" class="empty">暂无消息</div>
      <div v-else style="display: flex; flex-direction: column; gap: 10px">
        <div v-for="(m, i) in d.messages" :key="i" class="bubble"
             :class="m.sender === 'customer' ? 'user' : 'bot'" style="max-width: 92%">
          <div class="bmeta">
            <span>{{ senderLabel[m.sender] || m.sender }}</span>
            <span class="tag soft" :title="'来源标记：' + (m.source || '')">{{ sourceLabel[m.source] || m.source }}</span>
            <span>{{ fmtTime(m.at) }}</span>
          </div>
          {{ m.content }}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top: 14px">
      <div class="card-head"><h3>人工接管</h3>
        <span class="hint">POST /tickets/{{ d.id }}/human-reply</span></div>

      <div v-if="!canWrite" class="row wrap" style="margin-bottom: 10px">
        <Tag kind="soft" :text="isTerminal ? '终态工单不可回复' : (canReply ? '需要凭据' : '当前角色只读')" />
        <span class="mute2">
          <template v-if="isTerminal">closed / rejected 是终态：服务端会返回 422。</template>
          <template v-else>
            写操作仅限 agent 角色（viewer 只读）。右上角「坐席凭据」填入
            <code>key:租户:坐席名[:角色]</code> 中定义的那把 key。
          </template>
        </span>
      </div>

      <textarea v-model="state.detail.reply" class="textarea" :disabled="!canWrite"
                placeholder="以坐席身份回复客户…（身份取自 X-API-Key，请求体里不带 actor）" />
      <div class="row" style="margin-top: 10px">
        <button class="btn primary" :disabled="!canWrite || state.detail.sending || !state.detail.reply.trim()"
                @click="sendHumanReply">
          <Icon :name="state.detail.sending ? 'loader' : 'send'" :class="{ spin: state.detail.sending }" />回复并恢复流程
        </button>
        <span class="mute2">若该单有挂起的 LangGraph 诊断，回复会 resume 它继续走完；否则走快速路径。</span>
      </div>
    </div>

    <div class="row" style="margin-top: 14px">
      <button class="btn ghost" @click="closeDetail">关闭</button>
    </div>
  </div>
</template>
