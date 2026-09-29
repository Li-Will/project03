import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'
import { applyTheme } from './store.js'

// 主题先落地再挂载：避免首帧白闪（auto 模式下按 prefers-color-scheme 决定）
applyTheme()
createApp(App).mount('#app')
