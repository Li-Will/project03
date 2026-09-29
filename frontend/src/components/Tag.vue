<script setup>
// 业务值 → 徽标。三种语义：工单状态（8 态）、优先级（P1/P2/P3）、SLA 分桶（4 档）。
// 值本身来自后端（/api/v1/meta 的枚举），这里只决定配色与中文。
import { computed } from 'vue'
import { stateLabel, priorityLabel, bucketLabel } from '../meta.js'

const props = defineProps({
  kind: { type: String, default: 'soft' },   // state | prio | sla | soft
  value: { type: String, default: '' },
  text: { type: String, default: '' },
})

const label = computed(() => {
  if (props.text) return props.text
  if (props.kind === 'state') return stateLabel(props.value)
  if (props.kind === 'prio') return priorityLabel(props.value)
  if (props.kind === 'sla') return bucketLabel(props.value)
  return props.value
})

const attrs = computed(() => {
  if (props.kind === 'state') return { 'data-s': props.value }
  if (props.kind === 'prio') return { 'data-p': props.value }
  if (props.kind === 'sla') return { 'data-k': props.value }
  return {}
})
</script>

<template>
  <span class="tag" :class="kind" v-bind="attrs">
    <i v-if="kind !== 'soft'" class="dot" />{{ label }}
  </span>
</template>
