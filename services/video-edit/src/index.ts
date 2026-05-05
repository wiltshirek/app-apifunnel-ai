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
import { videoRouter } from './routes/video';
import { videoEditRouter } from './routes/video-edit';

const app = new Hono();

app.use('*', logger());
app.use('*', cors({
  origin: ['https://app.apifunnel.ai', 'http://localhost:3000'],
  allowHeaders: ['Authorization', 'Content-Type', 'X-Admin-Key'],
  allowMethods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
}));

app.get('/health', (c) => c.json({ status: 'ok', service: 'video-edit', ts: new Date().toISOString() }));

app.get('/v1/openapi.yaml', (c) => {
  try {
    const specPath = join(__dirname, '..', 'openapi', 'video-edit.yaml');
    c.header('Content-Type', 'application/yaml');
    return c.body(readFileSync(specPath, 'utf8'));
  } catch {
    return c.json({ error: 'OpenAPI spec not found' }, 404);
  }
});

app.route('/v1/video', videoRouter);
app.route('/v1/video', videoEditRouter);

app.notFound((c) => c.json({ error: 'Not found' }, 404));
app.onError((err, c) => {
  console.error('[Hono] Unhandled error:', err);
  return c.json({ error: 'Internal server error' }, 500);
});

async function main() {
  await connectDB();

  const PORT = parseInt(process.env.VIDEO_EDIT_PORT || process.env.PORT || '3005', 10);

  serve({ fetch: app.fetch, port: PORT }, (info) => {
    console.log(`video-edit-api listening on http://localhost:${info.port}`);
    console.log(`  OpenAPI spec: http://localhost:${info.port}/v1/openapi.yaml`);
    console.log(`  Health:       http://localhost:${info.port}/health`);
  });
}

main().catch((err) => {
  console.error('Failed to start:', err);
  process.exit(1);
});
