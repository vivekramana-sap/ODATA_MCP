'use client'

import { useState, useEffect } from 'react'
import type { ODataService, ProbeResult } from '@/lib/types'
import { probeService } from '@/lib/api'

interface Props {
  service?: ODataService | null
  defaultGroup?: string
  existingGroups?: string[]
  existingAliases?: string[]
  onSave: (data: ODataService) => void
  onClose: () => void
}

const BLANK: ODataService = {
  alias: '',
  url: '',
  username: '${ODATA_USERNAME}',
  password: '${ODATA_PASSWORD}',
  passthrough: true,
  readonly: false,
  default_top: 50,
}

function RevealInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const [show, setShow] = useState(false)
  return (
    <div className="relative flex">
      <input
        type={show ? 'text' : 'password'}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="input pr-9 w-full"
      />
      <button
        type="button"
        onClick={() => setShow(s => !s)}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-secondary text-xs"
      >
        {show ? 'hide' : 'show'}
      </button>
    </div>
  )
}

const CREDS_KEY = 'odata_svc_creds'

function genAlias(existingAliases: string[]): string {
  const existing = new Set(existingAliases)
  for (let n = 1; n <= 999; n++) {
    const candidate = `svc_${String(n).padStart(3, '0')}`
    if (!existing.has(candidate)) return candidate
  }
  return `svc_${Date.now() % 10000}`
}

