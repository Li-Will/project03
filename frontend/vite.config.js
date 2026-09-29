import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 单入口，不拆两个 HTML：
//   用户「在线客服」与坐席「待办队列/工单列表」是**同一套权限下的不同面板**（左侧导航切换），
//   首屏体积的大头来自"不引组件库"（见 src/styles.css 顶部说明），拆入口带来的边际收益很小。
//
// base 显式设为 '/'：FastAPI 用绝对路径托管 /assets/*；子路径托管时漏设 base 会白屏 —— 这是
//   项目一踩过的坑，写在这里当护栏。
export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 700,
  },
})
