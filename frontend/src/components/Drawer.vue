<script setup>
// 通用抽屉：遮罩 + 右滑面板。pointer 事件直接写 DOM 层，不引动画库。
import Icon from './Icon.vue'

defineProps({
  title: { type: String, default: '' },
  narrow: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])
</script>

<template>
  <div class="mask" @click="emit('close')" />
  <aside class="drawer" :class="{ 'panel-sm': narrow }">
    <header class="drawer-head">
      <strong>{{ title }}</strong>
      <span class="spacer" />
      <slot name="head" />
      <button class="btn ghost sm" title="关闭 (Esc)" @click="emit('close')"><Icon name="x" /></button>
    </header>
    <div class="drawer-body"><slot /></div>
  </aside>
</template>
