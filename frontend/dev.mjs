import { createServer } from 'vite'
import { createServer as createNetServer } from 'node:net'

// Vite 6 treats port 0 as its default port. Obtain an ephemeral port first;
// Vite can retry if another process claims it before the HTTP server binds.
const reservation = createNetServer()
await new Promise((resolve, reject) => {
  reservation.once('error', reject)
  reservation.listen(0, '127.0.0.1', resolve)
})
const port = reservation.address().port
await new Promise((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()))

const server = await createServer({
  server: { host: '127.0.0.1', port, strictPort: false },
})
await server.listen()
console.log(`DATACART_READY ${JSON.stringify({ url: server.resolvedUrls.local[0].replace(/\/$/, '') })}`)

async function stop() {
  await server.close()
  process.exit(0)
}
process.on('SIGINT', stop)
process.on('SIGTERM', stop)
