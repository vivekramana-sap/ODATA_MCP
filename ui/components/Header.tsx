'use client'

import { useState } from 'react'
import type { BridgeStatus } from '@/lib/types'
import { startBridge, stopBridge } from '@/lib/api'
import { showToast } from './Toast'

interface Props {
  bridge: BridgeStatus
  mcpPort?: number
  onBridgeChange: () => void
}

export default function Header({ bridge, mcpPort = 7777, onBridgeChange }: Props) {
  const [starting, setStarting] = useState(false)

  const handleStart = async () => {
    setStarting(true)
    try {
      const res = await startBridge()
      if (res.ok) {
        showToast(`Bridge started (PID ${res.pid})`)
        onBridgeChange()
      } else {
        showToast(res.error || 'Failed to start bridge', 'error')
      }
    } catch {
      showToast('Failed to start bridge', 'error')
    }
    setStarting(false)
  }

  const handleStop = async () => {
    try {
      await stopBridge()
      onBridgeChange()
      showToast('Bridge stopped', 'info')
    } catch {
      showToast('Failed to stop bridge', 'error')
    }
  }

  return (
    <header className="flex items-center gap-4 px-6 h-14 bg-surface-1 border-b border-border sticky top-0 z-40">
      {/* Logo + title */}
      <div className="flex items-center gap-2.5 flex-1">
        <span className="text-gold font-bold text-sm tracking-widest uppercase">MCP</span>
        <span className="text-border-DEFAULT text-xs">|</span>
        <span className="text-text-secondary text-sm">JAM OData Bridge</span>
      </div>

      {/* Bridge status */}
      <div className="flex items-center gap-2 text-xs text-text-muted">
        <span className={`inline-block w-2 h-2 rounded-full ${bridge.running ? 'bg-status-green shadow-[0_0_6px_#22c55e]' : 'bg-surface-4'}`} />
        {bridge.running
          ? <span className="text-text-secondary">port {mcpPort}{bridge.pid ? ` · PID ${bridge.pid}` : ''}</span>
          : <span>stopped</span>}
      </div>

      {/* Action button */}
      {bridge.running ? (
        <button
          onClick={handleStop}
          className="px-3 py-1.5 text-xs rounded bg-surface-3 border border-border hover:border-status-red hover:text-status-red transition-colors"
        >
          ■ Stop
        </button>
      ) : (
        <button
          onClick={handleStart}
          disabled={starting}
          className="px-3 py-1.5 text-xs rounded bg-gold text-black font-semibold hover:bg-gold-hover disabled:opacity-50 transition-colors"
        >
          {starting ? '…' : '▶ Start'}
        </button>
      )}
    </header>
  )
}
