'use client'

const safeAlias = (alias: string) => alias.replace(/[^a-zA-Z0-9_-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40) || 'svc'

import { useEffect, useState } from 'react'
import type { ODataService, BridgeStatus, MCPTool, CfAppStatus, BtpHealth } from '@/lib/types'
import { putServices, getCfApp, getBtpHealth } from '@/lib/api'
import { showToast } from '@/components/Toast'
import ServiceModal from '@/components/ServiceModal'

interface Props {
  services: ODataService[]
  bridge: BridgeStatus
  tools: MCPTool[]
  onSave: () => void
}

export default function ServicesTab({ services, bridge, tools = [], onSave }: Props) {
  const [modal, setModal] = useState<{ index: number; data: ODataService | null; defaultGroup?: string } | null>(null)
  const [cfApp,   setCfApp]   = useState<CfAppStatus | null>(null)
  const [btp,     setBtp]     = useState<BtpHealth | null>(null)
  const [loading, setLoading] = useState(false)

  const refreshStatus = () => {
    setLoading(true)
    Promise.all([getCfApp(), getBtpHealth()])
      .then(([cf, b]) => { setCfApp(cf); setBtp(b) })
      .finally(() => setLoading(false))
  }

  useEffect(() => { refreshStatus() }, [])

  const handleDelete = async (idx: number) => {
    if (!confirm('Delete this service?')) return
    await putServices(services.filter((_, i) => i !== idx))
    onSave()
    showToast('Service deleted')
  }

  const handleModalSave = async (data: ODataService) => {
    if (!modal) return
    const next = modal.index === -1
      ? [...services, data]
      : services.map((s, i) => i === modal.index ? data : s)
    await putServices(next)
    setModal(null)
    onSave()
    showToast(modal.index === -1 ? 'Service added' : 'Service updated')
    if (bridge?.running) showToast('Restart the bridge to apply changes', 'info')
  }

  const existingGroups = [...new Set(services.map(s => s.group).filter(Boolean) as string[])]
  const existingAliases = services.map(s => s.alias)

  return (
    <div className="space-y-4">
      {/* Header row */}
      <div className="flex items-center gap-3">
        <h2 className="text-base font-semibold flex-1">OData Services</h2>
        {cfApp?.routes && (
          <a
            href={`https://${cfApp.routes}`}
            target="_blank" rel="noopener noreferrer"
            className="text-xs text-gold hover:underline font-mono"
          >
            {cfApp.routes}
          </a>
        )}
        <button onClick={refreshStatus} disabled={loading} className="btn-ghost text-xs px-2 py-1">
          {loading ? '…' : '↻'}
        </button>
        <button onClick={() => setModal({ index: -1, data: null })} className="btn-gold text-sm">
          + Add Service
        </button>
      </div>

      {/* Deployment status strip */}
      <DeployStrip cfApp={cfApp} btp={btp} services={services} loading={loading} />

      {/* Accordion groups */}
      {services.length === 0 ? (
        <div className="text-center py-16 text-text-muted">
          <div className="text-4xl mb-3">🔌</div>
          <p className="text-sm">No services configured. Click "+ Add Service" to get started.</p>
        </div>
      ) : (
        <GroupAccordion
          services={services}
          tools={tools}
          bridgeRunning={bridge.running}
          cfRoutes={cfApp?.routes}
          onEdit={(idx) => setModal({ index: idx, data: services[idx] })}
          onDelete={handleDelete}
          onAddToGroup={(group) => setModal({ index: -1, data: null, defaultGroup: group })}
        />
      )}

      {modal && (
        <ServiceModal
          service={modal.data}
          defaultGroup={modal.defaultGroup}
          existingGroups={existingGroups}
          existingAliases={existingAliases}
          onSave={handleModalSave}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  )
}

// ── Accordion ──────────────────────────────────────────────────────────────

function GroupAccordion({ services, tools = [], bridgeRunning, cfRoutes, onEdit, onDelete, onAddToGroup }: {
  services: ODataService[]
  tools: MCPTool[]
  bridgeRunning: boolean
  cfRoutes?: string
  onEdit: (idx: number) => void
  onDelete: (idx: number) => void
  onAddToGroup: (group: string) => void
}) {
  const safeTools = Array.isArray(tools) ? tools : []
  const groupMap = new Map<string, number[]>()
  services.forEach((svc, idx) => {
    const g = svc.group || ''
    if (!groupMap.has(g)) groupMap.set(g, [])
    groupMap.get(g)!.push(idx)
  })

  // Named groups first (sorted), then ungrouped
  const namedGroups = [...groupMap.keys()].filter(Boolean).sort()
  const sections = [...namedGroups, '']

  // All groups open by default
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set(sections))

  const toggle = (g: string) =>
    setOpenGroups(prev => {
      const next = new Set(prev)
      next.has(g) ? next.delete(g) : next.add(g)
      return next
    })

  const baseUrl = cfRoutes ? `https://${cfRoutes}` : 'http://localhost:7777'
  const copyUrl = (url: string) =>
    navigator.clipboard.writeText(url).then(() => showToast('Copied!', 'success'))

  return (
    <div className="space-y-3">
      {sections.map(g => {
        const indices = groupMap.get(g)
        if (!indices || indices.length === 0) return null
        const endpointPath = g ? `/mcp/${g}` : '/mcp'
        const endpointUrl  = `${baseUrl}${endpointPath}`
        const isOpen = openGroups.has(g)
        const svcCount = indices.length
        const toolCount = indices.reduce((sum, idx) => {
          const alias = services[idx].alias
          return sum + safeTools.filter(t => t.name.startsWith(safeAlias(alias) + '_')).length
        }, 0)

        return (
          <div key={g || '__default__'} className="rounded-xl border border-border overflow-hidden">
            {/* Accordion header */}
            <div
              className="flex items-center gap-3 px-4 py-3 bg-surface-2 cursor-pointer hover:bg-surface-3 transition-colors select-none"
              onClick={() => toggle(g)}
            >
              {/* Chevron */}
              <span className={`text-text-muted transition-transform text-xs ${isOpen ? 'rotate-90' : ''}`}>▶</span>

              {/* Endpoint path */}
              <code className={`font-mono text-sm font-bold ${g ? 'text-gold' : 'text-text-secondary'}`}>
                {endpointPath}
              </code>

              {/* Service count badge */}
              <span className="text-xs bg-surface-3 border border-border px-2 py-0.5 rounded-full text-text-muted">
                {svcCount} service{svcCount !== 1 ? 's' : ''}
              </span>

              {/* Live tools badge */}
              {bridgeRunning && toolCount > 0 && (
                <span className="text-xs bg-status-green/10 border border-status-green/20 text-status-green px-2 py-0.5 rounded-full">
                  ⚡ {toolCount} tools
                </span>
              )}

              <div className="flex-1" />

              {/* URL copy */}
              <span className="font-mono text-xs text-text-muted hidden lg:block truncate max-w-[260px]">
                {endpointUrl}
              </span>
              <button
                onClick={e => { e.stopPropagation(); copyUrl(endpointUrl) }}
                className="shrink-0 text-xs px-2 py-0.5 rounded border border-border hover:border-gold hover:text-gold transition-colors text-text-muted"
              >
                Copy
              </button>

              {/* Add service to this group */}
              <button
                onClick={e => { e.stopPropagation(); onAddToGroup(g) }}
                className="shrink-0 text-xs px-2 py-0.5 rounded border border-gold/40 text-gold hover:bg-gold/10 transition-colors"
              >
                + Add
              </button>
            </div>

            {/* Accordion body */}
            {isOpen && (
              <div className="divide-y divide-border/60">
                {indices.map(idx => {
                  const svc = services[idx]
                  const tc = safeTools.filter(t => t.name.startsWith(safeAlias(svc.alias) + '_')).length
                  return (
                    <ServiceRow
                      key={idx}
                      svc={svc}
                      toolCount={tc}
                      bridgeRunning={bridgeRunning}
                      onEdit={() => onEdit(idx)}
                      onDelete={() => onDelete(idx)}
                    />
                  )
                })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function ServiceRow({ svc, toolCount, bridgeRunning, onEdit, onDelete }: {
  svc: ODataService
  toolCount: number
  bridgeRunning: boolean
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3 bg-surface-1 hover:bg-surface-2 transition-colors group">
      {/* Alias */}
      <span className="font-mono text-xs font-bold text-gold bg-gold/10 px-2 py-0.5 rounded shrink-0">
        {svc.alias}
      </span>

      {/* URL */}
      <span className="font-mono text-xs text-text-muted truncate flex-1 min-w-0" title={svc.url}>
        {svc.url}
      </span>

      {/* Badges */}
      <div className="hidden sm:flex items-center gap-1.5 shrink-0">
        {svc.readonly      && <Badge color="gray">read-only</Badge>}
        {svc.include       && <Badge color="green">{svc.include.length} ent.</Badge>}
        {svc.include_actions && <Badge color="green">{svc.include_actions.length} act.</Badge>}
        {bridgeRunning && toolCount > 0  && <Badge color="green">⚡ {toolCount}</Badge>}
        {bridgeRunning && toolCount === 0 && <Badge color="red">⚠ 0</Badge>}
      </div>

      {/* Actions */}
      <div className="flex gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
        <button onClick={onEdit}   className="btn-ghost text-xs px-2 py-1">Edit</button>
        <button onClick={onDelete} className="text-xs px-2 py-1 rounded hover:text-status-red transition-colors text-text-muted">Del</button>
      </div>
    </div>
  )
}

// ── Deploy status strip ────────────────────────────────────────────────────

function DeployStrip({ cfApp, btp, services, loading }: {
  cfApp: CfAppStatus | null
  btp: BtpHealth | null
  services: ODataService[]
  loading: boolean
}) {
  if (!cfApp && loading) return (
    <div className="text-xs text-text-muted py-1">Checking BTP status…</div>
  )
  if (!cfApp) return null

  const state = cfApp.state?.toLowerCase()
  const isRunning = state === 'started'

  const localAliases = new Set(services.map(s => s.alias))
  const btpAliases   = new Set(btp?.services ?? [])
  const allAliases   = [...new Set([...btpAliases, ...localAliases])]

  return (
    <div className="rounded-lg border border-border bg-surface-2 px-4 py-2.5 flex flex-wrap items-center gap-3 text-xs">
      {/* State dot */}
      <span className={`flex items-center gap-1.5 font-medium ${isRunning ? 'text-status-green' : 'text-status-orange'}`}>
        <span className={`w-2 h-2 rounded-full ${isRunning ? 'bg-status-green' : 'bg-status-orange'}`} />
        {cfApp.state ?? (cfApp.deployed === false ? 'Not deployed' : 'Unknown')}
      </span>

      {/* Instances / memory */}
      {cfApp.instances && <span className="text-text-muted">{cfApp.instances}</span>}
      {cfApp.memory    && <span className="text-text-muted">{cfApp.memory}</span>}

      <div className="flex-1" />

      {/* Service sync badges */}
      {btp?.ok && allAliases.map(alias => {
        const inBtp   = btpAliases.has(alias)
        const inLocal = localAliases.has(alias)
        return (
          <span
            key={alias}
            title={inBtp && inLocal ? 'In sync' : inBtp ? 'BTP only' : 'Local only — redeploy'}
            className={`font-mono px-1.5 py-0.5 rounded border
              ${inBtp && inLocal ? 'border-status-green/40 text-status-green bg-status-green/10'
              : inBtp            ? 'border-status-orange/40 text-status-orange bg-status-orange/10'
                                 : 'border-status-red/40 text-status-red bg-status-red/10'}`}
          >
            {inBtp ? '✓' : '✕'} {alias}
          </span>
        )
      })}

      {btp && !btp.ok && (
        <span className="text-status-red">{btp.error ?? 'BTP unreachable'}</span>
      )}
    </div>
  )
}

// ── Tiny helpers ───────────────────────────────────────────────────────────

function Badge({ color, children }: { color: 'green' | 'red' | 'orange' | 'gray' | 'gold'; children: React.ReactNode }) {
  const cls = {
    green:  'bg-status-green/10 text-status-green border-status-green/20',
    red:    'bg-status-red/10   text-status-red   border-status-red/20',
    orange: 'bg-status-orange/10 text-status-orange border-status-orange/20',
    gray:   'bg-surface-3 text-text-muted border-border',
    gold:   'bg-gold/10 text-gold border-gold/20',
  }[color]
  return <span className={`inline text-xs px-1.5 py-0.5 rounded border ${cls}`}>{children}</span>
}



