'use client'

import { useEffect, useRef, useState } from 'react'
import { showToast } from '@/components/Toast'

interface Checklist {
  cli?:       { ok: boolean; detail?: string }
  logged_in?: { ok: boolean; detail?: string }
  creds?:     { ok: boolean; detail?: string }
  services?:  { ok: boolean; detail?: string }
  app?:       { ok: boolean; state?: string; routes?: string; deployed?: boolean }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options)
  return res.json() as Promise<T>
}
const post = (p: string, b: unknown) => api<Record<string, unknown>>(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b),
})

export default function DeployTab() {
  const [loginForm, setLoginForm] = useState({ api: 'https://api.cf.eu10.hana.ondemand.com', username: '', password: '', org: '', space: '' })
  const [loginLoading, setLoginLoading] = useState(false)
  const [loginOut, setLoginOut] = useState<string>('')

  const [checklist,    setChecklist]    = useState<Checklist | null>(null)
  const [checkLoading, setCheckLoading] = useState(false)

  const [deploying, setDeploying] = useState(false)
  const [logs,      setLogs]      = useState<string[]>([])
  const [exitCode,  setExitCode]  = useState<number | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  const setLF = (k: keyof typeof loginForm, v: string) => setLoginForm(f => ({ ...f, [k]: v }))

  const fetchChecklist = () => {
    setCheckLoading(true)
    api<Checklist>('/api/cf/checklist').then(d => { setChecklist(d); setCheckLoading(false) })
  }
  useEffect(() => { fetchChecklist() }, [])
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [logs])

  const handleLogin = async () => {
    if (!loginForm.api || !loginForm.username || !loginForm.password) {
      showToast('API endpoint, username and password are required', 'error'); return
    }
    setLoginLoading(true)
    setLoginOut('Connecting to CF — this may take 20–60 seconds…')
    const res = await post('/api/cf/login', loginForm) as { ok?: boolean; output?: string; returncode?: number }
    const out = (res.output || '(no output)') + (res.returncode !== undefined ? `\n[exit: ${res.returncode}]` : '')
    setLoginOut(out)
    if (res.ok) { showToast('Logged in to CF'); fetchChecklist() }
    else showToast('CF login failed — see output below', 'error')
    setLoginLoading(false)
  }

  const handleLogout = async () => {
    const res = await post('/api/cf/logout', {}) as { ok?: boolean; output?: string }
    showToast(res.ok ? 'Logged out from CF' : (res.output || 'Logout failed'), res.ok ? 'success' : 'error')
    fetchChecklist()
  }

  const handleDeploy = () => {
    if (!checklist?.logged_in?.ok) { showToast('Log in to CF first', 'error'); return }
    setLogs([]); setExitCode(null); setDeploying(true)
    const es = new EventSource('/api/deploy')
    es.onmessage = e => {
      const d: { line?: string; exit?: number } = JSON.parse(e.data)
      if (d.line !== undefined) setLogs(l => [...l, d.line!])
      if (d.exit !== undefined) {
        setExitCode(d.exit)
        setDeploying(false)
        es.close()
        fetchChecklist()
        if (d.exit === 0) showToast('Deployment successful!')
        else showToast('Deployment failed — check logs', 'error')
      }
    }
    es.onerror = () => {
      setLogs(l => [...l, 'ERROR: deploy stream lost'])
      setDeploying(false); setExitCode(1); es.close()
    }
  }

  const loggedIn  = checklist?.logged_in?.ok
  const targetOut = checklist?.logged_in?.detail || ''
  const cfOrg     = targetOut.match(/org:\s+(.+)/)?.[1]?.trim()   || ''
  const cfSpace   = targetOut.match(/space:\s+(.+)/)?.[1]?.trim() || ''
  const cfApi     = targetOut.match(/api endpoint:\s+(.+)/i)?.[1]?.trim() || ''
  const cfUser    = targetOut.match(/user:\s+(.+)/)?.[1]?.trim()  || ''

  const checks = checklist ? [
    { label: 'CF CLI installed',              ok: !!checklist.cli?.ok,        detail: checklist.cli?.detail },
    { label: 'Logged in to Cloud Foundry',    ok: !!checklist.logged_in?.ok,  detail: loggedIn ? `${cfUser} · ${cfOrg}/${cfSpace}` : 'Not logged in' },
    { label: 'credentials.mtaext saved',      ok: !!checklist.creds?.ok,      detail: checklist.creds?.detail },
    { label: 'Services configured',           ok: !!checklist.services?.ok,   detail: checklist.services?.detail },
    { label: 'App running in BTP',            ok: !!checklist.app?.ok,
      detail: checklist.app?.routes ? `https://${checklist.app.routes} (${checklist.app?.state})`
              : checklist.app?.deployed === false ? 'Not yet deployed' : checklist.app?.state || '' },
  ] : []

  return (
    <div className="max-w-2xl space-y-5">
      {/* CF Login */}
      <section className="card border-t-2 border-t-gold">
        <div className="flex items-center mb-4">
          <h2 className="text-sm font-semibold flex-1">Cloud Foundry Login</h2>
          {loggedIn && (
            <button onClick={handleLogout} className="text-xs px-2 py-1 rounded border border-status-red/40 text-status-red hover:bg-status-red/10 transition-colors">
              Logout
            </button>
          )}
        </div>

        {loggedIn ? (
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="w-2 h-2 rounded-full bg-status-green shadow-[0_0_4px_#22c55e]" />
              <span className="text-sm font-semibold text-status-green">Logged in</span>
              <button onClick={fetchChecklist} disabled={checkLoading} className="ml-auto btn-ghost text-xs px-2 py-1">
                {checkLoading ? '…' : '↻ Refresh'}
              </button>
            </div>
            <div className="flex flex-wrap gap-4 text-xs text-text-secondary">
              {cfUser  && <span>{cfUser}</span>}
              {cfApi   && <span>{cfApi}</span>}
              {cfOrg   && <span className="font-medium">{cfOrg}</span>}
              {cfSpace && <span>{cfSpace}</span>}
            </div>
            {(!cfOrg || !cfSpace) && (
              <p className="mt-2 text-xs border-l-2 border-gold bg-gold/5 px-3 py-2 rounded-r text-text-secondary">
                No org/space targeted. Re-login with Org and Space, or run{' '}
                <code className="font-mono">cf target -o ORG -s SPACE</code> then Refresh.
              </p>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="col-span-2">
                <Label>CF API Endpoint</Label>
                <input className="input w-full text-sm" value={loginForm.api} onChange={e => setLF('api', e.target.value)} />
              </div>
              <div>
                <Label>Username</Label>
                <input className="input w-full text-sm" value={loginForm.username} onChange={e => setLF('username', e.target.value)} />
              </div>
              <div>
                <Label>Password</Label>
                <input className="input w-full text-sm" type="password" value={loginForm.password} onChange={e => setLF('password', e.target.value)} />
              </div>
              <div>
                <Label>Org <span className="text-text-muted font-normal">(optional)</span></Label>
                <input className="input w-full text-sm" value={loginForm.org} onChange={e => setLF('org', e.target.value)} />
              </div>
              <div>
                <Label>Space <span className="text-text-muted font-normal">(optional)</span></Label>
                <input className="input w-full text-sm" value={loginForm.space} onChange={e => setLF('space', e.target.value)} />
              </div>
            </div>
            <button onClick={handleLogin} disabled={loginLoading} className="btn-gold text-sm">
              {loginLoading ? 'Connecting…' : 'Login to CF'}
            </button>
            {loginOut && <div className="code-panel text-xs mt-2 max-h-32">{loginOut}</div>}
          </div>
        )}
      </section>

      {/* Pre-deploy checklist */}
      <section className="card">
        <h2 className="text-sm font-semibold mb-4">Pre-Deploy Checklist</h2>
        {checklist === null ? (
          <p className="text-xs text-text-muted">Loading…</p>
        ) : (
          <div className="space-y-2.5">
            {checks.map((c, i) => (
              <div key={i} className="flex items-start gap-3">
                <span className={`mt-0.5 text-xs font-bold w-4 shrink-0 ${c.ok ? 'text-status-green' : 'text-status-red'}`}>
                  {c.ok ? '✓' : '✕'}
                </span>
                <div>
                  <p className={`text-sm ${c.ok ? 'text-text-primary' : 'text-text-muted'}`}>{c.label}</p>
                  {c.detail && <p className="text-xs text-text-muted mt-0.5">{c.detail}</p>}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Deploy */}
      <section className="card">
        <h2 className="text-sm font-semibold mb-2">Deploy to BTP</h2>
        <p className="text-xs text-text-muted mb-4">
          Runs <code className="font-mono text-text-secondary">deploy.sh</code> — builds and pushes the MTA application to Cloud Foundry.
          All prerequisites must be green above.
        </p>
        <button
          onClick={handleDeploy}
          disabled={deploying || !loggedIn}
          className="btn-gold text-sm"
        >
          {deploying ? 'Deploying…' : '🚀 Deploy'}
        </button>
        {exitCode !== null && (
          <span className={`ml-3 text-xs font-medium ${exitCode === 0 ? 'text-status-green' : 'text-status-red'}`}>
            {exitCode === 0 ? 'Success' : `Exit code ${exitCode}`}
          </span>
        )}

        {(deploying || logs.length > 0) && (
          <div className="log-panel mt-4 max-h-72" ref={logRef}>
            {logs.map((l, i) => (
              <div key={i} className="log-line">{l}</div>
            ))}
            {deploying && <div className="log-line text-gold animate-pulse">▌</div>}
          </div>
        )}
      </section>
    </div>
  )
}

function Label({ children }: { children: React.ReactNode }) {
  return <label className="block text-xs font-medium text-text-muted mb-1.5">{children}</label>
}
