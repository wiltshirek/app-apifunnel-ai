/**
 * Video preprocessor — download, extract frames + transcript, align by timestamp.
 *
 * External deps (system): yt-dlp, ffmpeg (installed in Dockerfile).
 * All subprocess calls use execFile (no shell interpolation).
 * Whisper keys are server-supplied env vars, not from the caller.
 */

import { execFile } from 'node:child_process';
import { mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { LlmError } from './llm-call';

export interface VideoFrame {
  base64: string;
  timestamp: string;
}

export interface VideoPreprocessResult {
  frames: VideoFrame[];
  transcript: string;
}

interface PreprocessOptions {
  groqApiKey?: string;
  openaiWhisperKey?: string;
}

const MAX_DURATION_SECONDS = 4 * 60 * 60; // 4 hours
const DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000; // 10 min
const FFMPEG_TIMEOUT_MS = 5 * 60 * 1000; // 5 min

const VIDEO_HOST_PATTERNS = [
  /youtube\.com\/watch/i,
  /youtu\.be\//i,
  /youtube\.com\/shorts\//i,
  /vimeo\.com\//i,
];

export function isVideoUrl(url: string, mimeType?: string): boolean {
  if (mimeType?.startsWith('video/')) return true;
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) return false;
    return VIDEO_HOST_PATTERNS.some(p => p.test(url));
  } catch {
    return false;
  }
}

function validateUrl(url: string): void {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new LlmError(400, 'invalid_video_url', `Invalid URL: "${url}".`);
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new LlmError(400, 'invalid_video_url', 'Only HTTP(S) video URLs are supported.');
  }
}

function execFileAsync(
  cmd: string,
  args: string[],
  opts: { timeout?: number; cwd?: string } = {},
): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 50 * 1024 * 1024, ...opts }, (err, stdout, stderr) => {
      if (err) return reject(Object.assign(err, { stderr }));
      resolve({ stdout: stdout?.toString() ?? '', stderr: stderr?.toString() ?? '' });
    });
  });
}

async function probeDuration(url: string): Promise<number> {
  try {
    const { stdout } = await execFileAsync('yt-dlp', [
      '--dump-json', '--no-download', '--no-warnings', url,
    ], { timeout: 60_000 });
    const info = JSON.parse(stdout);
    return info.duration ?? 0;
  } catch (err: any) {
    const msg = err?.stderr ?? err?.message ?? 'Unknown error';
    if (/private|unavailable|removed|login|geo/i.test(msg)) {
      throw new LlmError(422, 'video_unavailable', `Video is unavailable: ${msg.slice(0, 200)}`);
    }
    throw new LlmError(422, 'video_unavailable', `Cannot access video: ${msg.slice(0, 200)}`);
  }
}

async function downloadVideo(url: string, tmpDir: string): Promise<string> {
  const outputTemplate = join(tmpDir, 'video.%(ext)s');
  try {
    await execFileAsync('yt-dlp', [
      '-f', 'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
      '--merge-output-format', 'mp4',
      '-o', outputTemplate,
      '--no-playlist',
      '--no-warnings',
      url,
    ], { timeout: DOWNLOAD_TIMEOUT_MS });
  } catch (err: any) {
    if (err?.killed) {
      throw new LlmError(504, 'video_timeout', `Video download timed out after ${DOWNLOAD_TIMEOUT_MS / 1000}s.`);
    }
    const msg = err?.stderr ?? err?.message ?? 'Download failed';
    if (/private|unavailable|removed|login|geo/i.test(msg)) {
      throw new LlmError(422, 'video_unavailable', `Video is private, deleted, or geo-restricted.`);
    }
    throw new LlmError(502, 'video_processing_failed', `yt-dlp download failed: ${msg.slice(0, 200)}`);
  }

  const files = await readdir(tmpDir);
  const videoFile = files.find(f => f.startsWith('video.'));
  if (!videoFile) {
    throw new LlmError(502, 'video_processing_failed', 'yt-dlp produced no output file.');
  }
  return join(tmpDir, videoFile);
}

