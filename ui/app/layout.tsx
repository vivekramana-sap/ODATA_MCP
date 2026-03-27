import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'JAM OData MCP Bridge',
  description: 'Configurator for the JAM OData MCP Bridge',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-surface-0 text-text-primary">
        {children}
      </body>
    </html>
  )
}
