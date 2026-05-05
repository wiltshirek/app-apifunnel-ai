/**
 * Video Editing API routes — Phase 1
 *
 * POST /v1/video/assets/prepare   — Prepare a user asset for timeline use
 * GET  /v1/video/assets/:id       — Fetch asset metadata
 * DELETE /v1/video/assets/:id     — Delete a prepared asset
 * POST /v1/video/renders/compose  — Compose inserts into a source video
 * GET  /v1/video/renders/:id      — Fetch render metadata + output URL
 * GET  /v1/video/renders/:id/download — Redirect to render download
 * POST /v1/video/renders/:id/preview  — Generate thumbnail strip from render
 * GET  /v1/video/jobs/:id         — Poll async job status
 * POST /v1/video/jobs/:id/cancel  — Cancel a queued/running job
 */

import { Hono } from 'hono';
import { authenticateInternalRequest } from '../lib/auth-internal';
import { LlmError, oneShotCompletion } from '../lib/llm-call';
import { VideoJob } from '../models/VideoJob';
import { VideoAsset } from '../models/VideoAsset';
import { runPrepareJob } from '../lib/video-edit/asset-prepare';
import { runComposeJob } from '../lib/video-edit/compose';
import { getPresignedUrl, deleteObject, videoS3Key } from '../lib/video-edit/s3';
import type { PrepareTarget, ComposeRequest, OutputSpec } from '../lib/video-edit/types';

export const videoEditRouter = new Hono();

const MAX_QUEUED_JOBS_PER_USER = 10;

