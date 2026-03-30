'use client'

/** Mirror of bridge_core/bridge.py _safe_alias — replace non-alphanumeric/underscore/dash with '_'. */
const safeAlias = (alias: string) => alias.replace(/[^a-zA-Z0-9_-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40) || 'svc'

import { useEffect, useRef, useState } from 'react'
import type { MCPTool, BridgeStatus, ODataService } from '@/lib/types'
import { callTool, startBridge, stopBridge, getBridgeStatus, getBridgeLogs, getTools } from '@/lib/api'
import { showToast } from '@/components/Toast'

interface Props {
  tools: MCPTool[]
  bridge: BridgeStatus
  services: ODataService[]
  mcpPort: number
  onBridgeChange: () => void
  onToolsRefresh: () => void
}

export default function ToolsTab({ tools = [], bridge, services, mcpPort, onBridgeChange, onToolsRefresh }: Props) {
  const [search,      setSearch]      = useState('')
  const [groupFilter, setGroupFilter] = useState<string | null>(null)
  const [selected,    setSelected]    = useState<MCPTool | null>(null)
  const [args,      setArgs]      = useState<Record<string, unknown>>({})
  const [auth,      setAuth]      = useState('')
  const [result,    setResult]    = useState<unknown>(null)
  const [calling,   setCalling]   = useState(false)
  const [starting,  setStarting]  = useState(false)
  const [logs,      setLogs]      = useState<string[]>([])
  const [showLogs,  setShowLogs]  = useState(false)
  const [copied,    setCopied]    = useState(false)
  const logsRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (logsRef.current) logsRef.current.scrollTop = logsRef.current.scrollHeight
  }, [logs])

  // Derive groups from services config
  const groups = [...new Set(services.map(s => s.group).filter(Boolean) as string[])].sort()

  // Map group → set of alias prefixes belonging to that group
  const groupAliases = (g: string) => services.filter(s => s.group === g).map(s => safeAlias(s.alias))

  const safeTools = Array.isArray(tools) ? tools : []
  const filtered = safeTools.filter(t => {
    const matchSearch = !search ||
      t.name.toLowerCase().includes(search.toLowerCase()) ||
      (t.description || '').toLowerCase().includes(search.toLowerCase())
    if (!matchSearch) return false
    if (!groupFilter) return true
    const aliases = groupAliases(groupFilter)
    return aliases.some(a => t.name.startsWith(a + '_'))
  })

  const selectTool = (t: MCPTool) => { setSelected(t); setArgs({}); setResult(null) }

  const handleCall = async () => {
    if (!selected) return
    const cleanArgs = Object.fromEntries(
      Object.entries(args).filter(([, v]) => v !== undefined && v !== '')
    )
    setCalling(true)
    try {
      const res = await callTool(selected.name, cleanArgs, auth)
      setResult(res)
    } catch (e) {
      setResult({ error: String(e) })
    }
    setCalling(false)
  }

  const handleStart = async () => {
    setStarting(true)
    setLogs([])
    setShowLogs(false)
    try {
      const res = await startBridge()
      if (res.ok) {
        showToast(`Bridge started (PID ${res.pid})`)
        let attempts = 0
        const poll = () => {
          attempts++
          getBridgeStatus().then(s => {
            onBridgeChange()
            if (!s.running && attempts <= 3) {
              getBridgeLogs().then(d => { setLogs(d.logs); setShowLogs(true) })
              showToast('Bridge stopped unexpectedly — see logs', 'error')
            } else if (s.running && attempts === 1) {
              setTimeout(poll, 1500)
            } else if (s.running && attempts === 2) {
              onToolsRefresh()
            }
          })
        }
        setTimeout(poll, 800)
      } else {
        showToast(res.error || 'Failed to start bridge', 'error')
      }
    } catch {
      showToast('Failed to start bridge', 'error')
    }
    setStarting(false)
  }

  const handleStop = async () => {
    await stopBridge()
    onBridgeChange()
    showToast('Bridge stopped', 'info')
  }

  const getDisplay = () => {
    if (!result) return ''
    try {
      const r = result as { result?: { content?: Array<{ text: string }> } }
      const text = r?.result?.content?.[0]?.text
      if (text) return JSON.stringify(JSON.parse(text), null, 2)
    } catch { /* fall through */ }
    return JSON.stringify(result, null, 2)
  }

  const handleCopy = () => {
    navigator.clipboard.writeText(getDisplay())
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="space-y-4">
      {/* Bridge bar */}
      <div className="card flex items-center gap-3">
        <span className={`w-2 h-2 rounded-full shrink-0 ${bridge.running ? 'bg-status-green shadow-[0_0_6px_#22c55e]' : 'bg-surface-4'}`} />
        <div className="flex-1">
          <p className="text-sm font-medium">
            {bridge.running ? `MCP Bridge running on port ${mcpPort}` : 'MCP Bridge is not running'}
          </p>
          {bridge.pid && <p className="text-xs text-text-muted">PID: {bridge.pid}</p>}
        </div>
        {bridge.running ? (
          <>
            <button onClick={handleStop}        className="btn-ghost text-xs px-2 py-1">■ Stop</button>
            <button onClick={onToolsRefresh}    className="btn-ghost text-xs px-2 py-1">↻ Refresh</button>
          </>
        ) : (
          <button onClick={handleStart} disabled={starting} className="btn-gold text-xs px-3 py-1.5">
            {starting ? 'Starting…' : '▶ Start Bridge'}
          </button>
        )}
      </div>

      {/* Warning */}
      {!bridge.running && (
        <div className="text-xs border-l-2 border-gold bg-gold/5 px-3 py-2 rounded-r text-text-secondary">
          Start the MCP bridge to load and test tools. The bridge reads <code className="font-mono">services.json</code> and connects to your OData endpoints.
        </div>
      )}

      {/* Startup log */}
      {logs.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wide flex-1">Startup Log</span>
            <button onClick={() => setShowLogs(s => !s)} className="btn-ghost text-xs px-2 py-1">{showLogs ? '▲ Hide' : '▼ Show'}</button>
            <button onClick={() => getBridgeLogs().then(d => setLogs(d.logs))} className="btn-ghost text-xs px-2 py-1">↻</button>
          </div>
          {showLogs && (
            <div className="log-panel max-h-48" ref={logsRef}>
              {logs.map((l, i) => (
                <div key={i} className={`log-line ${l.includes('ERROR') || l.includes('Traceback') ? 'log-err' : ''}`}>{l}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {bridge.running && safeTools.length === 0 && (
        <div className="text-xs border-l-2 border-status-orange bg-status-orange/5 px-3 py-2 rounded-r text-text-secondary flex items-center gap-3">
          No tools discovered. Check your services.
          <button onClick={onToolsRefresh} className="btn-ghost text-xs px-2 py-1">↻ Retry</button>
        </div>
      )}

      {safeTools.length > 0 && (
        <div className="grid grid-cols-[280px_1fr] gap-4">
          {/* Tool list */}
          <div className="flex flex-col gap-0">
            <div className="flex items-baseline gap-2 mb-2">
              <p className="text-xs font-semibold text-text-muted uppercase tracking-wide">
                {filtered.length} {groupFilter ? `of ${safeTools.length}` : ''} Tools
              </p>
            </div>

            {/* Group filter pills */}
            {groups.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-2">
                <button
                  onClick={() => setGroupFilter(null)}
                  className={`text-xs px-2 py-0.5 rounded-full border transition-colors
                    ${!groupFilter ? 'bg-gold text-surface-1 border-gold font-semibold' : 'border-border text-text-muted hover:border-gold hover:text-gold'}`}
                >
                  All
                </button>
                {groups.map(g => (
                  <button
                    key={g}
                    onClick={() => setGroupFilter(groupFilter === g ? null : g)}
                    className={`text-xs px-2 py-0.5 rounded-full border transition-colors font-mono
                      ${groupFilter === g ? 'bg-gold text-surface-1 border-gold font-semibold' : 'border-border text-text-muted hover:border-gold hover:text-gold'}`}
                  >
                    /{g}
                  </button>
                ))}
              </div>
            )}

            <input
              className="input text-sm rounded-b-none"
              placeholder="Search tools…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            <div className="flex-1 overflow-y-auto border border-t-0 border-border rounded-b-lg bg-surface-1 max-h-[420px]">
              {filtered.length === 0 ? (
                <p className="text-xs text-text-muted p-4 text-center">No matches</p>
              ) : filtered.map(t => {
                // Find which group this tool's service belongs to
                const ownerSvc = services.find(s => t.name.startsWith(safeAlias(s.alias) + '_'))
                const toolGroup = ownerSvc?.group
                return (
                  <button
                    key={t.name}
                    onClick={() => selectTool(t)}
                    className={`w-full text-left px-3 py-2.5 border-b border-border/50 last:border-b-0 transition-colors hover:bg-surface-3
                      ${selected?.name === t.name ? 'bg-gold/5 border-l-2 border-l-gold' : ''}`}
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      <p className="font-mono text-xs font-semibold text-gold truncate flex-1">{t.name}</p>
                      {toolGroup && (
                        <span className="shrink-0 font-mono text-xs text-text-muted bg-surface-3 border border-border px-1 rounded">
                          /{toolGroup}
                        </span>
                      )}
                    </div>
                    {t.description && <p className="text-xs text-text-muted mt-0.5 truncate">{t.description}</p>}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Right panel */}
          <div>
            {!selected ? (
              <div className="flex flex-col items-center justify-center h-40 text-text-muted text-sm">
                <span className="text-3xl mb-2">←</span>Select a tool to test it
              </div>
            ) : (
              <div className="space-y-4">
                <div>
                  <div className="flex items-start gap-2">
                    <p className="font-mono text-sm font-bold text-gold flex-1">{selected.name}</p>
                    {(() => {
                      const s = services.find(sv => selected.name.startsWith(safeAlias(sv.alias) + '_'))
                      return s?.group ? (
                        <span className="font-mono text-xs text-text-muted bg-surface-3 border border-border px-1.5 py-0.5 rounded shrink-0">
                          /mcp/{s.group}
                        </span>
                      ) : null
                    })()}
                  </div>
                  {selected.description && <p className="text-xs text-text-secondary mt-1">{selected.description}</p>}
                </div>

                <ToolForm tool={selected} args={args} setArgs={setArgs} />

                {/* Auth */}
                <div>
                  <label className="block text-xs font-medium text-text-muted mb-1.5">
                    Auth header <span className="text-text-muted font-normal">(optional)</span>
                  </label>
                  <input
                    className="input w-full text-sm font-mono"
                    placeholder="Bearer <token> or Basic <base64>"
                    value={auth}
                    onChange={e => setAuth(e.target.value)}
                  />
                </div>

                <button onClick={handleCall} disabled={calling} className="btn-gold text-sm w-full">
                  {calling ? 'Calling…' : '▶ Call Tool'}
                </button>

                {result !== null && (
                  <div className="relative">
                    <div className="code-panel max-h-96 text-xs">
                      {getDisplay()}
                    </div>
                    <button
                      onClick={handleCopy}
                      className="absolute top-2 right-2 text-xs bg-surface-3 border border-border rounded px-2 py-1 hover:border-border text-text-muted hover:text-text-primary transition-colors"
                    >
                      {copied ? 'Copied!' : 'Copy'}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ToolForm({ tool, args, setArgs }: {
  tool: MCPTool
  args: Record<string, unknown>
  setArgs: React.Dispatch<React.SetStateAction<Record<string, unknown>>>
}) {
  const props    = tool.inputSchema?.properties || {}
  const required = tool.inputSchema?.required   || []
  const set = (k: string, v: unknown) => setArgs(a => ({ ...a, [k]: v }))

  if (Object.keys(props).length === 0) {
    return <p className="text-xs text-text-muted">No input parameters.</p>
  }

  return (
    <div className="space-y-3">
      {Object.entries(props).map(([name, prop]) => {
        const isReq = required.includes(name)
        const type  = prop.type || 'string'
        return (
          <div key={name}>
            <label className="block text-xs font-medium text-text-muted mb-1">
              {name}{isReq && <span className="text-gold ml-1">*</span>}
              <span className="ml-1 text-text-muted font-normal">({type})</span>
            </label>
            {prop.description && prop.description !== name && (
              <p className="text-xs text-text-muted mb-1">{prop.description}</p>
            )}
            {type === 'boolean' ? (
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!args[name]}
                  onChange={e => set(name, e.target.checked)}
                  className="accent-gold"
                />
                <span className="text-xs text-text-secondary">true</span>
              </label>
            ) : (type === 'integer' || type === 'number') ? (
              <input
                type="number"
                className="input w-full text-sm"
                value={(args[name] as number) ?? ''}
                onChange={e => set(name, e.target.value === '' ? undefined : Number(e.target.value))}
                placeholder={prop.description || name}
              />
            ) : (
              <input
                type="text"
                className="input w-full text-sm"
                value={(args[name] as string) ?? ''}
                onChange={e => set(name, e.target.value)}
                placeholder={prop.description || name}
              />
            )}
          </div>
        )
      })}
    </div>
  )
}
