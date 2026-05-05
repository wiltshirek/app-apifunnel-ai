/**
 * FFmpeg / system command helpers for video editing.
 *
 * All subprocess calls use execFile (no shell interpolation).
 */

import { execFile } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const FFMPEG_TIMEOUT_MS = 10 * 60 * 1000;
const FFPROBE_TIMEOUT_MS = 30_000;
const MAX_DOWNLOAD_SIZE = 500 * 1024 * 1024; // 500 MB

export function execFileAsync(
  cmd: string,
  args: string[],
  opts: { timeout?: number; cwd?: string } = {},
): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 100 * 1024 * 1024, ...opts }, (err, stdout, stderr) => {
      if (err) return reject(Object.assign(err, { stderr }));
      resolve({ stdout: stdout?.toString() ?? '', stderr: stderr?.toString() ?? '' });
    });
  });
}

export async function makeTmpDir(prefix = 'video-edit-'): Promise<string> {
  return mkdtemp(join(tmpdir(), prefix));
}

export interface ProbeResult {
  width: number;
  height: number;
  duration_seconds: number;
  has_audio: boolean;
  has_video: boolean;
  codec?: string;
  fps?: number;
}

export async function probeFile(path: string): Promise<ProbeResult> {
  const { stdout } = await execFileAsync('ffprobe', [
    '-v', 'quiet',
    '-print_format', 'json',
    '-show_format', '-show_streams',
    path,
  ], { timeout: FFPROBE_TIMEOUT_MS });

  const info = JSON.parse(stdout);
  const videoStream = info.streams?.find((s: any) => s.codec_type === 'video');
  const audioStream = info.streams?.find((s: any) => s.codec_type === 'audio');

  let fps: number | undefined;
  if (videoStream?.r_frame_rate) {
    const [num, den] = videoStream.r_frame_rate.split('/').map(Number);
    if (den && den > 0) fps = Math.round((num / den) * 100) / 100;
  }

  return {
    width: videoStream?.width ?? 0,
    height: videoStream?.height ?? 0,
    duration_seconds: parseFloat(info.format?.duration ?? '0'),
    has_audio: !!audioStream,
    has_video: !!videoStream,
    codec: videoStream?.codec_name,
    fps,
  };
}

export async function downloadUrl(url: string, outputPath: string): Promise<void> {
  validateRemoteUrl(url);
  const res = await fetch(url, { signal: AbortSignal.timeout(60_000) });
  if (!res.ok) throw new Error(`Download failed: ${res.status} ${res.statusText}`);

  const contentLength = parseInt(res.headers.get('content-length') || '0', 10);
  if (contentLength > MAX_DOWNLOAD_SIZE) {
    throw new Error(`File too large: ${contentLength} bytes (max ${MAX_DOWNLOAD_SIZE})`);
  }

  const buffer = Buffer.from(await res.arrayBuffer());
  if (buffer.length > MAX_DOWNLOAD_SIZE) {
    throw new Error(`File too large: ${buffer.length} bytes (max ${MAX_DOWNLOAD_SIZE})`);
  }

  const { writeFile } = await import('node:fs/promises');
  await writeFile(outputPath, buffer);
}

/**
 * Block private/internal IPs, metadata services, localhost, and non-HTTP protocols.
 */
function validateRemoteUrl(url: string): void {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`Invalid URL: ${url}`);
  }

  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('Only HTTP(S) URLs are supported');
  }

  const host = parsed.hostname.toLowerCase();
  const blocked = [
    /^localhost$/,
    /^127\./,
    /^10\./,
    /^172\.(1[6-9]|2\d|3[01])\./,
    /^192\.168\./,
    /^169\.254\./,
    /^0\./,
    /^::1$/,
    /^fc00:/,
    /^fe80:/,
    /^metadata\.google/,
    /^169\.254\.169\.254$/,
  ];

  if (blocked.some(p => p.test(host))) {
    throw new Error('Access to internal/private addresses is blocked');
  }
}

export async function runFfmpeg(args: string[], timeout = FFMPEG_TIMEOUT_MS): Promise<string> {
  const { stderr } = await execFileAsync('ffmpeg', args, { timeout });
  return stderr;
}

/**
 * Convert a PDF to PNG pages using poppler's pdftoppm.
 */
export async function pdfToImages(
  pdfPath: string,
  outputDir: string,
  opts: { pages?: number[]; width?: number; height?: number; dpi?: number } = {},
): Promise<string[]> {
  const args: string[] = ['-png'];

  if (opts.dpi) {
    args.push('-r', String(opts.dpi));
  } else {
    args.push('-r', '150');
  }

  if (opts.width && opts.height) {
    args.push('-scale-to-x', String(opts.width), '-scale-to-y', String(opts.height));
  } else if (opts.width) {
    args.push('-scale-to', String(opts.width));
  }

  if (opts.pages && opts.pages.length > 0) {
    const first = Math.min(...opts.pages);
    const last = Math.max(...opts.pages);
    args.push('-f', String(first), '-l', String(last));
  }

  const prefix = join(outputDir, 'page');
  args.push(pdfPath, prefix);

  await execFileAsync('pdftoppm', args, { timeout: 120_000 });

  const { readdir } = await import('node:fs/promises');
  const files = (await readdir(outputDir))
    .filter(f => f.startsWith('page') && f.endsWith('.png'))
    .sort();

  if (opts.pages && opts.pages.length > 0) {
    const pageSet = new Set(opts.pages);
    return files
      .filter(f => {
        const num = parseInt(f.replace('page-', '').replace('.png', ''), 10);
        return pageSet.has(num);
      })
      .map(f => join(outputDir, f));
  }

  return files.map(f => join(outputDir, f));
}

/**
 * Normalize an image to target dimensions using ffmpeg.
 */
export async function normalizeImage(
  inputPath: string,
  outputPath: string,
  opts: { width: number; height: number; fit?: 'contain' | 'cover' | 'fill'; background?: string },
): Promise<void> {
  const bg = opts.background || 'black';
  let vf: string;

  switch (opts.fit || 'contain') {
    case 'cover':
      vf = `scale=${opts.width}:${opts.height}:force_original_aspect_ratio=increase,crop=${opts.width}:${opts.height}`;
      break;
    case 'fill':
      vf = `scale=${opts.width}:${opts.height}`;
      break;
    case 'contain':
    default:
      vf = `scale=${opts.width}:${opts.height}:force_original_aspect_ratio=decrease,pad=${opts.width}:${opts.height}:(ow-iw)/2:(oh-ih)/2:color=${bg}`;
      break;
  }

  await runFfmpeg([
    '-i', inputPath,
    '-vf', vf,
    '-frames:v', '1',
    '-y', outputPath,
  ]);
}

/**
 * Get PDF page count using pdfinfo (poppler).
 */
export async function getPdfPageCount(pdfPath: string): Promise<number> {
  try {
    const { stdout } = await execFileAsync('pdfinfo', [pdfPath], { timeout: 10_000 });
    const match = stdout.match(/Pages:\s+(\d+)/);
    return match ? parseInt(match[1], 10) : 0;
  } catch {
    return 0;
  }
}
