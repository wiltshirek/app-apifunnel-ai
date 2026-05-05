/**
 * Asset preparation worker — downloads source assets, converts to render-ready
 * formats, uploads to S3, and updates the VideoAsset + VideoJob records.
 */

import { rm } from 'node:fs/promises';
import { join, extname } from 'node:path';

import { VideoJob } from '../../models/VideoJob';
import { VideoAsset } from '../../models/VideoAsset';
import type { AssetType, PrepareTarget, PreparedAssetOutput } from './types';
import {
  makeTmpDir, downloadUrl, probeFile,
  pdfToImages, normalizeImage, getPdfPageCount,
} from './ffmpeg';
import { uploadFile, videoS3Key } from './s3';

const TTL_HOURS = 72;

function ttlDate(): Date {
  return new Date(Date.now() + TTL_HOURS * 60 * 60 * 1000);
}

function inferAssetType(uri: string, mime?: string): AssetType {
  if (mime) {
    if (mime.startsWith('video/')) return 'video';
    if (mime.startsWith('audio/')) return 'audio';
    if (mime.startsWith('image/')) return 'image';
    if (mime === 'application/pdf') return 'pdf';
    if (mime.includes('spreadsheet') || mime.includes('csv')) return 'spreadsheet';
    if (mime.includes('document') || mime.includes('word')) return 'document';
    if (mime.includes('subtitle') || mime.includes('srt') || mime.includes('vtt')) return 'subtitle';
  }

  const ext = extname(uri).toLowerCase();
  const map: Record<string, AssetType> = {
    '.mp4': 'video', '.mov': 'video', '.webm': 'video', '.avi': 'video', '.mkv': 'video',
    '.mp3': 'audio', '.wav': 'audio', '.aac': 'audio', '.ogg': 'audio', '.flac': 'audio',
    '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image', '.gif': 'image',
    '.bmp': 'image', '.tiff': 'image', '.svg': 'image',
    '.pdf': 'pdf',
    '.xlsx': 'spreadsheet', '.xls': 'spreadsheet', '.csv': 'spreadsheet',
    '.docx': 'document', '.doc': 'document',
    '.srt': 'subtitle', '.vtt': 'subtitle', '.ass': 'subtitle',
  };
  return map[ext] || 'unknown';
}

function mimeForFormat(fmt: string): string {
  const m: Record<string, string> = {
    png: 'image/png', jpg: 'image/jpeg', webp: 'image/webp',
  };
  return m[fmt] || 'image/png';
}

export interface PrepareParams {
  jobId: string;
  assetId: string;
  userId: string;
  sourceKind: string;
  sourceUri: string;
  target: PrepareTarget;
}

