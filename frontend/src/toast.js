import { reactive } from 'vue'

export const toasts = reactive([])
let seq = 0

export function toast(message, kind = 'info', ttl = 4200) {
  const item = { id: ++seq, message: String(message), kind }
  toasts.push(item)
  if (typeof window !== 'undefined') {
    setTimeout(() => dismiss(item.id), ttl)
  }
  return item.id
}

export function dismiss(id) {
  const i = toasts.findIndex((t) => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}

toast.ok = (m, ttl) => toast(m, 'ok', ttl)
toast.error = (m, ttl) => toast(m, 'error', ttl)
toast.warn = (m, ttl) => toast(m, 'warn', ttl)
