/**
 * Compose worker — builds an ffmpeg filter graph from a ComposeRequest,
 * renders the output, uploads to S3, and updates job records.
 *
 * This is the "simple inserts" renderer: take a source video, insert
 * assets at specified timestamps with positioning/transitions/animation,
 * render to a single output MP4.
 */

import { rm, readdir, stat } from 'node:fs/promises';
import { join, extname } from 'node:path';

import { VideoJob } from '../../models/VideoJob';
import { VideoAsset } from '../../models/VideoAsset';
import type { ComposeRequest, ComposeInsert, OutputSpec } from './types';
import { makeTmpDir, downloadUrl, probeFile, runFfmpeg } from './ffmpeg';
import { uploadFile, videoS3Key, getPresignedUrl } from './s3';

const TTL_HOURS = 72;

function parseTimestamp(ts: string | number): number {
  if (typeof ts === 'number') return ts;
  const parts = ts.split(':').map(Number);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parseFloat(ts) || 0;
}

/**
 * Resolve an asset URI to a local file path.
 * Supports: direct URLs, s3:// URIs (via presigned URL), asset:// references.
 */
async function resolveAssetToLocal(
  uri: string,
  userId: string,
  tmpDir: string,
  index: number,
): Promise<string> {
  if (uri.startsWith('asset://lakehouse/')) {
    const assetRef = uri.replace('asset://lakehouse/', '');
    const asset = await VideoAsset.findOne({ asset_id: assetRef, user_id: userId }).lean();
    if (!asset) throw new Error(`Asset not found: ${assetRef}`);

    const prepared = (asset as any).prepared_outputs?.[0];
    if (prepared?.uri) {
      return resolveAssetToLocal(prepared.uri, userId, tmpDir, index);
    }
    throw new Error(`Asset ${assetRef} has no prepared outputs`);
  }

  if (uri.startsWith('s3://')) {
    const withoutProtocol = uri.replace('s3://', '');
    const slashIdx = withoutProtocol.indexOf('/');
    const s3Key = withoutProtocol.substring(slashIdx + 1);
    const presignedUrl = await getPresignedUrl(s3Key);
    return resolveAssetToLocal(presignedUrl, userId, tmpDir, index);
  }

  const ext = extname(new URL(uri).pathname) || '.bin';
  const localPath = join(tmpDir, `insert_${index}${ext}`);
  await downloadUrl(uri, localPath);
  return localPath;
}

interface ResolvedInsert {
  localPath: string;
  startSeconds: number;
  duration: number;
  insert: ComposeInsert;
  isImage: boolean;
  isVideo: boolean;
  width?: number;
  height?: number;
}

/**
 * Build the ffmpeg command to compose inserts into a source video.
 *
 * Strategy:
 * - For fullscreen/cutaway inserts: split the source at insert points, overlay
 *   the asset image/video, then concatenate segments.
 * - For pip/overlay inserts: use the overlay filter at the specified time range.
 * - Audio behavior depends on the insert's `audio` field.
 *
 * This Phase 1 implementation handles the most common case: sequential
 * fullscreen inserts with crossfade transitions and optional ken burns.
 */