async function extractCaptions(url: string, tmpDir: string): Promise<string | null> {
  try {
    await execFileAsync('yt-dlp', [
      '--write-auto-sub', '--write-sub',
      '--sub-lang', 'en',
      '--sub-format', 'vtt',
      '--skip-download',
      '-o', join(tmpDir, 'subs'),
      '--no-warnings',
      url,
    ], { timeout: 60_000 });
  } catch {
    return null;
  }

  const files = await readdir(tmpDir);
  const subFile = files.find(f => f.startsWith('subs') && f.endsWith('.vtt'));
  if (!subFile) return null;

  const raw = await readFile(join(tmpDir, subFile), 'utf-8');
  return parseVtt(raw);
}

function parseVtt(vtt: string): string {
  const lines = vtt.split('\n');
  const segments: string[] = [];
  let currentTime = '';
  const seen = new Set<string>();

  for (const line of lines) {
    const timeMatch = line.match(/^(\d{2}:\d{2}:\d{2})\.\d{3}\s*-->/);
    if (timeMatch) {
      currentTime = timeMatch[1];
      continue;
    }
    const text = line.replace(/<[^>]+>/g, '').trim();
    if (text && !text.startsWith('WEBVTT') && !text.match(/^\d+$/) && currentTime) {
      if (!seen.has(text)) {
        seen.add(text);
        segments.push(`${currentTime} ${text}`);
      }
    }
  }
  return segments.join('\n');
}

async function extractFrames(
  videoPath: string,
  durationSeconds: number,
  tmpDir: string,
): Promise<VideoFrame[]> {
  const frameCount = Math.max(10, Math.min(100, Math.floor(durationSeconds / 30)));
  const interval = Math.max(1, Math.floor(durationSeconds / frameCount));
  const framesDir = join(tmpDir, 'frames');

  try {
    await execFileAsync('mkdir', ['-p', framesDir]);
    await execFileAsync('ffmpeg', [
      '-i', videoPath,
      '-vf', `fps=1/${interval},scale=720:-2`,
      '-q:v', '5',
      '-frames:v', String(frameCount),
      join(framesDir, 'frame_%04d.jpg'),
    ], { timeout: FFMPEG_TIMEOUT_MS });
  } catch (err: any) {
    if (err?.killed) {
      throw new LlmError(504, 'video_timeout', 'Frame extraction timed out.');
    }
    throw new LlmError(502, 'video_processing_failed', `Frame extraction failed: ${(err?.stderr ?? err?.message ?? '').slice(0, 200)}`);
  }

  const files = (await readdir(framesDir)).filter(f => f.endsWith('.jpg')).sort();
  if (files.length === 0) {
    throw new LlmError(502, 'video_processing_failed', 'ffmpeg produced zero frames.');
  }

  const frames: VideoFrame[] = [];
  for (const file of files) {
    const idx = parseInt(file.replace('frame_', '').replace('.jpg', ''), 10) - 1;
    const seconds = idx * interval;
    const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
    const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
    const s = String(seconds % 60).padStart(2, '0');
    const base64 = (await readFile(join(framesDir, file))).toString('base64');
    frames.push({ base64, timestamp: `${h}:${m}:${s}` });
  }

  return frames;
}

async function transcribeWithWhisper(
  videoPath: string,
  tmpDir: string,
  opts: PreprocessOptions,
): Promise<string | null> {
  const audioPath = join(tmpDir, 'audio.mp3');
  try {
    await execFileAsync('ffmpeg', [
      '-i', videoPath,
      '-vn', '-acodec', 'libmp3lame', '-q:a', '4',
      audioPath,
    ], { timeout: FFMPEG_TIMEOUT_MS });
  } catch {
    return null;
  }

  const audioBuffer = await readFile(audioPath);
  if (audioBuffer.length > 25 * 1024 * 1024) {
    console.log('[video] audio file exceeds 25MB, skipping Whisper');
    return null;
  }

  const groqKey = opts.groqApiKey || process.env.GROQ_API_KEY;
  const openaiKey = opts.openaiWhisperKey || process.env.WHISPER_OPENAI_API_KEY;

  if (groqKey) {
    return callWhisperApi(
      'https://api.groq.com/openai/v1/audio/transcriptions',
      groqKey,
      'whisper-large-v3-turbo',
      audioBuffer,
    );
  }

  if (openaiKey) {
    return callWhisperApi(
      'https://api.openai.com/v1/audio/transcriptions',
      openaiKey,
      'whisper-1',
      audioBuffer,
    );
  }

  return null;
}

