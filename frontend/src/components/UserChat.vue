<script setup>
// 用户侧「在线客服」：对话 + 诊断过程可见 + 工单卡 + 满意度闭环。
//
// 三处刻意"把真相摆在用户眼前"的地方：
//  1) 每条回复都标出 type / intent / confidence —— 用户能看到系统是"照 FAQ 答的"
//     还是"建单诊断的"还是"转人工的"，而不是一个不透明的黑箱在回话；
//  2) 建单时把诊断轨迹（检索策略 + top 分数 + 证据条数）显示出来；
//  3) 已解决的工单给"确认关闭 + 打分"入口 —— 满意度是业务闭环，不是装饰。
import { computed, ref, nextTick } from 'vue'
import Icon from './Icon.vue'
import Tag from './Tag.vue'
import { state, sendChat, newChat, ackTicket, loadTicketDetail, describe } from '../store.js'
import { intentLabel } from '../meta.js'

const draft = ref('')
const logEl = ref(null)
const ratingPick = ref({})
const acking = ref(null)

const PRESETS = [
  { text: '免费空间多大？', hint: 'FAQ 直答（不建单）' },
  { text: '会议打不开了，一直报错卡死！', hint: '报障 → 建单 + 诊断' },
  { text: '我要转人工', hint: '明确要求人工' },
  { text: '云盘里的照片好像被弄丢了，你们必须给个说法', hint: '高危前置闸：不让 FAQ 直答' },
]

const msgs = computed(() => state.chat.messages)
const lastTicket = computed(() => {
  const m = [...msgs.value].reverse().find((x) => x.ticketId)
  return m || null
})

async function scrollDown() {
  await nextTick()
  if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
}

async function submit(text) {
  const t = (text ?? draft.value).trim()
  if (!t || state.chat.busy) return
  draft.value = ''
  await sendChat(t)
  await scrollDown()
}

async function pick(p) { await submit(p.text) }

async function ack(msg) {
  const id = msg.ticketId
  const rating = ratingPick.value[id] || null
  acking.value = id
  try { await ackTicket(id, rating) } catch { /* 已在 store 里 toast */ } finally { acking.value = null }
}

function stepText(s) {
  const steps = s.diagnosis_steps || []
  if (!steps.length) return `${s.node} · 运行`
  return steps.map((x) => `步${x.step} ${x.strategy} top=${x.top_score} 证据${x.evidence_count}`).join(' | ')
}
</script>

<template>
  <div class="card" style="padding: 0; overflow: hidden">
    <div style="padding: 14px 16px; border-bottom: 1px solid var(--border)" class="row wrap">
      <div>
        <strong>在线客服</strong>
        <div class="mute2">用户侧入口：意图路由 → FAQ 直答 / 建单诊断 / 转人工</div>
      </div>
      <span class="spacer" />
      <span class="tag soft">客户：{{ state.customerName }}</span>
      <button class="btn ghost sm" @click="newChat"><Icon name="plus" />新会话</button>
    </div>

    <div style="padding: 14px 16px" class="chat-wrap">
      <div class="presets">
        <button v-for="p in PRESETS" :key="p.text" class="preset" :title="p.hint"
                :disabled="state.chat.busy" @click="pick(p)">{{ p.text }}</button>
      </div>

      <div ref="logEl" class="chat-log">
        <div v-if="!msgs.length" class="empty">
          发一条消息试试。四条预设各走一条不同的链路 —— 其中「照片被弄丢了」走的是
          <strong>高危前置闸</strong>：命中数据/账号/资金安全词表时不许 FAQ 直答，直接转人工。
        </div>

        <div v-for="(m, i) in msgs" :key="i" class="bubble" :class="m.role === 'user' ? 'user' : 'bot'">
          <template v-if="m.role === 'bot'">
            <div class="bmeta">
              <template v-if="m.pending"><Icon name="loader" :size="13" class="spin" /> 处理中…</template>
              <template v-else>
                <Tag v-if="m.type" kind="soft" :text="'type=' + m.type" />
                <Tag v-if="m.intent" kind="soft" :text="'意图 ' + intentLabel(m.intent)" />
                <Tag v-if="m.confidence !== undefined && m.confidence !== null" kind="soft"
                     :text="'置信度 ' + Number(m.confidence).toFixed(2)" />
                <Tag v-if="m.error" kind="soft" text="异常" />
              </template>
            </div>
            <div v-if="m.text">{{ m.text }}</div>

            <div v-if="m.steps && m.steps.length" class="steps">
              <div class="mute2" style="margin-bottom: 3px">诊断轨迹（LangGraph 节点逐段推送）</div>
              <div v-for="(s, j) in m.steps" :key="j" class="step-line">{{ stepText(s) }}</div>
            </div>

            <div v-if="m.citations && m.citations.length" class="cites">
              <span v-for="ct in m.citations" :key="ct.id" class="cite" :title="ct.question">
                [{{ ct.n }}] {{ ct.question }}
              </span>
            </div>

            <div v-if="m.ticketId" class="card" style="margin-top: 10px; padding: 10px 12px; background: var(--bg-subtle)">
              <div class="row wrap">
                <Icon name="ticket" :size="14" />
                <strong>工单 #{{ m.ticketId }}</strong>
                <Tag v-if="m.ticketState" kind="state" :value="m.ticketState" />
                <Tag v-if="m.priority" kind="prio" :value="m.priority" />
                <span v-if="m.category" class="mute2">{{ m.category }}</span>
                <span class="spacer" />
                <button class="btn ghost sm" @click="loadTicketDetail(m.ticketId)">查看详情</button>
              </div>
              <div v-if="m.reason" class="mute2" style="margin-top: 6px">转人工理由：{{ m.reason }}</div>

              <div v-if="m.ticketState === 'resolved' || m.ticketState === 'pending_user'" class="row wrap"
                   style="margin-top: 10px">
                <span class="mute2">满意度：</span>
                <button v-for="n in 5" :key="n" class="preset" :class="{ active: ratingPick[m.ticketId] === n }"
                        @click="ratingPick[m.ticketId] = n">{{ n }}★</button>
                <button class="btn sm primary" :disabled="acking === m.ticketId" @click="ack(m)">
                  <Icon name="check-circle" />确认已解决并关闭
                </button>
              </div>
            </div>
          </template>
          <template v-else>{{ m.text }}</template>
        </div>
      </div>

      <div class="chat-input">
        <input v-model="draft" class="input" placeholder="说点什么…（Enter 发送）"
               :disabled="state.chat.busy" @keyup.enter="submit()" />
        <button class="btn primary" :disabled="state.chat.busy || !draft.trim()" @click="submit()">
          <Icon :name="state.chat.busy ? 'loader' : 'send'" :class="{ spin: state.chat.busy }" />发送
        </button>
      </div>
      <div class="mute2" style="margin-top: 8px">
        流式接口用 <code>fetch</code> 手动读流解析 SSE —— <code>EventSource</code> 不支持自定义请求头，
        带租户凭据时会把请求打到默认租户去。
      </div>
    </div>
  </div>
</template>