function buildComposeCommand(
  sourcePath: string,
  inserts: ResolvedInsert[],
  output: OutputSpec,
  outputPath: string,
): string[] {
  const w = output.width || 1920;
  const h = output.height || 1080;
  const fps = output.fps || 30;

  if (inserts.length === 0) {
    return [
      '-i', sourcePath,
      '-vf', `scale=${w}:${h}:force_original_aspect_ratio=decrease,pad=${w}:${h}:(ow-iw)/2:(oh-ih)/2`,
      '-c:v', 'libx264', '-preset', 'medium', '-crf', qualityCrf(output.quality),
      '-c:a', 'aac', '-b:a', '192k',
      '-r', String(fps),
      '-movflags', '+faststart',
      '-y', outputPath,
    ];
  }

  const sorted = [...inserts].sort((a, b) => a.startSeconds - b.startSeconds);
  const inputs: string[] = ['-i', sourcePath];
  const filterParts: string[] = [];

  for (const ins of sorted) {
    if (ins.isImage) {
      inputs.push('-loop', '1', '-t', String(ins.duration), '-i', ins.localPath);
    } else {
      inputs.push('-i', ins.localPath);
    }
  }

  // Scale source to target dimensions
  filterParts.push(`[0:v]scale=${w}:${h}:force_original_aspect_ratio=decrease,pad=${w}:${h}:(ow-iw)/2:(oh-ih)/2,setsar=1[base]`);

  let currentLabel = 'base';

  for (let i = 0; i < sorted.length; i++) {
    const ins = sorted[i];
    const inputIdx = i + 1;
    const nextLabel = `out${i}`;

    if (ins.insert.mode === 'pip') {
      const pipW = parseSizeSpec(ins.insert.size || '25%', w);
      const pipH = Math.round(pipW * (ins.height && ins.width ? ins.height / ins.width : 9 / 16));
      const pos = pipPosition(ins.insert.position || 'bottom-right', w, h, pipW, pipH);

      filterParts.push(
        `[${inputIdx}:v]scale=${pipW}:${pipH}:force_original_aspect_ratio=decrease,setsar=1[pip${i}]`
      );
      filterParts.push(
        `[${currentLabel}][pip${i}]overlay=${pos.x}:${pos.y}:enable='between(t,${ins.startSeconds},${ins.startSeconds + ins.duration})'[${nextLabel}]`
      );
    } else if (ins.insert.mode === 'overlay') {
      filterParts.push(
        `[${inputIdx}:v]scale=${w}:${h}:force_original_aspect_ratio=decrease,format=rgba,colorchannelmixer=aa=0.85[ovl${i}]`
      );
      filterParts.push(
        `[${currentLabel}][ovl${i}]overlay=(W-w)/2:(H-h)/2:enable='between(t,${ins.startSeconds},${ins.startSeconds + ins.duration})'[${nextLabel}]`
      );
    } else {
      // fullscreen / cutaway / side_by_side / background — replace video, keep or mute audio
      const anim = ins.insert.animation;
      if (anim && anim.type === 'ken_burns') {
        const fromScale = anim.from.scale;
        const toScale = anim.to.scale;
        const fromX = parsePercent(anim.from.x);
        const fromY = parsePercent(anim.from.y);
        const toX = parsePercent(anim.to.x);
        const toY = parsePercent(anim.to.y);
        const d = ins.duration;
        const zoomW = Math.round(w * Math.max(fromScale, toScale));
        const zoomH = Math.round(h * Math.max(fromScale, toScale));

        filterParts.push(
          `[${inputIdx}:v]scale=${zoomW}:${zoomH}:force_original_aspect_ratio=decrease,pad=${zoomW}:${zoomH}:(ow-iw)/2:(oh-ih)/2,` +
          `crop=${w}:${h}:` +
          `'${lerp(fromX * zoomW - w / 2, toX * zoomW - w / 2, d)}':` +
          `'${lerp(fromY * zoomH - h / 2, toY * zoomH - h / 2, d)}',` +
          `setsar=1[fs${i}]`
        );
      } else {
        filterParts.push(
          `[${inputIdx}:v]scale=${w}:${h}:force_original_aspect_ratio=decrease,pad=${w}:${h}:(ow-iw)/2:(oh-ih)/2,setsar=1[fs${i}]`
        );
      }

      const fadeDur = ins.insert.transition_duration || 0;
      if (fadeDur > 0 && ins.insert.transition === 'fade') {
        filterParts.push(
          `[fs${i}]fade=in:d=${fadeDur},fade=out:st=${ins.duration - fadeDur}:d=${fadeDur}[ffs${i}]`
        );
        filterParts.push(
          `[${currentLabel}][ffs${i}]overlay=0:0:enable='between(t,${ins.startSeconds},${ins.startSeconds + ins.duration})'[${nextLabel}]`
        );
      } else {
        filterParts.push(
          `[${currentLabel}][fs${i}]overlay=0:0:enable='between(t,${ins.startSeconds},${ins.startSeconds + ins.duration})'[${nextLabel}]`
        );
      }
    }

    currentLabel = nextLabel;
  }

  const filterGraph = filterParts.join(';\n');

  return [
    ...inputs,
    '-filter_complex', filterGraph,
    '-map', `[${currentLabel}]`,
    '-map', '0:a?',
    '-c:v', 'libx264', '-preset', 'medium', '-crf', qualityCrf(output.quality),
    '-c:a', 'aac', '-b:a', '192k',
    '-r', String(fps),
    '-movflags', '+faststart',
    '-y', outputPath,
  ];
}

function qualityCrf(quality?: string): string {
  switch (quality) {
    case 'low': return '28';
    case 'high': return '18';
    default: return '23';
  }
}

function parseSizeSpec(size: string, reference: number): number {
  if (size.endsWith('%')) {
    return Math.round(reference * parseFloat(size) / 100);
  }
  return parseInt(size, 10) || Math.round(reference * 0.25);
}

function pipPosition(
  pos: string,
  canvasW: number, canvasH: number,
  pipW: number, pipH: number,
): { x: number; y: number } {
  const margin = 20;
  switch (pos) {
    case 'top-left': return { x: margin, y: margin };
    case 'top-right': return { x: canvasW - pipW - margin, y: margin };
    case 'bottom-left': return { x: margin, y: canvasH - pipH - margin };
    case 'center': return { x: Math.round((canvasW - pipW) / 2), y: Math.round((canvasH - pipH) / 2) };
    case 'bottom-right':
    default: return { x: canvasW - pipW - margin, y: canvasH - pipH - margin };
  }
}

