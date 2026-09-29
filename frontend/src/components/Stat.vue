<script setup>
// 指标卡：值 + 说明。tone 只影响数值颜色，不做别的解读。
defineProps({
  k: { type: String, required: true },
  v: { type: [String, Number], default: '—' },
  s: { type: String, default: '' },
  tone: { type: String, default: '' },   // '' | ok | warn | danger
})
</script>

<template>
  <div class="stat" :class="tone">
    <div class="k">{{ k }}</div>
    <div class="v">{{ v }}</div>
    <div v-if="s" class="s">{{ s }}</div>
  </div>
</template>