function genId(prefix: string): string {
  return `${prefix}_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;
}

function ttlDate(hours = 72): Date {
  return new Date(Date.now() + hours * 60 * 60 * 1000);
}

// ── Assets ───────────────────────────────────────────────────────────────────

videoEditRouter.post('/assets/prepare', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const body = await c.req.json();
  if (!body.source?.uri) {
    return c.json({ error: 'missing_source', message: 'source.uri is required' }, 400);
  }

  const queuedCount = await VideoJob.countDocuments({
    user_id: auth.sub,
    status: { $in: ['queued', 'running'] },
  });
  if (queuedCount >= MAX_QUEUED_JOBS_PER_USER) {
    return c.json({ error: 'too_many_jobs', message: `You have ${queuedCount} active jobs. Wait for some to complete.` }, 429);
  }

  const assetId = genId('vid_asset');
  const jobId = genId('prepare_job');
  const target: PrepareTarget = body.target || {};

  await VideoAsset.create({
    asset_id: assetId,
    user_id: auth.sub,
    type: 'unknown',
    source_kind: body.source.kind || 'url',
    source_uri: body.source.uri,
    mime_type: body.source.mime_type,
    prepared_outputs: [],
    ttl_expires_at: ttlDate(),
  });

  await VideoJob.create({
    job_id: jobId,
    user_id: auth.sub,
    type: 'prepare',
    status: 'queued',
    ttl_expires_at: ttlDate(),
  });

  runPrepareJob({
    jobId,
    assetId,
    userId: auth.sub,
    sourceKind: body.source.kind || 'url',
    sourceUri: body.source.uri,
    target,
  }).catch(err => console.error(`[video-edit] prepare job fire-and-forget error:`, err));

  return c.json({
    asset_id: assetId,
    job_id: jobId,
    status: 'queued',
  });
});

videoEditRouter.get('/assets/:id', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const asset = await VideoAsset.findOne({ asset_id: id, user_id: auth.sub }).lean();
  if (!asset) return c.json({ error: 'Asset not found' }, 404);

  const a = asset as any;

  const outputs = [];
  for (const out of a.prepared_outputs || []) {
    let url: string | undefined;
    if (out.uri?.startsWith('s3://')) {
      const s3Key = out.uri.replace(/^s3:\/\/[^/]+\//, '');
      url = await getPresignedUrl(s3Key).catch(() => undefined);
    }
    outputs.push({ ...out, download_url: url });
  }

  return c.json({
    asset_id: a.asset_id,
    type: a.type,
    source: { kind: a.source_kind, uri: a.source_uri },
    mime_type: a.mime_type,
    width: a.width,
    height: a.height,
    page_count: a.page_count,
    duration_seconds: a.duration_seconds,
    prepared_outputs: outputs,
    created_at: a.created_at,
  });
});

videoEditRouter.delete('/assets/:id', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const asset = await VideoAsset.findOne({ asset_id: id, user_id: auth.sub }).lean();
  if (!asset) return c.json({ error: 'Asset not found' }, 404);

  const a = asset as any;
  for (const out of a.prepared_outputs || []) {
    if (out.uri?.startsWith('s3://')) {
      const s3Key = out.uri.replace(/^s3:\/\/[^/]+\//, '');
      await deleteObject(s3Key).catch(() => {});
    }
  }

  await VideoAsset.deleteOne({ asset_id: id, user_id: auth.sub });
  return c.json({ deleted: true, asset_id: id });
});

// ── Renders ──────────────────────────────────────────────────────────────────

videoEditRouter.post('/renders/compose', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const body = await c.req.json();
  if (!body.source_video) {
    return c.json({ error: 'missing_source_video', message: 'source_video is required' }, 400);
  }
  if (!Array.isArray(body.inserts) || body.inserts.length === 0) {
    return c.json({ error: 'missing_inserts', message: 'At least one insert is required' }, 400);
  }

  const queuedCount = await VideoJob.countDocuments({
    user_id: auth.sub,
    status: { $in: ['queued', 'running'] },
  });
  if (queuedCount >= MAX_QUEUED_JOBS_PER_USER) {
    return c.json({ error: 'too_many_jobs', message: `You have ${queuedCount} active jobs.` }, 429);
  }

  const jobId = genId('compose_job');
  const request: ComposeRequest = {
    source_video: body.source_video,
    inserts: body.inserts,
    output: body.output || {},
  };

  await VideoJob.create({
    job_id: jobId,
    user_id: auth.sub,
    type: 'compose',
    status: 'queued',
    ttl_expires_at: ttlDate(),
  });

  runComposeJob({
    jobId,
    userId: auth.sub,
    request,
  }).catch(err => console.error(`[video-edit] compose job fire-and-forget error:`, err));

  return c.json({ job_id: jobId, status: 'queued' });
});

videoEditRouter.get('/renders/:id', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const job = await VideoJob.findOne({
    job_id: id,
    user_id: auth.sub,
    type: { $in: ['compose', 'render'] },
  }).lean();

  if (!job) return c.json({ error: 'Render not found' }, 404);

  const j = job as any;
  let outputUrl = j.result?.output_url;

  if (j.status === 'completed' && j.result?.s3_key) {
    outputUrl = await getPresignedUrl(j.result.s3_key).catch(() => outputUrl);
  }

  return c.json({
    job_id: j.job_id,
    status: j.status,
    progress: j.progress,
    output_url: outputUrl,
    duration_seconds: j.result?.duration_seconds,
    width: j.result?.width,
    height: j.result?.height,
    file_size_bytes: j.result?.file_size_bytes,
    error: j.error,
    created_at: j.created_at,
    completed_at: j.completed_at,
  });
});

videoEditRouter.get('/renders/:id/download', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const job = await VideoJob.findOne({
    job_id: id,
    user_id: auth.sub,
    type: { $in: ['compose', 'render'] },
    status: 'completed',
  }).lean();

  if (!job) return c.json({ error: 'Render not found or not completed' }, 404);

  const j = job as any;
  if (!j.result?.s3_key) {
    return c.json({ error: 'No output file available' }, 404);
  }

  const url = await getPresignedUrl(j.result.s3_key, 3600);
  return c.redirect(url, 302);
});

videoEditRouter.post('/renders/:id/preview', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const body = await c.req.json();
  const frameCount = Math.min(body.frame_count || 12, 30);
  const thumbWidth = body.width || 320;

  const job = await VideoJob.findOne({
    job_id: id,
    user_id: auth.sub,
    type: { $in: ['compose', 'render'] },
    status: 'completed',
  }).lean();

  if (!job) return c.json({ error: 'Render not found or not completed' }, 404);

  const j = job as any;
  if (!j.result?.s3_key) {
    return c.json({ error: 'No output file available' }, 404);
  }

  // Download rendered video, extract frames, upload thumbnails
  const { makeTmpDir, downloadUrl, runFfmpeg } = await import('../lib/video-edit/ffmpeg');
  const { uploadFile: uploadToS3 } = await import('../lib/video-edit/s3');
  const { rm, readdir, readFile } = await import('node:fs/promises');
  const { join } = await import('node:path');

  const tmpDir = await makeTmpDir('preview-');
  try {
    const videoUrl = await getPresignedUrl(j.result.s3_key);
    const videoPath = join(tmpDir, 'render.mp4');
    await downloadUrl(videoUrl, videoPath);

    const duration = j.result.duration_seconds || 60;
    const interval = Math.max(1, Math.floor(duration / frameCount));
    const framesDir = join(tmpDir, 'frames');
    const { mkdir } = await import('node:fs/promises');
    await mkdir(framesDir, { recursive: true });

    await runFfmpeg([
      '-i', videoPath,
      '-vf', `fps=1/${interval},scale=${thumbWidth}:-2`,
      '-q:v', '5',
      '-frames:v', String(frameCount),
      join(framesDir, 'frame_%04d.jpg'),
    ]);

    const files = (await readdir(framesDir)).filter(f => f.endsWith('.jpg')).sort();
    const frames: Array<{ timestamp: string; image_url: string }> = [];

    for (const file of files) {
      const idx = parseInt(file.replace('frame_', '').replace('.jpg', ''), 10) - 1;
      const seconds = idx * interval;
      const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
      const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
      const s = String(seconds % 60).padStart(2, '0');

      const s3Key = videoS3Key(auth.sub, 'previews', id, file);
      await uploadToS3(s3Key, join(framesDir, file), 'image/jpeg');
      const url = await getPresignedUrl(s3Key);
      frames.push({ timestamp: `${h}:${m}:${s}`, image_url: url });
    }

    return c.json({ render_id: id, frames });
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
});

/**
 * POST /v1/video/analyze
 *
 * Multimodal analysis of any video URL (YouTube, direct MP4, etc.).
 * Downloads via yt-dlp (YouTube) or direct fetch, extracts frames,
 * sends them + transcript to an LLM with the caller's question.
 */
videoEditRouter.post('/analyze', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const apiSettings = (auth as any).api_settings ?? {};
  if (!apiSettings.openai && !apiSettings.anthropic && !apiSettings.google) {
    return c.json({ error: 'missing_api_key', message: 'No LLM provider API key found in api_settings.' }, 400);
  }

  const body = await c.req.json();
  const url = body.url;
  if (!url || typeof url !== 'string') {
    return c.json({ error: 'missing_url', message: 'A video URL is required.' }, 400);
  }
  const question = body.question || body.prompt;
  if (!question || typeof question !== 'string') {
    return c.json({ error: 'missing_question', message: 'A question or prompt is required.' }, 400);
  }

  const model = body.model || 'gpt-4o-mini';
  const frameCount = Math.min(body.frame_count || 12, 30);

  try {
    const { isVideoUrl } = await import('../lib/video');
    const { makeTmpDir, downloadUrl: downloadDirect, runFfmpeg, probeFile } = await import('../lib/video-edit/ffmpeg');
    const { rm, readdir, readFile, mkdir } = await import('node:fs/promises');
    const { join } = await import('node:path');

    const isPlatformUrl = isVideoUrl(url);
    let frames: Array<{ base64: string; timestamp: string }>;
    let transcript = '';

    if (isPlatformUrl) {
      // YouTube/Vimeo — use yt-dlp pipeline (handles auth, captions, proxy)
      const { preprocessVideo } = await import('../lib/video');
      const result = await preprocessVideo(url);
      frames = result.frames;
      transcript = result.transcript;
    } else {
      // Direct URL — download with fetch, extract frames with ffmpeg
      const tmpDir = await makeTmpDir('analyze-');
      try {
        const videoPath = join(tmpDir, 'source.mp4');
        await downloadDirect(url, videoPath);

        const probe = await probeFile(videoPath);
        const effectiveDuration = probe.duration_seconds || 60;
        const interval = Math.max(1, Math.floor(effectiveDuration / frameCount));
        const framesDir = join(tmpDir, 'frames');
        await mkdir(framesDir, { recursive: true });

        await runFfmpeg([
          '-i', videoPath,
          '-vf', `fps=1/${interval},scale=720:-2`,
          '-q:v', '5',
          '-frames:v', String(frameCount),
          join(framesDir, 'frame_%04d.jpg'),
        ]);

        const files = (await readdir(framesDir)).filter(f => f.endsWith('.jpg')).sort();
        frames = [];
        for (const file of files) {
          const idx = parseInt(file.replace('frame_', '').replace('.jpg', ''), 10) - 1;
          const seconds = idx * interval;
          const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
          const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
          const s = String(seconds % 60).padStart(2, '0');
          const base64 = (await readFile(join(framesDir, file))).toString('base64');
          frames.push({ base64, timestamp: `${h}:${m}:${s}` });
        }
      } finally {
        await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
      }
    }

    const sampledFrames = sampleFrames(frames, frameCount);

    const media: Array<{ url: string; type: string }> = sampledFrames.map(f => ({
      url: `data:image/jpeg;base64,${f.base64}`,
      type: 'image/jpeg',
    }));

    const frameLabels = sampledFrames.map((f, i) => `[Frame ${i + 1}: ${f.timestamp}]`).join(' ');
    const transcriptBlock = transcript ? `\nTranscript:\n${transcript}\n` : '';
    const fullPrompt = `${sampledFrames.length} frames from a video. ${frameLabels}${transcriptBlock}\n${question}`;

    const result = await oneShotCompletion({
      prompt: fullPrompt,
      model,
      media,
      max_tokens: body.max_tokens || 4096,
      api_settings: apiSettings,
    });

    return c.json({
      url,
      answer: result.text,
      frames_analyzed: sampledFrames.length,
      transcript_length: transcript.length,
      model: result.model,
      provider: result.provider,
      tokens: result.tokens,
    });
  } catch (err: any) {
    if (err instanceof LlmError) {
      return c.json({ error: err.code, message: err.message }, err.status as any);
    }
    console.error('[video-edit/analyze] error:', err);
    return c.json({ error: 'analysis_failed', message: err?.message || 'Unknown error' }, 500);
  }
});

function sampleFrames<T>(frames: T[], target: number): T[] {
  if (frames.length <= target) return frames;
  const step = frames.length / target;
  const sampled: T[] = [];
  for (let i = 0; i < target; i++) {
    sampled.push(frames[Math.min(Math.floor(i * step), frames.length - 1)]);
  }
  return sampled;
}

/**
 * POST /v1/video/renders/:id/review
 *
 * Multimodal analysis of a completed render. Extracts frames, sends them
 * to an LLM with the caller's question, returns the answer. One call.
 */
videoEditRouter.post('/renders/:id/review', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const apiSettings = (auth as any).api_settings ?? {};
  if (!apiSettings.openai && !apiSettings.anthropic && !apiSettings.google) {
    return c.json({ error: 'missing_api_key', message: 'No LLM provider API key found in api_settings. At least one of openai, anthropic, or google is required.' }, 400);
  }

  const id = c.req.param('id');
  const body = await c.req.json();
  const question = body.question || body.prompt;
  if (!question || typeof question !== 'string') {
    return c.json({ error: 'missing_question', message: 'A question or prompt is required.' }, 400);
  }
  const model = body.model || 'gpt-4o-mini';
  const frameCount = Math.min(body.frame_count || 10, 20);

  const job = await VideoJob.findOne({
    job_id: id,
    user_id: auth.sub,
    type: { $in: ['compose', 'render'] },
    status: 'completed',
  }).lean();

  if (!job) return c.json({ error: 'Render not found or not completed' }, 404);
  const j = job as any;
  if (!j.result?.s3_key) return c.json({ error: 'No output file available' }, 404);

  const { makeTmpDir, downloadUrl, runFfmpeg } = await import('../lib/video-edit/ffmpeg');
  const { rm, readdir, readFile } = await import('node:fs/promises');
  const { join } = await import('node:path');

  const tmpDir = await makeTmpDir('review-');
  try {
    const videoUrl = await getPresignedUrl(j.result.s3_key);
    const videoPath = join(tmpDir, 'render.mp4');
    await downloadUrl(videoUrl, videoPath);

    const duration = j.result.duration_seconds || 60;
    const interval = Math.max(1, Math.floor(duration / frameCount));
    const framesDir = join(tmpDir, 'frames');
    const { mkdir } = await import('node:fs/promises');
    await mkdir(framesDir, { recursive: true });

    await runFfmpeg([
      '-i', videoPath,
      '-vf', `fps=1/${interval},scale=720:-2`,
      '-q:v', '5',
      '-frames:v', String(frameCount),
      join(framesDir, 'frame_%04d.jpg'),
    ]);

    const files = (await readdir(framesDir)).filter(f => f.endsWith('.jpg')).sort();

    // Build media array with base64 data URLs (no S3 upload round-trip needed)
    const media: Array<{ url: string; type: string }> = [];
    const timestamps: string[] = [];

    for (const file of files) {
      const idx = parseInt(file.replace('frame_', '').replace('.jpg', ''), 10) - 1;
      const seconds = idx * interval;
      const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
      const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
      const s = String(seconds % 60).padStart(2, '0');
      timestamps.push(`${h}:${m}:${s}`);

      const base64 = (await readFile(join(framesDir, file))).toString('base64');
      media.push({
        url: `data:image/jpeg;base64,${base64}`,
        type: 'image/jpeg',
      });
    }

    const frameLabels = timestamps.map((t, i) => `[Frame ${i + 1}: ${t}]`).join(' ');
    const fullPrompt = `${files.length} frames, ${interval}s apart, from a ${j.result.duration_seconds}s ${j.result.width}x${j.result.height} video. ${frameLabels}\n\n${question}`;

    const result = await oneShotCompletion({
      prompt: fullPrompt,
      model,
      media,
      max_tokens: body.max_tokens || 2048,
      api_settings: apiSettings,
    });

    return c.json({
      render_id: id,
      review: result.text,
      frames_analyzed: files.length,
      model: result.model,
      provider: result.provider,
      tokens: result.tokens,
    });
  } catch (err: any) {
    if (err instanceof LlmError) {
      return c.json({ error: err.code, message: err.message }, err.status as any);
    }
    console.error('[video-edit/review] error:', err);
    return c.json({ error: 'review_failed', message: err?.message || 'Unknown error' }, 500);
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
});

// ── Jobs ─────────────────────────────────────────────────────────────────────

videoEditRouter.get('/jobs/:id', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const job = await VideoJob.findOne({ job_id: id, user_id: auth.sub }).lean();
  if (!job) return c.json({ error: 'Job not found' }, 404);

  const j = job as any;

  let result = j.result;
  if (j.status === 'completed' && result?.s3_key) {
    result = { ...result, output_url: await getPresignedUrl(result.s3_key).catch(() => result?.output_url) };
  }

  return c.json({
    id: j.job_id,
    type: j.type,
    status: j.status,
    progress: j.progress,
    result,
    error: j.error,
    created_at: j.created_at,
    updated_at: j.updated_at,
    completed_at: j.completed_at,
  });
});

videoEditRouter.post('/jobs/:id/cancel', async (c) => {
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const job = await VideoJob.findOneAndUpdate(
    { job_id: id, user_id: auth.sub, status: { $in: ['queued', 'running'] } },
    { $set: { status: 'cancelled', completed_at: new Date(), updated_at: new Date() } },
    { new: true },
  );

  if (!job) return c.json({ error: 'Job not found or not cancellable' }, 404);

  return c.json({ id: (job as any).job_id, status: 'cancelled' });
});