async function callWhisperApi(
  endpoint: string,
  apiKey: string,
  model: string,
  audioBuffer: Buffer,
): Promise<string> {
  const blob = new Blob([audioBuffer], { type: 'audio/mpeg' });
  const formData = new FormData();
  formData.append('file', blob, 'audio.mp3');
  formData.append('model', model);
  formData.append('response_format', 'verbose_json');
  formData.append('timestamp_granularities[]', 'segment');

  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${apiKey}` },
    body: formData,
    signal: AbortSignal.timeout(5 * 60 * 1000),
  });

  if (!res.ok) {
    const err: any = await res.json().catch(() => ({}));
    throw new LlmError(502, 'transcript_failed', `Whisper transcription failed: ${err?.error?.message || res.statusText}`);
  }

  const data: any = await res.json();
  if (data.segments && Array.isArray(data.segments)) {
    return data.segments.map((seg: any) => {
      const s = Math.floor(seg.start ?? 0);
      const h = String(Math.floor(s / 3600)).padStart(2, '0');
      const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
      const sec = String(s % 60).padStart(2, '0');
      return `${h}:${m}:${sec} ${seg.text?.trim() ?? ''}`;
    }).join('\n');
  }

  return data.text ?? '';
}

export async function preprocessVideo(
  url: string,
  opts: PreprocessOptions = {},
): Promise<VideoPreprocessResult> {
  validateUrl(url);

  console.log(`[video] start url=${url}`);
  const pipelineStart = Date.now();

  const duration = await probeDuration(url);
  console.log(`[video] probe duration=${duration}s`);

  if (duration > MAX_DURATION_SECONDS) {
    throw new LlmError(400, 'video_too_long', `Video duration ${duration}s exceeds maximum ${MAX_DURATION_SECONDS}s.`);
  }

  const tmpDir = await mkdtemp(join(tmpdir(), 'video-'));

  try {
    const [captionText, videoPath] = await Promise.all([
      extractCaptions(url, tmpDir),
      downloadVideo(url, tmpDir),
    ]);

    const downloadElapsed = Date.now() - pipelineStart;
    console.log(`[video] download complete elapsed=${downloadElapsed}ms`);

    let transcript = captionText;
    const captionSource = captionText ? 'captions' : 'none';

    if (!transcript) {
      console.log('[video] no captions found, attempting Whisper');
      transcript = await transcribeWithWhisper(videoPath, tmpDir, opts);
    }

    if (!transcript) {
      const hasKey = !!(opts.groqApiKey || process.env.GROQ_API_KEY || opts.openaiWhisperKey || process.env.WHISPER_OPENAI_API_KEY);
      if (!hasKey) {
        throw new LlmError(422, 'transcript_unavailable', 'No captions found and no API key available for Whisper fallback.');
      }
      transcript = '';
    }

    console.log(`[video] transcript source=${captionText ? 'captions' : 'whisper'} length=${transcript.length}`);

    const effectiveDuration = duration || 300;
    const frames = await extractFrames(videoPath, effectiveDuration, tmpDir);
    const framesElapsed = Date.now() - pipelineStart - downloadElapsed;
    console.log(`[video] frames extracted count=${frames.length} elapsed=${framesElapsed}ms`);

    const totalElapsed = Date.now() - pipelineStart;
    console.log(`[video] pipeline complete total_elapsed=${totalElapsed}ms`);

    return { frames, transcript };
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
}