export async function runPrepareJob(params: PrepareParams): Promise<void> {
  const { jobId, assetId, userId, sourceKind, sourceUri, target } = params;
  const tmpDir = await makeTmpDir('prepare-');

  try {
    await VideoJob.updateOne({ job_id: jobId }, { $set: { status: 'running', progress: 10, updated_at: new Date() } });

    const localPath = join(tmpDir, 'source' + extname(sourceUri));
    if (sourceKind === 'url' || sourceKind === 'data_url') {
      await downloadUrl(sourceUri, localPath);
    } else {
      throw new Error(`Source kind "${sourceKind}" not yet supported for asset preparation`);
    }

    await VideoJob.updateOne({ job_id: jobId }, { $set: { progress: 30, updated_at: new Date() } });

    const assetType = inferAssetType(sourceUri);
    const format = target.format || 'png';
    const outputs: PreparedAssetOutput[] = [];

    if (assetType === 'pdf') {
      const pageCount = await getPdfPageCount(localPath);
      const pagesDir = join(tmpDir, 'pages');
      await import('node:fs/promises').then(fs => fs.mkdir(pagesDir, { recursive: true }));

      const pngFiles = await pdfToImages(localPath, pagesDir, {
        pages: target.pages,
        width: target.width,
        height: target.height,
      });

      await VideoJob.updateOne({ job_id: jobId }, { $set: { progress: 60, updated_at: new Date() } });

      const uploadedPages: number[] = [];
      for (let i = 0; i < pngFiles.length; i++) {
        let finalPath = pngFiles[i];

        if (target.width && target.height) {
          const normalizedPath = join(tmpDir, `normalized_${i}.${format}`);
          await normalizeImage(pngFiles[i], normalizedPath, {
            width: target.width,
            height: target.height,
            fit: target.fit,
            background: target.background,
          });
          finalPath = normalizedPath;
        }

        const pageNum = target.pages?.[i] ?? i + 1;
        const s3Key = videoS3Key(userId, 'assets', assetId, `page_${pageNum}.${format}`);
        await uploadFile(s3Key, finalPath, mimeForFormat(format));
        uploadedPages.push(pageNum);
      }

      const s3KeyBase = videoS3Key(userId, 'assets', assetId);
      outputs.push({
        id: `${assetId}_pages`,
        asset_id: assetId,
        type: 'image_sequence',
        uri: `s3://${process.env.HETZNER_S3_ASSETS_BUCKET}/${s3KeyBase}/`,
        width: target.width,
        height: target.height,
        pages: uploadedPages,
      });

      await VideoAsset.updateOne({ asset_id: assetId }, {
        $set: {
          type: 'pdf',
          page_count: pageCount,
          prepared_outputs: outputs,
          width: target.width,
          height: target.height,
        },
      });

    } else if (assetType === 'image') {
      const probe = await probeFile(localPath).catch(() => null);
      const w = target.width || probe?.width || 1920;
      const h = target.height || probe?.height || 1080;

      const normalizedPath = join(tmpDir, `output.${format}`);
      await normalizeImage(localPath, normalizedPath, {
        width: w,
        height: h,
        fit: target.fit,
        background: target.background,
      });

      await VideoJob.updateOne({ job_id: jobId }, { $set: { progress: 70, updated_at: new Date() } });

      const s3Key = videoS3Key(userId, 'assets', assetId, `prepared.${format}`);
      await uploadFile(s3Key, normalizedPath, mimeForFormat(format));

      outputs.push({
        id: `${assetId}_prepared`,
        asset_id: assetId,
        type: 'image_sequence',
        uri: `s3://${process.env.HETZNER_S3_ASSETS_BUCKET}/${s3Key}`,
        width: w,
        height: h,
      });

      await VideoAsset.updateOne({ asset_id: assetId }, {
        $set: {
          type: 'image',
          prepared_outputs: outputs,
          width: w,
          height: h,
        },
      });

    } else if (assetType === 'video' || assetType === 'audio') {
      const probe = await probeFile(localPath);
      const s3Key = videoS3Key(userId, 'assets', assetId, `source${extname(sourceUri)}`);
      await uploadFile(s3Key, localPath, assetType === 'video' ? 'video/mp4' : 'audio/mpeg');

      outputs.push({
        id: `${assetId}_source`,
        asset_id: assetId,
        type: assetType === 'video' ? 'video_clip' : 'audio_clip',
        uri: `s3://${process.env.HETZNER_S3_ASSETS_BUCKET}/${s3Key}`,
        width: probe.width,
        height: probe.height,
        duration_seconds: probe.duration_seconds,
      });

      await VideoAsset.updateOne({ asset_id: assetId }, {
        $set: {
          type: assetType,
          prepared_outputs: outputs,
          width: probe.width,
          height: probe.height,
          duration_seconds: probe.duration_seconds,
        },
      });

    } else {
      throw new Error(`Unsupported asset type "${assetType}" for source: ${sourceUri}`);
    }

    await VideoJob.updateOne({ job_id: jobId }, {
      $set: {
        status: 'completed',
        progress: 100,
        result: { asset_id: assetId, outputs },
        completed_at: new Date(),
        updated_at: new Date(),
      },
    });

    console.log(`[video-edit/prepare] job=${jobId} asset=${assetId} completed outputs=${outputs.length}`);

  } catch (err: any) {
    console.error(`[video-edit/prepare] job=${jobId} failed:`, err);
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
