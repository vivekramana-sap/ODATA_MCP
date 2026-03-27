'use client'

export type Tab = 'services' | 'credentials' | 'tools' | 'deploy'

const TABS: { id: Tab; label: string }[] = [
  { id: 'services',    label: 'Services'     },
  { id: 'credentials', label: 'Credentials'  },
  { id: 'tools',       label: 'Tools & Test' },
  { id: 'deploy',      label: 'Deploy'       },
]

interface Props {
  active: Tab
  toolCount?: number
  onChange: (t: Tab) => void
}

export default function TabBar({ active, toolCount, onChange }: Props) {
  return (
    <nav className="flex border-b border-border bg-surface-1 px-6">
      {TABS.map(t => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`px-4 py-3.5 text-sm border-b-2 transition-colors -mb-px
            ${active === t.id
              ? 'border-gold text-gold font-medium'
              : 'border-transparent text-text-muted hover:text-text-secondary hover:border-border'}`}
        >
          {t.label}
          {t.id === 'tools' && toolCount !== undefined && toolCount > 0 && (
            <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-gold/20 text-gold">
              {toolCount}
            </span>
          )}
        </button>
      ))}
    </nav>
  )
}