export default function ServiceModal({ service, defaultGroup, existingGroups = [], existingAliases = [], onSave, onClose }: Props) {
  const isEdit = !!service
  const [form, setForm] = useState<ODataService>(() => {
    if (service) return { ...BLANK, ...service }
    return { ...BLANK, alias: genAlias(existingAliases), group: defaultGroup || '' }
  })
  const [showAdvanced, setShowAdvanced] = useState(!!(service?.username && !service.username.startsWith('${')))
  const [probe, setProbe]       = useState<ProbeResult | null>(null)
  const [probing, setProbing]   = useState(false)
  const [selEs, setSelEs]       = useState<Set<string> | null>(null)
  const [selAct, setSelAct]     = useState<Set<string> | null>(null)
  const [groupIsNew, setGroupIsNew] = useState(
    () => !!(service?.group && !existingGroups.includes(service.group))
  )
  const [rememberCreds, setRememberCreds] = useState(false)

  // Load saved credentials on mount (new service only)
  useEffect(() => {
    if (isEdit) return
    try {
      const saved = localStorage.getItem(CREDS_KEY)
      if (saved) {
        const { username, password } = JSON.parse(saved) as { username?: string; password?: string }
        if (username || password) {
          setForm(f => ({ ...f, username: username || f.username, password: password || f.password }))
          setRememberCreds(true)
        }
      }
    } catch { /* ignore */ }
  }, [isEdit])

  const set = <K extends keyof ODataService>(k: K, v: ODataService[K]) =>
    setForm(f => ({ ...f, [k]: v }))

  const valid = form.alias.trim() && form.url.trim()

  const handleProbe = async () => {
    setProbing(true)
    try {
      const result = await probeService(form)
      setProbe(result)
      if (result.success) {
        const allEs  = result.entity_sets.map(e => e.name)
        const allAct = result.actions || []
        setSelEs(new Set(form.include?.length ? form.include : allEs))
        setSelAct(new Set(form.include_actions?.length ? form.include_actions : allAct))
      }
    } catch { /* handled via probe.success */ }
    setProbing(false)
  }

  const toggleEs  = (n: string) => setSelEs(s  => { if (!s) return s; const x = new Set(s); x.has(n) ? x.delete(n) : x.add(n); return x })
  const toggleAct = (n: string) => setSelAct(s => { if (!s) return s; const x = new Set(s); x.has(n) ? x.delete(n) : x.add(n); return x })

  const handleSave = () => {
    // Persist/clear credentials in localStorage
    if (rememberCreds && form.username && form.password) {
      try { localStorage.setItem(CREDS_KEY, JSON.stringify({ username: form.username, password: form.password })) } catch { /* ignore */ }
    } else if (!rememberCreds) {
      try { localStorage.removeItem(CREDS_KEY) } catch { /* ignore */ }
    }
    const data: ODataService = { ...form }
    if (data.default_top === 50) delete data.default_top
    if (probe?.success && selEs !== null) {
      const allEs  = probe.entity_sets.map(e => e.name)
      const allAct = probe.actions || []
      const incEs  = selEs.size  < allEs.length  ? [...selEs]  : undefined
      const incAct = selAct && selAct.size < allAct.length ? [...selAct] : undefined
      data.include         = incEs
      data.include_actions = incAct
    }
    onSave(data)
  }

  const allEs  = probe?.success ? probe.entity_sets       : []
  const allAct = probe?.success ? (probe.actions || []) : []

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={e => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-surface-2 border border-border rounded-xl p-6 w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-2xl">
        {/* Header */}
        <div className="flex items-center mb-5">
          <h2 className="text-base font-semibold flex-1">{isEdit ? 'Edit Service' : 'Add Service'}</h2>
          <button onClick={onClose} className="text-text-muted hover:text-text-primary text-lg leading-none">✕</button>
        </div>

        {/* Alias */}
        <Field label="Alias" required>
          <input
            className="input w-full font-mono"
            value={form.alias}
            onChange={e => set('alias', e.target.value.replace(/\s/g, '_'))}
            placeholder="e.g. sales_order"
          />
          <p className="hint mt-1">
            Short unique name. Becomes the prefix for every generated tool —
            e.g. <code className="font-mono text-text-secondary">{form.alias || 'alias'}_filter_SalesOrder</code>
          </p>
        </Field>

        {/* URL */}
        <Field label="OData Service URL" required>
          <input className="input w-full" value={form.url} onChange={e => set('url', e.target.value)} placeholder="https://host/sap/opu/odata4/…" />
        </Field>

        {/* Options */}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Read-only">
            <Toggle checked={!!form.readonly} onChange={v => set('readonly', v)} label="No write / delete tools" />
          </Field>
          <Field label="Default Result Limit">
            <input
              type="number" min={1} max={10000}
              className="input w-full"
              value={form.default_top ?? 50}
              onChange={e => set('default_top', e.target.value ? parseInt(e.target.value) : 50)}
            />
          </Field>
        </div>

        {/* Credentials */}
        <div className="mb-4">
          <button
            type="button"
            onClick={() => setShowAdvanced(v => !v)}
            className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text-secondary transition-colors"
          >
            <span className={`transition-transform ${showAdvanced ? 'rotate-90' : ''}`}>▶</span>
            OData credentials
          </button>
          {showAdvanced && (
            <div className="mt-3 p-3 bg-surface-1 border border-border rounded-lg space-y-3">
              <p className="text-xs text-text-muted">
                Use env-var placeholders <code className="font-mono text-text-secondary">${'{ODATA_USERNAME}'}</code> &amp; <code className="font-mono text-text-secondary">${'{ODATA_PASSWORD}'}</code>, or enter static values.
              </p>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Username">
                  <input className="input w-full" value={form.username || ''} onChange={e => set('username', e.target.value)} placeholder="${ODATA_USERNAME}" />
                </Field>
                <Field label="Password">
                  <RevealInput value={form.password || ''} onChange={v => set('password', v)} placeholder="${ODATA_PASSWORD}" />
                </Field>
              </div>
              <div className="flex items-center justify-between">
                <Field label="Passthrough Auth">
                  <Toggle checked={!!form.passthrough} onChange={v => set('passthrough', v)} label="Forward caller's token to OData" />
                </Field>
                <label className="flex items-center gap-1.5 text-xs text-text-muted cursor-pointer select-none shrink-0 mt-5">
                  <input
                    type="checkbox"
                    checked={rememberCreds}
                    onChange={e => setRememberCreds(e.target.checked)}
                    className="accent-gold"
                  />
                  Remember locally
                </label>
              </div>
            </div>
          )}
        </div>

        {/* Group */}
        <Field label="MCP Group">
          <select
            className="input w-full"
            value={groupIsNew ? '__new__' : (form.group || '')}
            onChange={e => {
              const v = e.target.value
              if (v === '__new__') {
                setGroupIsNew(true)
                set('group', '')
              } else {
                setGroupIsNew(false)
                set('group', v)
              }
            }}
          >
            <option value="">/mcp — default endpoint</option>
            {existingGroups.map(g => (
              <option key={g} value={g}>/mcp/{g}</option>
            ))}
            <option value="__new__">+ New group…</option>
          </select>
          {groupIsNew && (
            <input
              className="input w-full font-mono mt-2"
              value={form.group || ''}
              onChange={e => set('group', e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ''))}
              placeholder="group-name  (letters, digits, - and _)"
              autoFocus
            />
          )}
          <div className="mt-2 flex items-center gap-2 px-3 py-2 bg-surface-1 border border-border rounded-lg">
            <span className="text-xs text-text-muted shrink-0">Routes to:</span>
            <code className="font-mono text-xs text-gold">
              {form.group ? `/mcp/${form.group}` : '/mcp'}
            </code>
          </div>
        </Field>

        {/* Probe section */}
        <div className="border-t border-border pt-4 mt-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider flex-1">Entity & Action Filter</span>
            {!probe && form.include && (
              <span className="badge-gold text-xs">{form.include.length} entities</span>
            )}
            <button
              onClick={handleProbe}
              disabled={probing || !form.url.trim()}
              className="px-2.5 py-1 text-xs rounded border border-border hover:border-gold hover:text-gold disabled:opacity-50 transition-colors"
            >
              {probing ? 'Probing…' : 'Test & Probe'}
            </button>
          </div>

          {!probe && (
            <p className="text-xs text-text-muted">
              {form.include
                ? `Current filter: ${form.include.length} entity set(s). Click Probe to modify.`
                : 'Probe the service to select entity sets and actions to expose as tools.'}
            </p>
          )}

          {probe && !probe.success && (
            <div className="bg-surface-3 border border-status-red/30 rounded-lg p-3">
              <p className="text-status-red font-medium text-sm mb-1">Connection failed</p>
              <p className="text-xs text-text-secondary">{probe.error}</p>
              {probe.hint === 'dns' && (
                <p className="text-xs text-gold mt-2">Internal hostname — check VPN.</p>
              )}
            </div>
          )}

          {probe?.success && selEs !== null && (
            <div className="space-y-3 mt-1">
              {allEs.length > 0 && (
                <CheckList
                  title={`Entity Sets (${selEs.size}/${allEs.length})`}
                  items={allEs.map(e => ({ id: e.name, label: e.name, meta: e.keys?.length ? `[${e.keys.join(',')}]` : '' }))}
                  selected={selEs}
                  onSelectAll={() => setSelEs(new Set(allEs.map(e => e.name)))}
                  onSelectNone={() => setSelEs(new Set())}
                  onToggle={toggleEs}
                />
              )}
              {allAct.length > 0 && (
                <CheckList
                  title={`Actions (${selAct?.size ?? 0}/${allAct.length})`}
                  items={allAct.map(a => ({ id: a, label: a, meta: '' }))}
                  selected={selAct ?? new Set()}
                  onSelectAll={() => setSelAct(new Set(allAct))}
                  onSelectNone={() => setSelAct(new Set())}
                  onToggle={toggleAct}
                />
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-2 mt-6">
          <button onClick={onClose} className="btn-ghost text-sm">Cancel</button>
          <button onClick={handleSave} disabled={!valid} className="btn-gold text-sm">Save</button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <label className="block text-xs font-medium text-text-muted mb-1.5">
        {label}{required && <span className="text-gold ml-1">*</span>}
      </label>
      {children}
    </div>
  )
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer select-none text-sm text-text-secondary">
      <span
        onClick={() => onChange(!checked)}
        className={`relative inline-block w-9 h-5 rounded-full transition-colors cursor-pointer
          ${checked ? 'bg-gold' : 'bg-surface-4'}`}
      >
        <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${checked ? 'translate-x-4' : ''}`} />
      </span>
      {label}
    </label>
  )
}

interface CheckItem { id: string; label: string; meta: string }

function CheckList({ title, items, selected, onSelectAll, onSelectNone, onToggle }: {
  title: string
  items: CheckItem[]
  selected: Set<string>
  onSelectAll: () => void
  onSelectNone: () => void
  onToggle: (id: string) => void
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">{title}</span>
        <span className="flex gap-3 text-xs">
          <button onClick={onSelectAll}  className="text-gold hover:underline">all</button>
          <button onClick={onSelectNone} className="text-gold hover:underline">none</button>
        </span>
      </div>
      <div className="max-h-36 overflow-y-auto bg-surface-1 border border-border rounded-lg p-2 space-y-0.5">
        {items.map(item => (
          <label key={item.id} className="flex items-center gap-2 px-1 py-0.5 rounded hover:bg-surface-3 cursor-pointer text-sm">
            <input
              type="checkbox"
              checked={selected.has(item.id)}
              onChange={() => onToggle(item.id)}
              className="accent-gold"
            />
            <span className="font-medium">{item.label}</span>
            {item.meta && <span className="text-xs text-text-muted">{item.meta}</span>}
          </label>
        ))}
      </div>
    </div>
  )
}
