// A local receiver for the intake webhook that runs the REAL verification code of clio-matter-bridge, unmodified.
//
//   CLIO_BRIDGE_DIR=../clio-matter-bridge BRIDGE_SECRET=lab-bridge-secret PORT=9301 node tools/bridge_receiver.mjs
//
// The bridge's own `handleEvent` (src/lib/webhook.mjs) checks the x-hook-signature HMAC over the raw bytes, finds the
// event under meta.event, and dedupes on event + data.id + data.etag. Nothing is copied into that repository and
// nothing is written to it: this file only imports from it. The HTTP status mapping below is this lab's choice, the
// bridge's own server answers 200 to everything.
import { createServer } from 'node:http';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { appendFileSync } from 'node:fs';

const dir = resolve(process.env.CLIO_BRIDGE_DIR || '../clio-matter-bridge');
const { handleEvent, MemoryIdempotencyStore } = await import(pathToFileURL(resolve(dir, 'src/lib/webhook.mjs')).href);
const secret = process.env.BRIDGE_SECRET;
if (!secret) throw new Error('BRIDGE_SECRET is required');
const port = Number(process.env.PORT || 9301);
const logFile = process.env.RECEIVER_LOG || '';
const store = new MemoryIdempotencyStore({ maxEntries: 5000 });
const accepted = [];

const server = createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', async () => {
    if (req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ up: true, accepted: accepted.length }));
      return;
    }
    const rawBody = Buffer.concat(chunks).toString('utf8');
    let result;
    try {
      result = await handleEvent({
        rawBody,
        headers: req.headers,
        secret,
        store,
        handlers: {
          '*': async (payload) => {
            accepted.push(payload.data.id);
            return { stored_intake: payload.data.id, tenant: payload.meta.tenant_id, practice_area: payload.data.matter.practice_area };
          },
        },
      });
    } catch (err) {
      result = { outcome: 'error', reason: String(err.message) };
    }
    const status = result.outcome === 'rejected' ? 401 : result.outcome === 'error' ? 500 : 200;
    const line = JSON.stringify({ at: new Date().toISOString(), status, result, idempotency_key_header: req.headers['idempotency-key'] });
    console.log(line);
    if (logFile) appendFileSync(logFile, line + '\n');
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(result));
  });
});
server.listen(port, '127.0.0.1', () => console.log(`bridge receiver on 127.0.0.1:${port}, verification code from ${dir}`));
