<script setup>
// 分布条：给的是一组 {key, label, value, tone?}，宽度按最大值归一（不是总和 ——
// 这里看的是"哪个最多"，不是"占比"）。
import { computed } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  emptyText: { type: String, default: '暂无数据' },
})

const max = computed(() => Math.max(1, ...props.items.map((i) => i.value || 0)))
const total = computed(() => props.items.reduce((a, b) => a + (b.value || 0), 0))
</script>

<template>
  <div v-if="total === 0" class="empty">{{ emptyText }}</div>
  <div v-else>
    <div v-for="it in items" :key="it.key" class="dist-row">
      <span class="lab">
        <i v-if="it.tone" class="dot" :style="{ background: it.tone, width: '7px', height: '7px', borderRadius: '50%', display: 'inline-block' }" />
        {{ it.label }}
      </span>
      <div class="bar"><i :style="{ width: ((it.value || 0) / max) * 100 + '%', background: it.tone || 'var(--brand)' }" /></div>
      <span class="val">{{ it.value || 0 }}</span>
    </div>
  </div>
</template>
