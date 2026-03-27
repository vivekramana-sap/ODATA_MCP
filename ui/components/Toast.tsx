'use client'

import { useEffect, useState } from 'react'

export type ToastType = 'success' | 'error' | 'info'

export interface ToastMessage {
  id: number
  msg: string
  type: ToastType
}

let toastCount = 0
const listeners: Array<(t: ToastMessage) => void> = []

export function showToast(msg: string, type: ToastType = 'success') {
  const t: ToastMessage = { id: ++toastCount, msg, type }
  listeners.forEach(fn => fn(t))
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  useEffect(() => {
    const fn = (t: ToastMessage) => {
      setToasts(prev => [...prev, t])
      setTimeout(() => setToasts(prev => prev.filter(x => x.id !== t.id)), 3500)
    }
    listeners.push(fn)
    return () => { const i = listeners.indexOf(fn); if (i > -1) listeners.splice(i, 1) }
  }, [])

  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`px-4 py-3 rounded-lg text-sm font-medium shadow-lg animate-fade-in
            ${t.type === 'success' ? 'bg-status-green text-black'
            : t.type === 'error'   ? 'bg-status-red text-white'
            : 'bg-gold text-black'}`}
          style={{ animation: 'slideUp .2s ease' }}
        >
          {t.msg}
        </div>
      ))}
    </div>
  )
}
