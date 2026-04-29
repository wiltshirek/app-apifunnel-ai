import { config } from 'dotenv';
import { resolve } from 'path';
config({ path: resolve(__dirname, '..', '..', '..', '.env') });
import { serve } from '@hono/node-server';
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { logger } from 'hono/logger';
import { readFileSync } from 'fs';
import { join } from 'path';

import { connectDB } from './lib/db';
import { subagentsRouter } from './routes/subagents';
import { notificationsRouter } from './routes/notifications';
import { videoRouter } from './routes/video';

const app = new Hono();

// ── Middleware ──────────────────────────────────────────────────────────────
app.use('*', logger());
app.use('*', cors({
  origin: ['https://app.apifunnel.ai', 'http://localhost:3000'],
  allowHeaders: ['Authorization', 'Content-Type', 'X-Admin-Key'],
  allowMethods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
}));

// ── Health ──────────────────────────────────────────────────────────────────
app.get('/health', (c) => c.json({ status: 'ok', ts: new Date().toISOString() }));

// ── OpenAPI spec ────────────────────────────────────────────────────────────
app.get('/v1/openapi.yaml', (c) => {
  try {
    const specPath = join(__dirname, '..', 'openapi', 'subagents.yaml');
    c.header('Content-Type', 'application/yaml');
    return c.body(readFileSync(specPath, 'utf8'));
  } catch {
    return c.json({ error: 'OpenAPI spec not found' }, 404);
  }
});

// ── Routes ──────────────────────────────────────────────────────────────────
app.route('/v1/subagents', subagentsRouter);
app.route('/v1/notifications', notificationsRouter);
app.route('/v1/video', videoRouter);

// ── 404 fallthrough ─────────────────────────────────────────────────────────
app.notFound((c) => c.json({ error: 'Not found' }, 404));
app.onError((err, c) => {
  console.error('[Hono] Unhandled error:', err);
  return c.json({ error: 'Internal server error' }, 500);
});

// ── Startup ─────────────────────────────────────────────────────────────────
async function main() {
  await connectDB();

  const PORT = parseInt(process.env.PORT || '3001', 10);

  serve({ fetch: app.fetch, port: PORT }, (info) => {
    console.log(`🚀 api-apifunnel-ai listening on http://localhost:${info.port}`);
    console.log(`   OpenAPI spec: http://localhost:${info.port}/v1/openapi.yaml`);
    console.log(`   Health:       http://localhost:${info.port}/health`);
  });
}

main().catch((err) => {
  console.error('Failed to start:', err);
  process.exit(1);
});