function parsePercent(s: string): number {
  if (typeof s === 'number') return s;
  if (s.endsWith('%')) return parseFloat(s) / 100;
  return parseFloat(s) || 0.5;
}

function lerp(from: number, to: number, duration: number): string {
  if (from === to) return String(Math.round(Math.max(0, from)));
  return `${Math.round(from)}+(${Math.round(to - from)})*t/${duration}`;
}

// ── Public API ───────────────────────────────────────────────────────────────

export interface ComposeJobParams {
  jobId: string;
  userId: string;
  request: ComposeRequest;
}

export async function runComposeJob(params: ComposeJobParams): Promise<void> {
  const { jobId, userId, request } = params;
  const tmpDir = await makeTmpDir('compose-');

  try {
    await VideoJob.updateOne({ job_id: jobId }, { $set: { status: 'running', progress: 5, updated_at: new Date() } });

    // Resolve source video
    const sourcePath = join(tmpDir, 'source.mp4');
    if (request.source_video.startsWith('asset://lakehouse/')) {
      const assetRef = request.source_video.replace('asset://lakehouse/', '');
      const asset = await VideoAsset.findOne({ asset_id: assetRef, user_id: userId }).lean();
      if (!asset) throw new Error(`Source video asset not found: ${assetRef}`);
      const prepared = (asset as any).prepared_outputs?.[0];
      if (prepared?.uri?.startsWith('s3://')) {
        const s3Key = prepared.uri.replace(/^s3:\/\/[^/]+\//, '');
        const url = await getPresignedUrl(s3Key);
        await downloadUrl(url, sourcePath);
      } else {
        throw new Error(`Source asset ${assetRef} has no downloadable output`);
      }
    } else {
      await downloadUrl(request.source_video, sourcePath);
    }

    await VideoJob.updateOne({ job_id: jobId }, { $set: { progress: 20, updated_at: new Date() } });

    const sourceProbe = await probeFile(sourcePath);

    // Resolve all insert assets
    const resolvedInserts: ResolvedInsert[] = [];
    for (let i = 0; i < request.inserts.length; i++) {
      const ins = request.inserts[i];
      const localPath = await resolveAssetToLocal(ins.asset, userId, tmpDir, i);
      const probe = await probeFile(localPath).catch(() => null);
      const isImage = !probe?.has_video || (probe.duration_seconds < 0.5);
      resolvedInserts.push({
        localPath,
        startSeconds: parseTimestamp(ins.at),
        duration: ins.duration,
        insert: ins,
        isImage,
        isVideo: !!probe?.has_video && probe.duration_seconds >= 0.5,
        width: probe?.width,
        height: probe?.height,
      });
    }

    await VideoJob.updateOne({ job_id: jobId }, { $set: { progress: 40, updated_at: new Date() } });

    // Build and run ffmpeg command
    const outputPath = join(tmpDir, 'output.mp4');
    const args = buildComposeCommand(sourcePath, resolvedInserts, request.output, outputPath);

    console.log(`[video-edit/compose] job=${jobId} running ffmpeg with ${args.length} args`);
    await runFfmpeg(args, 30 * 60 * 1000); // 30 min timeout for renders

    await VideoJob.updateOne({ job_id: jobId }, { $set: { progress: 80, updated_at: new Date() } });

    // Probe output and upload to S3
    const outputProbe = await probeFile(outputPath);
    const outputStat = await stat(outputPath);
    const s3Key = videoS3Key(userId, 'renders', jobId, 'output.mp4');
    await uploadFile(s3Key, outputPath, 'video/mp4', {
      'x-amz-meta-job-id': jobId,
      'x-amz-meta-ttl-hours': String(TTL_HOURS),
    });

    const outputUrl = await getPresignedUrl(s3Key);

    await VideoJob.updateOne({ job_id: jobId }, {
      $set: {
        status: 'completed',
        progress: 100,
        result: {
          output_url: outputUrl,
          s3_key: s3Key,
          duration_seconds: outputProbe.duration_seconds,
          width: outputProbe.width,
          height: outputProbe.height,
          file_size_bytes: outputStat.size,
        },
        completed_at: new Date(),
        updated_at: new Date(),
        ttl_expires_at: new Date(Date.now() + TTL_HOURS * 60 * 60 * 1000),
      },
    });

    console.log(`[video-edit/compose] job=${jobId} completed duration=${outputProbe.duration_seconds}s size=${outputStat.size}`);

  } catch (err: any) {
    console.error(`[video-edit/compose] job=${jobId} failed:`, err);
    await VideoJob.updateOne({ job_id: jobId }, {
      $set: {
        status: 'failed',
        error: err?.message || 'Unknown error',
        completed_at: new Date(),
        updated_at: new Date(),
      },
    });
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
}
