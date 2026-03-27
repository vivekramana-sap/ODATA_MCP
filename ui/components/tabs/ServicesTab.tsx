'use client'

import { useEffect, useState } from 'react'
import type { ODataService, BridgeStatus, MCPTool, CfAppStatus, BtpHealth, BridgeEndpoints } from '@/lib/types'
import { putServices, getCfApp, getBtpHealth, getBridgeEndpoints } from '@/lib/api'
import { showToast } from '@/components/Toast'
import ServiceModal from '@/components/ServiceModal'

interface Props {
  services: ODataService[]
  bridge: BridgeStatus
  tools: MCPTool[]
  onSave: () => void
}

export default function ServicesTab({ services, bridge, tools, onSave }: Props) {
  const [modal,     setModal]     = useState<{ index: number; data: ODataService | null } | null>(null)
  const [cfApp,     setCfApp]     = useState<CfAppStatus | null>(null)
  const [btp,       setBtp]       = useState<BtpHealth | null>(null)
  const [endpoints, setEndpoints] = useState<BridgeEndpoints | null>(null)
  const [loading,   setLoading]   = useState(false)

  const refreshStatus = () => {
    setLoading(true)
    Promise.all([getCfApp(), getBtpHealth()])
      .then(([cf, b]) => { setCfApp(cf); setBtp(b) })
      .finally(() => setLoading(false))
  }

  const refreshEndpoints = () => {
    getBridgeEndpoints().then(setEndpoints).catch(() => {})
  }

  useEffect(() => { refreshStatus(); refreshEndpoints() }, [])
  useEffect(() => { if (bridge.running) refreshEndpoints() }, [bridge.running])

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

  return (
    <div>
      {/* Header row */}
      <div className="flex items-center mb-5">
        <h2 className="text-base font-semibold flex-1">OData Services</h2>
        <button onClick={() => setModal({ index: -1, data: null })} className="btn-gold text-sm">
          + Add Service
        </button>
      </div>

      {/* MCP Endpoints panel */}
      <EndpointsPanel endpoints={endpoints} services={services} bridgeRunning={bridge.running} />

      {/* BTP card */}
      <div className="card mb-5 border-l-2 border-l-status-green">
        <div className="flex items-center mb-3">
          <span className="text-sm font-semibold flex-1">Deployed App</span>
          <button onClick={refreshStatus} disabled={loading} className="btn-ghost text-xs px-2 py-1">
            {loading ? '…' : '↻ Refresh'}
          </button>
        </div>
        <CfStatusRow cfApp={cfApp} loading={loading} />
        {btp && cfApp?.ok && <BtpRow btp={btp} services={services} />}
      </div>

      {/* Service list — grouped by MCP endpoint */}
      {services.length === 0 ? (
        <EmptyState icon="🔌" text="No services configured. Add one to get started." />
      ) : (
        <GroupedServiceList
          services={services}
          tools={tools}
          bridgeRunning={bridge.running}
          onEdit={(idx) => setModal({ index: idx, data: services[idx] })}
          onDelete={handleDelete}
        />
      )}

      {modal && (
        <ServiceModal
          service={modal.data}
          existingGroups={[...new Set(services.map(s => s.group).filter(Boolean) as string[])]}
          onSave={handleModalSave}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  )
}

function GroupedServiceList({ services, tools, bridgeRunning, onEdit, onDelete }: {
  services: ODataService[]
  tools: MCPTool[]
  bridgeRunning: boolean
  onEdit: (idx: number) => void
  onDelete: (idx: number) => void
}) {
  // Build ordered groups: named groups first (alphabetical), then ungrouped
  const groupMap = new Map<string, number[]>()
  services.forEach((svc, idx) => {
    const g = svc.group || ''
    if (!groupMap.has(g)) groupMap.set(g, [])
    groupMap.get(g)!.push(idx)
  })

  const namedGroups = [...groupMap.keys()].filter(Boolean).sort()
  const sections    = [...namedGroups, '']  // named groups first, then ungrouped

  return (
    <div className="space-y-5">
      {sections.map(g => {
        const indices = groupMap.get(g)
        if (!indices || indices.length === 0) return null
        return (
          <div key={g || '__default__'}>
            <div className="flex items-center gap-2 mb-2.5">
              <span className="font-mono text-xs font-semibold text-gold">
                {g ? `/mcp/${g}` : '/mcp'}
              </span>
              <span className="text-xs text-text-muted">{g ? `scoped endpoint` : `default endpoint`}</span>
              <div className="flex-1 border-t border-border/50" />
              <span className="text-xs text-text-muted">{indices.length} service{indices.length !== 1 ? 's' : ''}</span>
            </div>
            <div className="space-y-2 pl-3 border-l border-gold/20">
              {indices.map(idx => {
                const svc = services[idx]
                const toolCount = tools.filter(t => t.name.startsWith(svc.alias + '_')).length
                return (
                  <ServiceCard
                    key={idx}
                    svc={svc}
                    toolCount={toolCount}
                    bridgeRunning={bridgeRunning}
                    onEdit={() => onEdit(idx)}
                    onDelete={() => onDelete(idx)}
                  />
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ServiceCard({ svc, toolCount, bridgeRunning, onEdit, onDelete }: {
  svc: ODataService
  toolCount: number
  bridgeRunning: boolean
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <div className="card border-l-2 border-l-gold/40 hover:border-l-gold transition-colors">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5 mb-1.5">
            <span className="inline font-mono text-xs font-bold text-gold bg-gold/10 px-2 py-0.5 rounded">{svc.alias}</span>
            {svc.readonly    && <Badge color="gray">read-only</Badge>}
            {svc.include     && <Badge color="green">{svc.include.length} entities</Badge>}
            {svc.include_actions && <Badge color="green">{svc.include_actions.length} actions</Badge>}
            {svc.default_top && svc.default_top !== 50 && <Badge color="gray">top={svc.default_top}</Badge>}
            {svc.group && <Badge color="gold">/{svc.group}</Badge>}
            {bridgeRunning && toolCount > 0  && <Badge color="green">⚡ {toolCount} tools</Badge>}
            {bridgeRunning && toolCount === 0 && <Badge color="red">⚠ not loaded</Badge>}
          </div>
          <p className="font-mono text-xs text-text-muted break-all">{svc.url}</p>
        </div>
        <div className="flex gap-1.5 shrink-0">
          <button onClick={onEdit}   className="btn-ghost text-xs px-2 py-1">Edit</button>
          <button onClick={onDelete} className="text-xs px-2 py-1 rounded border border-transparent hover:border-status-red hover:text-status-red transition-colors">Del</button>
        </div>
      </div>
    </div>
  )
}

function CfStatusRow({ cfApp, loading }: { cfApp: CfAppStatus | null; loading: boolean }) {
  if (!cfApp) return <p className="text-xs text-text-muted">{loading ? 'Checking…' : 'No data'}</p>
  if (cfApp.deployed === false) return (
    <div className="flex items-center gap-2 text-xs text-text-muted"><Dot color="orange" /> Not deployed — use the Deploy tab.</div>
  )
  if (!cfApp.ok) return (
    <div className="flex items-center gap-2 text-xs text-status-red"><Dot color="red" /> {cfApp.output?.split('\n')[0] || 'Not logged in to CF'}</div>
  )
  return (
    <div className="flex flex-wrap items-center gap-4 text-xs text-text-secondary">
      <span className="flex items-center gap-1.5">
        <Dot color={cfApp.state?.toLowerCase() === 'started' ? 'green' : 'red'} />
        <span className="font-semibold">{cfApp.state}</span>
      </span>
      {cfApp.routes && (
        <a href={`https://${cfApp.routes}`} target="_blank" rel="noopener noreferrer" className="text-gold hover:underline">
          {cfApp.routes}
        </a>
      )}
      {cfApp.instances && <span>{cfApp.instances}</span>}
      {cfApp.memory    && <span>{cfApp.memory}</span>}
    </div>
  )
}

function BtpRow({ btp, services }: { btp: BtpHealth; services: ODataService[] }) {
  if (!btp.ok) return (
    <p className="text-xs text-status-red mt-2">{btp.error || 'Could not reach BTP app'}</p>
  )
  const localAliases = new Set(services.map(s => s.alias))
  const btpSet       = new Set(btp.services || [])
  const allAliases   = [...new Set([...(btp.services || []), ...services.map(s => s.alias)])]

  return (
    <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-border">
      {allAliases.map(alias => {
        const inBtp   = btpSet.has(alias)
        const inLocal = localAliases.has(alias)
        return (
          <span
            key={alias}
            title={inBtp && inLocal ? 'Running in BTP · matches local' : inBtp ? 'BTP only' : 'Local only — redeploy'}
            className={`font-mono text-xs px-2 py-0.5 rounded border
              ${inBtp && inLocal ? 'border-status-green/40 text-status-green bg-status-green/10'
              : inBtp            ? 'border-status-orange/40 text-status-orange bg-status-orange/10'
              : 'border-status-red/40 text-status-red bg-status-red/10'}`}
          >
            {inBtp ? '✓' : '✕'} {alias}
          </span>
        )
      })}
    </div>
  )
}

function EndpointsPanel({ endpoints, services, bridgeRunning }: {
  endpoints: BridgeEndpoints | null
  services: ODataService[]
  bridgeRunning: boolean
}) {
  const localGroups = new Set(services.map(s => s.group).filter(Boolean) as string[])
  const liveEndpoints = endpoints?.endpoints ?? {}
  const allGroups = new Set([...localGroups, ...Object.keys(liveEndpoints).filter(k => k !== 'default')])
  const baseUrl = liveEndpoints['default']?.replace('/mcp', '') ?? 'http://localhost:7777'

  const copyUrl = (url: string) => {
    navigator.clipboard.writeText(url).then(() => showToast('Copied!', 'success'))
  }

  // Map group → service aliases for display
  const groupAliases = (group: string) =>
    services.filter(s => (s.group || '') === group).map(s => s.alias)

  return (
    <div className="card mb-5">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-sm font-semibold flex-1">MCP Endpoints</span>
        <span className={`inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full border
          ${bridgeRunning
            ? 'border-status-green/40 text-status-green bg-status-green/10'
            : 'border-border text-text-muted bg-surface-3'}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${bridgeRunning ? 'bg-status-green' : 'bg-surface-4'}`} />
          {bridgeRunning ? 'live' : 'bridge stopped'}
        </span>
      </div>

      <div className="rounded-lg border border-border overflow-hidden">
        {/* Default row */}
        <EndpointRow
          path="/mcp"
          url={`${baseUrl}/mcp`}
          aliases={groupAliases('')}
          isDefault
          isLive={bridgeRunning}
          onCopy={copyUrl}
        />

        {[...allGroups].sort().map((group, i) => (
          <EndpointRow
            key={group}
            path={`/mcp/${group}`}
            url={`${baseUrl}/mcp/${group}`}
            aliases={groupAliases(group)}
            isDefault={false}
            isLive={bridgeRunning && group in liveEndpoints}
            onCopy={copyUrl}
            striped={i % 2 === 0}
          />
        ))}
      </div>

      {allGroups.size === 0 && (
        <p className="hint text-xs mt-2">
          Set a <code className="font-mono">group</code> on any service to create a scoped endpoint like <code className="font-mono">/mcp/products</code>.
        </p>
      )}
    </div>
  )
}

function EndpointRow({ path, url, aliases, isDefault, isLive, onCopy, striped }: {
  path: string
  url: string
  aliases: string[]
  isDefault: boolean
  isLive: boolean
  onCopy: (url: string) => void
  striped?: boolean
}) {
  return (
    <div className={`flex items-center gap-3 px-3 py-2.5 ${striped ? 'bg-surface-1' : ''} hover:bg-surface-3 transition-colors group`}>
      <code className={`font-mono text-xs font-bold shrink-0 ${isDefault ? 'text-gold' : 'text-text-secondary'}`}>
        {path}
      </code>
      <div className="flex flex-wrap gap-1 flex-1 min-w-0">
        {aliases.length > 0 ? (
          aliases.map(a => (
            <span key={a} className="font-mono text-xs bg-surface-3 border border-border px-1.5 py-0 rounded text-text-secondary">
              {a}
            </span>
          ))
        ) : (
          <span className="text-xs text-text-muted italic">
            {isDefault ? 'all services' : 'no services assigned'}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
        <span className="font-mono text-xs text-text-muted hidden xl:block">{url}</span>
        <button
          onClick={() => onCopy(url)}
          className="text-xs px-2 py-0.5 rounded border border-border hover:border-gold hover:text-gold transition-colors text-text-muted"
          title={`Copy ${url}`}
        >
          Copy
        </button>
      </div>
    </div>
  )
}

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

function Dot({ color }: { color: 'green' | 'red' | 'orange' }) {
  const cls = { green: 'bg-status-green', red: 'bg-status-red', orange: 'bg-status-orange' }[color]
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} />
}

function EmptyState({ icon, text }: { icon: string; text: string }) {
  return (
    <div className="text-center py-16 text-text-muted">
      <div className="text-4xl mb-3">{icon}</div>
      <p className="text-sm">{text}</p>
    </div>
  )
}
