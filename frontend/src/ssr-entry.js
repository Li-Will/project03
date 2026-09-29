// SSR 烟测入口：把组件树渲染成字符串，用来兜"白屏级"错误（模板写错、字段访问越界、
// 未定义的 props 等）。它**不是验收** —— 布局、动效、交互只有人打开浏览器才算数。
//
// 浏览器构建不会打包本文件（index.html 只引 main.js）。
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import App from './App.vue'
import { state } from './store.js'

export { state }

/** 渲染一次；setup(state) 可先摆状态（SSR 不执行 onMounted，所以不会发请求）。 */
export async function render(setup) {
  if (setup) setup(state)
  return await renderToString(createSSRApp(App))
}
