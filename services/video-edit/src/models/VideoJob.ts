import mongoose, { Schema, Document } from 'mongoose';
import type { JobType, JobStatus, JobResult } from '../lib/video-edit/types';

export interface IVideoJob extends Document {
  job_id: string;
  user_id: string;
  type: JobType;
  status: JobStatus;
  progress: number;
  result: JobResult | null;
  error: string | null;
  created_at: Date;
  updated_at: Date;
  completed_at?: Date;
  ttl_expires_at?: Date;
}

const VideoJobSchema = new Schema<IVideoJob>({
  job_id:   { type: String, required: true, unique: true, index: true },
  user_id:  { type: String, required: true, index: true },
  type:     { type: String, enum: ['prepare', 'render', 'compose', 'generate'], required: true },
  status:   { type: String, enum: ['queued', 'running', 'completed', 'failed', 'cancelled'], default: 'queued' },
  progress: { type: Number, default: 0 },
  result:   { type: Schema.Types.Mixed, default: null },
  error:    { type: String, default: null },
  created_at:     { type: Date, default: Date.now },
  updated_at:     { type: Date, default: Date.now },
  completed_at:   { type: Date },
  ttl_expires_at: { type: Date },
});

VideoJobSchema.index({ user_id: 1, type: 1, status: 1 });
VideoJobSchema.index({ ttl_expires_at: 1 }, { expireAfterSeconds: 0 });

export const VideoJob = mongoose.models.VideoJob
  || mongoose.model<IVideoJob>('VideoJob', VideoJobSchema);
