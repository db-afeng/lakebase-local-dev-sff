import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

// One database per supervised development session; never persist it to disk.
const db = await PGlite.create('memory://')
const server = new PGLiteSocketServer({
  db,
  host: '127.0.0.1',
  port: Number(process.env.PGPORT),
  // One pooled app connection, plus a maintenance/fixture client. The server
  // serializes these over PGlite's single underlying database session.
  maxConnections: 2,
})
await server.start()
console.log(`DATACART_READY ${JSON.stringify({ port: Number(server.getServerConn().split(':').pop()) })}`)

let stopping = false
async function stop() {
  if (stopping) return
  stopping = true
  await server.stop()
  await db.close()
  process.exit(0)
}
process.on('SIGINT', stop)
process.on('SIGTERM', stop)
