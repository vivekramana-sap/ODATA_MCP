'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { BridgeStatus, Credentials, MCPTool, ODataService } from '@/lib/types'
import { getBridgeStatus, getCredentials, getServices, getTools } from '@/lib/api'

import Header from '@/components/Header'
import TabBar, { type Tab } from '@/components/TabBar'
import { ToastContainer } from '@/components/Toast'
import ServicesTab    from '@/components/tabs/ServicesTab'
import CredentialsTab from '@/components/tabs/CredentialsTab'
import ToolsTab       from '@/components/tabs/ToolsTab'
import DeployTab      from '@/components/tabs/DeployTab'

const MCP_PORT = 7777

export default function Home() {
  const [tab,         setTab]         = useState<Tab>('services')
  const [services,    setServices]    = useState<ODataService[]>([])
  const [credentials, setCredentials] = useState<Credentials>({})
  const [tools,       setTools]       = useState<MCPTool[]>([])
  const [bridge,      setBridge]      = useState<BridgeStatus>({ running: false })

  const refreshServices     = useCallback(() => getServices().then(setServices).catch(() => {}), [])
  const refreshCredentials  = useCallback(() => getCredentials().then(setCredentials).catch(() => {}), [])
  const refreshTools        = useCallback(() => getTools().then(t => setTools(Array.isArray(t) ? t : [])).catch(() => setTools([])), [])
  const refreshBridgeStatus = useCallback(() => getBridgeStatus().then(setBridge).catch(() => {}), [])

  useEffect(() => {
    refreshServices()
    refreshCredentials()
    refreshBridgeStatus()
  }, [refreshServices, refreshCredentials, refreshBridgeStatus])

  useEffect(() => {
    if (bridge.running) refreshTools()
  }, [bridge.running, refreshTools])

  // Poll bridge status every 10 s
  useEffect(() => {
    const t = setInterval(refreshBridgeStatus, 10_000)
    return () => clearInterval(t)
  }, [refreshBridgeStatus])

  // Deploy snapshot — persisted in localStorage so ServicesTab can show new/modified/deployed badges
  const [deployedSnapshot, setDeployedSnapshot] = useState<ODataService[]>(() => {
    try {
      const s = typeof window !== 'undefined' ? localStorage.getItem('odata_deployed_snapshot') : null
      return s ? (JSON.parse(s) as ODataService[]) : []
    } catch { return [] }
  })
  // Keep a ref so handleDeploySuccess always closes over the latest services without re-creating
  const servicesRef = useRef<ODataService[]>(services)
  useEffect(() => { servicesRef.current = services }, [services])
  const handleDeploySuccess = useCallback(() => {
    const snap = servicesRef.current
    setDeployedSnapshot(snap)
    try { localStorage.setItem('odata_deployed_snapshot', JSON.stringify(snap)) } catch { /* ignore */ }
  }, [])

  // Ensure tools is always an array (defensive against API returning non-array)
  const safeTools = Array.isArray(tools) ? tools : []

  return (
    <div className="flex flex-col min-h-screen">
      <Header
        bridge={bridge}
        mcpPort={MCP_PORT}
        onBridgeChange={refreshBridgeStatus}
      />

      <TabBar
        active={tab}
        toolCount={safeTools.length}
        onChange={setTab}
      />

      <main className="flex-1 px-6 py-6 max-w-5xl w-full mx-auto">
        {tab === 'services'    && (
          <ServicesTab
            services={services}
            bridge={bridge}
            tools={safeTools}
            onSave={refreshServices}
            deployedSnapshot={deployedSnapshot}
          />
        )}
        {tab === 'credentials' && (
          <CredentialsTab
            credentials={credentials}
            onSave={refreshCredentials}
          />
        )}
        {tab === 'tools'       && (
          <ToolsTab
            tools={safeTools}
            bridge={bridge}
            services={services}
            mcpPort={MCP_PORT}
            onBridgeChange={refreshBridgeStatus}
            onToolsRefresh={refreshTools}
          />
        )}
        {/* DeployTab stays mounted across tab switches so streaming logs and deploy state are preserved */}
        <div className={tab !== 'deploy' ? 'hidden' : ''}>
          <DeployTab onDeploySuccess={handleDeploySuccess} />
        </div>
      </main>

      <ToastContainer />
    </div>
  )
}
