import { Hono } from 'hono';
import { authenticateInternalRequest } from '../lib/auth-internal';
import { extractTranscript } from '../lib/video';
import { LlmError } from '../lib/llm-call';

export const videoRouter = new Hono();

/**
 * GET /v1/video/transcript?url=<youtube_url>
 *
 * Pure infrastructure — extracts timestamped transcript from a video URL.
 * Tries embedded captions first, falls back to Whisper transcription.
 * No LLM involved, no reasoning, no inference.
 */
videoRouter.get('/transcript', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const url = c.req.query('url');
  if (!url) {
    return c.json({ error: 'missing_url', message: 'Query parameter "url" is required.' }, 400);
  }

  try {
    const result = await extractTranscript(url);
    return c.json(result);
  } catch (err: any) {
    if (err instanceof LlmError) {
      return c.json({ error: err.code, message: err.message }, err.status as any);
    }
    console.error('[video/transcript] unexpected error:', err);
    return c.json({ error: 'internal_error', message: err?.message || 'Unknown error' }, 500);
  }
});
