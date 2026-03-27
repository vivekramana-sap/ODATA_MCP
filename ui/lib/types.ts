// ── Service ────────────────────────────────────────────────────────────────
export interface ODataService {
  alias: string
  url: string
  username?: string
  password?: string
  passthrough?: boolean
  readonly?: boolean
  include?: string[]
  include_actions?: string[]
  default_top?: number
  max_top?: number
  enable_ops?: string
  disable_ops?: string
  cookie_file?: string
  cookie_string?: string
  group?: string
}

// ── Credentials ────────────────────────────────────────────────────────────
export interface Credentials {
  MCP_TOKEN?: string
  MCP_USERNAME?: string
  MCP_PASSWORD?: string
  [key: string]: string | undefined
}

// ── Tool ───────────────────────────────────────────────────────────────────
export interface ToolInputSchema {
  type: string
  properties: Record<string, ToolProp>
  required?: string[]
}

export interface ToolProp {
  type: string
  description?: string
}

export interface MCPTool {
  name: string
  description?: string
  inputSchema: ToolInputSchema
}

// ── Bridge ─────────────────────────────────────────────────────────────────
export interface BridgeStatus {
  running: boolean
  pid?: number
}

export interface BridgeEndpoints {
  endpoints: Record<string, string>  // group name (or "default") → URL
}

// ── Probe ──────────────────────────────────────────────────────────────────
export interface EntitySet {
  name: string
  keys?: string[]
  prop_count?: number
}

export interface ProbeResult {
  success: boolean
  error?: string
  hint?: string
  entity_sets: EntitySet[]
  actions?: string[]
}

// ── CF App ─────────────────────────────────────────────────────────────────
export interface CfAppStatus {
  ok: boolean
  deployed?: boolean
  state?: string
  routes?: string
  instances?: string
  memory?: string
  output?: string
  error?: string
}

// ── BTP Health ─────────────────────────────────────────────────────────────
export interface BtpHealth {
  ok: boolean
  tools?: number
  services?: string[]
  error?: string
}

// ── Deploy ─────────────────────────────────────────────────────────────────
export interface DeployResult {
  ok: boolean
  output?: string
  error?: string
}

// ── Tool call ──────────────────────────────────────────────────────────────
export interface ToolCallResult {
  result?: {
    content?: Array<{ type: string; text: string }>
  }
  error?: string
  [key: string]: unknown
}
