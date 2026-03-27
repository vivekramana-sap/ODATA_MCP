import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        gold: {
          DEFAULT: '#eeb717',
          hover:   '#d4a415',
          muted:   '#eeb71720',
          border:  '#eeb71740',
        },
        surface: {
          0: '#0a0a0a',
          1: '#111111',
          2: '#1a1a1a',
          3: '#222222',
          4: '#2a2a2a',
        },
        border: {
          DEFAULT: '#2a2a2a',
          subtle:  '#1f1f1f',
        },
        text: {
          primary:   '#e5e5e5',
          secondary: '#a3a3a3',
          muted:     '#6b6b6b',
        },
        status: {
          green:  '#22c55e',
          red:    '#ef4444',
          orange: '#f97316',
        },
      },
      fontFamily: {
        mono: ['var(--font-mono)', 'SFMono-Regular', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}

export default config
