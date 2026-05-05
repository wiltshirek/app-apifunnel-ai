/**
 * Hetzner S3-compatible storage client for video editing outputs.
 *
 * Reuses the same env vars as the lakehouse Python service:
 *   HETZNER_S3_ENDPOINT, HETZNER_S3_ACCESS_KEY, HETZNER_S3_SECRET,
 *   HETZNER_S3_REGION, HETZNER_S3_ASSETS_BUCKET
 */

import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
  DeleteObjectCommand,
  HeadObjectCommand,
} from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { readFile } from 'node:fs/promises';

let _client: S3Client | null = null;

function getClient(): S3Client | null {
  if (_client) return _client;

  const endpoint = process.env.HETZNER_S3_ENDPOINT;
  if (!endpoint) {
    console.warn('[video-edit/s3] HETZNER_S3_ENDPOINT not set — S3 disabled');
    return null;
  }

  _client = new S3Client({
    endpoint,
    region: process.env.HETZNER_S3_REGION || 'hel1',
    credentials: {
      accessKeyId: process.env.HETZNER_S3_ACCESS_KEY || '',
      secretAccessKey: process.env.HETZNER_S3_SECRET || '',
    },
    forcePathStyle: true,
  });
  return _client;
}

function bucket(): string {
  const b = process.env.HETZNER_S3_ASSETS_BUCKET;
  if (!b) throw new Error('HETZNER_S3_ASSETS_BUCKET not set');
  return b;
}

const VIDEO_PREFIX = 'video-edit';

export function videoS3Key(userId: string, ...parts: string[]): string {
  return [VIDEO_PREFIX, userId, ...parts].join('/');
}

export async function uploadBuffer(
  s3Key: string,
  body: Buffer,
  contentType: string,
  metadata?: Record<string, string>,
): Promise<string> {
  const client = getClient();
  if (!client) throw new Error('S3 not configured');

  await client.send(new PutObjectCommand({
    Bucket: bucket(),
    Key: s3Key,
    Body: body,
    ContentType: contentType,
    Metadata: metadata,
  }));

  return `s3://${bucket()}/${s3Key}`;
}

export async function uploadFile(
  s3Key: string,
  localPath: string,
  contentType: string,
  metadata?: Record<string, string>,
): Promise<string> {
  const data = await readFile(localPath);
  return uploadBuffer(s3Key, data, contentType, metadata);
}

export async function getPresignedUrl(
  s3Key: string,
  expiresIn = 3600,
): Promise<string> {
  const client = getClient();
  if (!client) throw new Error('S3 not configured');

  return getSignedUrl(client, new GetObjectCommand({
    Bucket: bucket(),
    Key: s3Key,
  }), { expiresIn });
}

export async function deleteObject(s3Key: string): Promise<void> {
  const client = getClient();
  if (!client) return;

  await client.send(new DeleteObjectCommand({
    Bucket: bucket(),
    Key: s3Key,
  }));
}

export async function headObject(s3Key: string): Promise<{ size: number; contentType?: string } | null> {
  const client = getClient();
  if (!client) return null;

  try {
    const res = await client.send(new HeadObjectCommand({
      Bucket: bucket(),
      Key: s3Key,
    }));
    return { size: res.ContentLength ?? 0, contentType: res.ContentType };
  } catch {
    return null;
  }
}
