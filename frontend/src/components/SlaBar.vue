<script setup>
// SLA 剩余条 + 倒计时。
// **这是展示口径**：规则（时限 / warn_ratio）来自 /api/v1/meta，权威判定在服务端的
// biz/sla.py::scan_sla —— 它到点会真的把工单迁移为 escalated；界面只是让人提前看见。
import { computed } from 'vue'
import { state } from '../store.js'
import { slaView, humanDuration } from '../meta.js'

const props = defineProps({
  row: { type: Object, required: true },
  meta: { type: Object, default: null },
  showText: { type: Boolean, default: true },
})

const view = computed(() => slaView(props.row, props.meta, state.nowMs))
const pct = computed(() => {
  const v = view.value
  if (v.total && v.remain !== null && v.remain > 0) return Math.max(2, Math.min(100, (v.remain / v.total) * 100))
  if (v.bucket === 'overdue') return 100
  return 100
})
</script>

<template>
  <div class="row" style="gap: 8px">
    <div class="bar" style="flex: 1">
      <i :class="view.bucket" :style="{ width: pct + '%' }" />
    </div>
    <span v-if="showText" class="countdown" :class="view.bucket">
      {{ view.bucket === 'overdue' ? '已超时 ' + humanDuration(-(view.remain || 0)) : humanDuration(view.remain) }}
    </span>
  </div>
</template>
