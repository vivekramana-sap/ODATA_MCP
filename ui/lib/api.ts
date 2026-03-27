import type {
  ODataService,
  Credentials,
  MCPTool,
  BridgeStatus,
  BridgeEndpoints,
  ProbeResult,
  CfAppStatus,
  BtpHealth,
  DeployResult,
  ToolCallResult,
} from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<T>
}

function json(method: string, body: unknown) {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

// ── Services ───────────────────────────────────────────────────────────────
export const getServices  = ()                      => request<ODataService[]>('/api/services')
export const putServices  = (s: ODataService[])     => request<void>('/api/services', json('PUT', s))

// ── Credentials ────────────────────────────────────────────────────────────
export const getCredentials  = ()               => request<Credentials>('/api/credentials')
export const putCredentials  = (c: Credentials) => request<void>('/api/credentials', json('PUT', c))

// ── Tools ──────────────────────────────────────────────────────────────────
export const getTools    = ()                                    => request<MCPTool[]>('/api/tools')
export const callTool    = (name: string, args: Record<string, unknown>, auth: string) =>
  request<ToolCallResult>('/api/tools/call', json('POST', { name, arguments: args, auth }))

// ── Bridge ─────────────────────────────────────────────────────────────────
export const getBridgeStatus    = ()  => request<BridgeStatus>('/api/bridge/status')
export const getBridgeEndpoints = ()  => request<BridgeEndpoints>('/api/bridge/endpoints')
export const startBridge        = ()  => request<BridgeStatus & { ok: boolean; pid?: number; error?: string }>('/api/bridge/start', json('POST', {}))
export const stopBridge         = ()  => request<void>('/api/bridge/stop', json('POST', {}))
export const getBridgeLogs      = ()  => request<{ logs: string[] }>('/api/bridge/logs')

// ── Probe ──────────────────────────────────────────────────────────────────
export const probeService = (svc: Partial<ODataService>) =>
  request<ProbeResult>('/api/probe', json('POST', svc))

// ── CF / BTP ───────────────────────────────────────────────────────────────
export const getCfApp    = () => request<CfAppStatus>('/api/cf/app')
export const getBtpHealth = () => request<BtpHealth>('/api/btp/health')

// ── Deploy ─────────────────────────────────────────────────────────────────
export const deploy = () => request<DeployResult>('/api/deploy', json('POST', {}))
