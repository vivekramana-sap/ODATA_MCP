export const dynamic = 'force-dynamic'

export async function GET() {
  const backend = await fetch('http://localhost:7770/api/deploy', {
    headers: { Accept: 'text/event-stream' },
  })

  return new Response(backend.body, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'X-Accel-Buffering': 'no',
    },
  })
}
