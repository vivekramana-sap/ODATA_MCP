'use client'

import { useEffect, useState } from 'react'
import type { Credentials } from '@/lib/types'
import { putCredentials } from '@/lib/api'
import { showToast } from '@/components/Toast'

interface Props {
  credentials: Credentials
  onSave: () => void
}

export default function CredentialsTab({ credentials, onSave }: Props) {
  const [form,   setForm]   = useState<Credentials>(credentials)
  const [saving, setSaving] = useState(false)

  useEffect(() => setForm(credentials), [credentials])

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  const handleSave = async () => {
    setSaving(true)
    try {
      await putCredentials(form)
      onSave()
      showToast('Credentials saved')
    } catch {
      showToast('Failed to save credentials', 'error')
    }
    setSaving(false)
  }

  return (
    <div className="max-w-lg space-y-4">
      {/* MCP gateway creds */}
      <section className="card">
        <SectionTitle>MCP Gateway Credentials</SectionTitle>
        <p className="text-xs text-text-muted mb-4 leading-relaxed">
          Protect the <code className="font-mono text-text-secondary">/mcp</code> endpoint.
          Use Bearer token, Basic Auth, or both — either grants access.
          Basic Auth credentials are forwarded to OData backends when <code className="font-mono text-text-secondary">passthrough</code> is enabled.
        </p>
        <CredField label="MCP_TOKEN"    hint="Bearer token (Authorization: Bearer …)"     value={form.MCP_TOKEN    || ''} onChange={v => set('MCP_TOKEN', v)}    secret />
        <CredField label="MCP_USERNAME" hint="Basic auth username — forwarded when passthrough is on" value={form.MCP_USERNAME || ''} onChange={v => set('MCP_USERNAME', v)} />
        <CredField label="MCP_PASSWORD" hint="Basic auth password"                          value={form.MCP_PASSWORD || ''} onChange={v => set('MCP_PASSWORD', v)} secret />
      </section>

      <button onClick={handleSave} disabled={saving} className="btn-gold text-sm">
        {saving ? 'Saving…' : 'Save Credentials'}
      </button>
    </div>
  )
}

function CredField({ label, hint, value, onChange, secret }: {
  label: string; hint?: string; value: string; onChange: (v: string) => void; secret?: boolean
}) {
  const [show, setShow] = useState(false)
  return (
    <div className="mb-4">
      <label className="block text-xs font-medium text-text-muted mb-1.5">{label}</label>
      <div className="relative flex">
        <input
          type={secret && !show ? 'password' : 'text'}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={label}
          className="input w-full font-mono text-sm"
        />
        {secret && (
          <button
            type="button"
            onClick={() => setShow(s => !s)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-text-muted hover:text-text-secondary"
          >
            {show ? 'hide' : 'show'}
          </button>
        )}
      </div>
      {hint && <p className="text-xs text-text-muted mt-1">{hint}</p>}
    </div>
  )
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-3 pb-3 border-b border-border">
      {children}
    </p>
  )
}
