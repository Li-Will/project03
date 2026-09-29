// SSR 构建：产出 .ssr-out/ssr-smoke.mjs + 分包。
//
// 两个坑（项目一、项目二都踩过，写在这里当护栏）：
//  1) **必须显式钉住产物的 entryFileNames**：SSR 模式默认输出 .js，而下一步脚本按 .mjs 找
//     文件 → "构建成功但 MODULE_NOT_FOUND"；
//  2) **target 必须 esnext**：默认的浏览器矩阵不接受顶层 await。
import { build } from 'vite'

await build({
  logLevel: 'warn',
  build: {
    ssr: 'src/ssr-entry.js',
    outDir: '.ssr-out',
    emptyOutDir: true,
    target: 'esnext',
    rollupOptions: {
      output: { entryFileNames: 'ssr-smoke.mjs', chunkFileNames: 'assets/[name]-[hash].mjs' },
    },
  },
})
console.log('[ssr-build] 产物：.ssr-out/ssr-smoke.mjs')
